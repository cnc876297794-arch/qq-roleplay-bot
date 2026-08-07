"""Reply probability decider for group chat roleplay bot."""

import random
import time
from datetime import datetime

try:
    from nonebot.log import logger
except ImportError:
    import logging
    logger = logging.getLogger("reply_decider")


class ReplyDecider:
    """
    Decides whether the bot should reply to a group message.

    Rules:
      - @'d -> always reply (100%)
      - Otherwise -> per-group base probability (default 1%) with dynamic adjustments
      - Cooldown between replies (default 5 minutes, per-group)
      - Daily reply limit per group (default 20, per-group)

    Per-group override:
      `set_group_probability(group_id, p)` 单独设置某个群的概率，
      启动时/热重载时从 config.groups.{id}.reply_probability 注入。
      同一个 ReplyDecider 单例服务所有群，base_probability 只是
      注册表里没有命中时的兜底。
    """

    def __init__(self, config: dict | None = None):
        """
        Initialize decider.

        :param config: Dict with keys:
            reply_probability: float = 0.01    # 兜底（未注册群使用）
            reply_cooldown: int = 300 (seconds)
            max_daily_replies: int = 20
            refresh_times: list[int] = [8, 12, 18]  # 每日刷新时间点（小时）
            at_debounce: int = 2 (seconds)  # @触发防抖（同一群内同一用户）
        """
        cfg = config or {}
        self._default_probability = cfg.get("reply_probability", 0.01)
        self._group_probability: dict[int, float] = {}   # group_id -> 概率
        self.cooldown = cfg.get("reply_cooldown", 300)
        self.max_daily_replies = cfg.get("max_daily_replies", 20)
        self.refresh_times = sorted(cfg.get("refresh_times", [8, 12, 18]))
        self.at_debounce = cfg.get("at_debounce", 5)  # @触发防抖秒数

        # 每个时段配额 = max_daily_replies（每个时段独立配额）
        self.period_quota = self.max_daily_replies

        self.last_reply_time: dict[int, float] = {}    # group_id -> timestamp（非@冷却用）
        self.last_at_time: dict[tuple[int, int], float] = {}  # (group_id, user_id) -> timestamp（@防抖用，per-user）
        self.period_reply_count: dict[int, int] = {}   # group_id -> 当前时段已用
        self._current_period_index: int = -1

    @property
    def base_probability(self) -> float:
        """兼容旧接口：返回默认概率（兜底值）。"""
        return self._default_probability

    @base_probability.setter
    def base_probability(self, value: float) -> None:
        """兼容旧接口：设置默认概率（兜底值），不动 per-group 覆盖。"""
        self._default_probability = float(value)

    def get_group_probability(self, group_id: int) -> float:
        """获取该群生效概率（注册覆盖 > 兜底默认）。"""
        return self._group_probability.get(int(group_id), self._default_probability)

    def set_group_probability(self, group_id: int, probability: float) -> None:
        """注册或更新某个群的自定义概率。"""
        self._group_probability[int(group_id)] = float(probability)

    def apply_group_config(self, group_id: int, group_cfg: dict) -> None:
        """从 group_cfg 抽取 reply_probability 并注册（如果有显式配置）。"""
        if not isinstance(group_cfg, dict):
            return
        if "reply_probability" in group_cfg:
            self.set_group_probability(group_id, group_cfg["reply_probability"])

    def apply_groups_config(self, groups_cfg: dict) -> None:
        """
        批量注入所有群配置。
        注意：只覆盖 group_cfg 里显式给出 reply_probability 的群，
        不会清空之前注册过的（避免热重载丢配置）。
        """
        if not isinstance(groups_cfg, dict):
            return
        for raw_gid, gcfg in groups_cfg.items():
            if raw_gid == "default":
                continue
            try:
                gid = int(raw_gid)
            except (TypeError, ValueError):
                continue
            self.apply_group_config(gid, gcfg)

    def _get_current_period(self) -> int:
        """根据刷新时间点，返回当前所属时段索引。"""
        hour = datetime.now().hour
        for i, h in enumerate(self.refresh_times):
            if hour < h:
                return i - 1
        return len(self.refresh_times) - 1

    def _check_period_reset(self) -> None:
        """检查是否跨时段，如果是则重置计数。"""
        period = self._get_current_period()
        # 8点之前的时段（如 0:00-8:00）映射为最后一个时段
        if period == -1:
            period = len(self.refresh_times) - 1

        if period != self._current_period_index:
            self._current_period_index = period
            self.period_reply_count.clear()

    def reset_period_counts(self) -> None:
        """强制重置当前时段计数（供定时任务精确刷新时调用）。"""
        self._check_period_reset()  # 先同步时段
        self.period_reply_count.clear()
    
    def should_reply(self, group_id: int, is_at_me: bool = False,
                     content_length: int = 0, sender_activity: float = 0.5,
                     user_id: int = 0) -> bool:
        """
        Decide whether to reply to a group message.

        :param group_id: Group ID
        :param is_at_me: Whether the bot was @'d
        :param content_length: Message content length in chars
        :param sender_activity: Sender activity score 0.0-1.0
        :param user_id: Sender QQ ID（用于 @ 防抖，per-user 互不干扰）
        :return: True if should reply
        """
        # Rule 1: @'d -> always reply（per-user 防抖，不同用户/不同群同时 @ 互不阻塞）
        if is_at_me:
            now = time.time()
            last = self.last_at_time.get((group_id, user_id), 0)
            if now - last < self.at_debounce:
                logger.info(
                    f"[AtDebounce] g={group_id} user={user_id} 被防抖挡："
                    f"距上次 {now - last:.1f}s（阈值 {self.at_debounce}s）"
                )
                return False
            self.last_at_time[(group_id, user_id)] = now
            self._record_reply(group_id)
            return True
            
        self._check_period_reset()

        # Rule 2: cooldown check
        now = time.time()
        if group_id in self.last_reply_time:
            if now - self.last_reply_time[group_id] < self.cooldown:
                return False

        # Rule 3: period limit
        if self.period_reply_count.get(group_id, 0) >= self.period_quota:
            return False
        
        # Rule 4: probability + dynamic adjustments
        # per-group base (注册覆盖 > 默认兜底)
        prob = self.get_group_probability(group_id)

        # Moderate length messages slightly more likely
        if 10 < content_length < 100:
            prob += 0.05

        # Active members slightly more likely
        if sender_activity > 0.8:
            prob += 0.05

        if random.random() < prob:
            self._record_reply(group_id)
            return True

        return False
    
    def _record_reply(self, group_id: int) -> None:
        """Record a reply for cooldown and period tracking."""
        self.last_reply_time[group_id] = time.time()
        self._check_period_reset()
        self.period_reply_count[group_id] = self.period_reply_count.get(group_id, 0) + 1

    def get_stats(self, group_id: int) -> dict:
        """
        Get reply statistics for a group.

        :param group_id: Group ID
        :return: Dict with period_count, period_quota, base_probability
        """
        self._check_period_reset()
        return {
            "period_count": self.period_reply_count.get(group_id, 0),
            "period_quota": self.period_quota,
            "base_probability": self.get_group_probability(group_id),
        }
