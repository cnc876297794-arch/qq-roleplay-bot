"""DeepSeek API client with token budget control for the roleplay plugin."""

import json
import time

import httpx

# 使用 nonebot 的全局 logger，确保日志被框架正确格式化输出
try:
    from nonebot.log import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)


class TokenBudget:
    """Daily token budget control with auto-reset at midnight."""

    def __init__(self, daily_limit: int = 5000, name: str = "default"):
        """
        Initialize token budget.

        :param daily_limit: Maximum tokens allowed per day
        :param name: Budget name for logging
        """
        self.daily_limit = daily_limit
        self.name = name
        self.used_today = 0
        self.reset_date: str | None = None

    def _check_reset(self) -> None:
        """Reset counter if day has changed."""
        today = time.strftime("%Y-%m-%d")
        if self.reset_date != today:
            self.reset_date = today
            self.used_today = 0

    def can_consume(self, estimated_tokens: int) -> bool:
        """
        Check if estimated tokens can be consumed without exceeding budget.

        :param estimated_tokens: Estimated token count
        :return: True if within budget
        """
        self._check_reset()
        return self.used_today + estimated_tokens < self.daily_limit

    def consume(self, tokens: int) -> None:
        """Record consumed tokens."""
        self._check_reset()
        self.used_today += tokens

    def reset(self) -> None:
        """Force reset today's counter."""
        self.used_today = 0
        self.reset_date = time.strftime("%Y-%m-%d")

    @property
    def remaining(self) -> int:
        """Get remaining tokens for today."""
        self._check_reset()
        return max(0, self.daily_limit - self.used_today)

    def __repr__(self) -> str:
        return f"<TokenBudget name={self.name} used={self.used_today}/{self.daily_limit}>"


class AIClient:
    """Async client for DeepSeek Chat Completions API with budget tracking."""

    def __init__(self, api_key: str, api_base: str, model: str = "deepseek-v4-flash",
                 max_tokens: int = 500, temperature: float = 0.7,
                 reply_budget: int = 30000, learning_budget: int = 10000,
                 thinking_enabled: bool = False,
                 reasoning_effort: str = "high",
                 thinking_max_tokens: int = 2000,
                 strip_reasoning_from_log: bool = False):
        """
        Initialize AI client with separate budgets for reply and learning.

        :param api_key: DeepSeek API key
        :param api_base: API base URL (e.g. https://api.deepseek.com)
        :param model: Model name
        :param max_tokens: Default max tokens for responses
        :param temperature: Default temperature
        :param reply_budget: Daily token budget for chat replies
        :param learning_budget: Daily token budget for learning tasks (topic/personality)
        :param thinking_enabled: 启用 DeepSeek 思考模式（thinking.type=enabled）
        :param reasoning_effort: 思考强度，"high" 或 "max"
        :param thinking_max_tokens: 思考模式下的 max_tokens 上限（需比普通模式更大）
        :param strip_reasoning_from_log: 是否在日志中隐藏 reasoning_content
        """
        self.api_key = api_key
        # 处理 api_base: 如果以 /v1 结尾，直接使用；否则保留原值供后续拼接
        base = api_base.rstrip("/")
        self.api_base = base
        self._api_base_has_v1 = base.endswith("/v1")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.client = httpx.AsyncClient(timeout=30.0)

        # 分离两个独立预算
        self.reply_budget = TokenBudget(daily_limit=reply_budget, name="reply")
        self.learning_budget = TokenBudget(daily_limit=learning_budget, name="learning")
        # 向后兼容：budget 指向 reply_budget
        self.budget = self.reply_budget

        # DeepSeek 思考模式配置
        self.thinking_enabled = thinking_enabled
        self.reasoning_effort = reasoning_effort
        self.thinking_max_tokens = thinking_max_tokens
        self.strip_reasoning_from_log = strip_reasoning_from_log

    def _clean_cot(self, text: str) -> str:
        """
        清理 minimax-m3-free 模型的推理过程(CoT/思考链)。
        策略：移除【】内的内容，以及明显的推理行。
        """
        if not text:
            return text
        import re

        # 1. 移除【】内的所有内容（中文推理）
        text = re.sub(r'【[^】]*】', '', text)
        # 也处理未闭合的【
        text = re.sub(r'【[^】]*$', '', text, flags=re.MULTILINE)

        lines = text.split('\n')
        clean_lines = []

        # 推理特征词
        reasoning_patterns = [
            r'用户说', r'根据规则', r'我应该回', r'我应该',
            r'按照.*规则', r'根据.*设定', r'回复.*应该是',
            r'所以.*回', r'应该.*回复', r'那么.*回',
        ]

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            # 跳过明显是推理的行
            is_reasoning = any(re.search(p, stripped) for p in reasoning_patterns)
            if is_reasoning:
                continue

            # 移除开头的【】残留
            stripped = re.sub(r'^【[^】]*】\s*', '', stripped)
            stripped = re.sub(r'^【.*$', '', stripped)

            if not stripped:
                continue

            clean_lines.append(stripped)

        result = '\n'.join(clean_lines)

        # 2. 过滤掉（）内的动作/心理描写，如（脸微微发红）（带着哭腔）
        result = re.sub(r'（[^）]*）', '', result)
        # 也处理半角括号
        result = re.sub(r'\([^)]*\)', '', result)
        # 移除连续空格
        result = re.sub(r'\s+', ' ', result)

        return result.strip()

    async def chat(self, messages: list[dict], max_tokens: int | None = None,
                   temperature: float | None = None,
                   frequency_penalty: float | None = None,
                   presence_penalty: float | None = None) -> str:
        """
        Send chat completion request.

        :param messages: List of {"role": str, "content": str}
        :param max_tokens: Override default max tokens
        :param temperature: Override default temperature
        :param frequency_penalty: Penalize repeated tokens (positive = less repetition)
        :param presence_penalty: Penalize already-mentioned topics (positive = explore new topics)
        :return: Reply text, or empty string on error/budget-exceeded
        """
        # Rough token estimation (Chinese ~2 chars/token)
        estimated = sum(len(m.get("content", "")) // 2 for m in messages)
        if not self.budget.can_consume(estimated):
            return "[今日回复预算已用完，暂时无法回复]"

        try:
            # 思考模式下，max_tokens 必须用 thinking_max_tokens（输出更长）
            effective_max_tokens = (
                self.thinking_max_tokens if self.thinking_enabled
                else (max_tokens or self.max_tokens)
            )

            payload = {
                "model": self.model,
                "messages": messages,
                "max_tokens": effective_max_tokens,
                "temperature": temperature or self.temperature,
            }
            if frequency_penalty is not None:
                payload["frequency_penalty"] = frequency_penalty
            if presence_penalty is not None:
                payload["presence_penalty"] = presence_penalty

            # 注入 DeepSeek 思考模式
            if self.thinking_enabled:
                payload["thinking"] = {"type": "enabled"}
                # reasoning_effort 合法值：high / max
                if self.reasoning_effort in ("high", "max"):
                    payload["reasoning_effort"] = self.reasoning_effort
                else:
                    logger.warning(
                        f"[AIClient] 非法的 reasoning_effort={self.reasoning_effort!r}，"
                        f"合法值为 'high' 或 'max'，已回退到 'high'"
                    )
                    payload["reasoning_effort"] = "high"
                logger.debug(
                    f"[AIClient] thinking mode enabled, reasoning_effort={payload['reasoning_effort']}"
                )

            resp = await self.client.post(
                f"{self.api_base}/chat/completions" if self._api_base_has_v1 else f"{self.api_base}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            message = data["choices"][0]["message"]
            raw_reply = message.get("content") or ""
            # 思考模式下 reasoning_content 与 content 同级，仅用于日志
            reasoning_content = message.get("reasoning_content") or ""

            # 日志：原始回复 + 思考链长度（可选隐藏）
            log_reply = raw_reply[:120]
            if reasoning_content:
                if self.strip_reasoning_from_log:
                    logger.info(
                        f"[API Raw] {log_reply} | reasoning=<hidden len={len(reasoning_content)}>"
                    )
                else:
                    logger.info(
                        f"[API Raw] {log_reply} | reasoning={reasoning_content[:80]!r}... "
                        f"(total len={len(reasoning_content)})"
                    )
            else:
                logger.info(f"[API Raw] {log_reply}")

            reply = self._clean_cot(raw_reply)
            usage = data.get("usage", {})
            total_tokens = usage.get("total_tokens", estimated)
            self.budget.consume(total_tokens)

            # DeepSeek KV Cache 命中率统计
            cache_hit = usage.get("prompt_cache_hit_tokens", 0)
            cache_miss = usage.get("prompt_cache_miss_tokens", 0)
            if cache_hit + cache_miss > 0:
                hit_rate = cache_hit / (cache_hit + cache_miss) * 100
                logger.info(f"[Cache] hit={cache_hit}, miss={cache_miss}, rate={hit_rate:.1f}%")

            return reply
        except Exception as e:
            logger.error(f"[AIClient Error] {e}")
            return ""

    async def chat_for_learning(self, messages: list[dict]) -> tuple[str, int]:
        """
        Optimized chat for learning tasks with lower temperature and max tokens.
        Uses separate learning_budget instead of reply budget.

        :param messages: List of chat messages
        :return: (reply_text, tokens_used)
        """
        estimated = sum(len(m.get("content", "")) // 2 for m in messages)
        if not self.learning_budget.can_consume(estimated):
            return "", 0
        try:
            payload = {
                "model": self.model,
                "messages": messages,
                "max_tokens": 300,
                "temperature": 0.3,
            }
            # 学习任务也跟随 thinking_enabled 开关（统一由 bot.yaml thinking.enabled 控制）
            if self.thinking_enabled:
                payload["thinking"] = {"type": "enabled"}
                if self.reasoning_effort in ("high", "max"):
                    payload["reasoning_effort"] = self.reasoning_effort

            resp = await self.client.post(
                f"{self.api_base}/chat/completions" if self._api_base_has_v1 else f"{self.api_base}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                json=payload
            )
            resp.raise_for_status()
            data = resp.json()
            raw_reply = data["choices"][0]["message"].get("content") or ""
            reply = self._clean_cot(raw_reply)
            usage = data.get("usage", {})
            tokens = usage.get("total_tokens", estimated)
            self.learning_budget.consume(tokens)
            return reply, tokens
        except Exception as e:
            logger.error(f"[AIClient Learning Error] {e}")
            return "", 0
