"""
roleplay 插件 - QQ群聊AI角色扮演机器人

架构：
- 本地处理（0 token）：消息存储、词频统计、话题匹配、性格特征提取
- AI辅助（低token）：话题摘要、性格标签、角色扮演回复
- 新架构（v4）：「世界视角」prompt —— user role 不再伪装成群友，
  LLM 自己决定是否插话、说什么。
- 4 层缓存（v5）：L0 静态 / L1 few-shot / L2 慢变 per-user / L3 快变
"""

import asyncio
import json
import os
import re
import time
from pathlib import Path

from nonebot import on_message, get_driver, require, get_bot
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent, PrivateMessageEvent, Bot, MessageSegment
)
from nonebot.rule import to_me
from nonebot.log import logger

from .config import BotConfig
from . import database as db
from .ai_client import AIClient
from .reply_decider import ReplyDecider
from .message_processor import (
    extract_text_from_message,
    process_group_message,
    resolve_display_name,
)
from .topic_tracker import (
    process_unprocessed_for_topics,
    generate_topic_summary,
)
from .personality_engine import (
    batch_update_personalities,
    update_member_personality,
)
from . import consciousness as cs
from .consciousness import (
    ConsciousState,
    is_consciousness_enabled,
    run_memory_decay,
    run_reflection,
)
from .character_runtime import build_runtime_messages, build_private_runtime_messages


# ─── 消息分段发送工具 ────────────────────────────
def split_message(text: str, max_length: int = 30) -> list[str]:
    if not text or not text.strip():
        return []
    text = text.strip()
    raw_parts = re.split(r'(?<=[。？！\?\!\n])', text)
    result = []
    for part in raw_parts:
        part = part.strip()
        if not part:
            continue
        if len(part) > max_length:
            sub_parts = re.split(r'(?<=[，、,])', part)
            for sub in sub_parts:
                sub = sub.strip()
                if sub:
                    result.append(sub)
        else:
            result.append(part)
    return result


def split_reply(text: str, max_length: int = 30) -> list[str]:
    parts = split_message(text, max_length)
    return [p for p in parts if p and re.sub(r'[，。？！,\.?!、\s]', '', p)]


# ─── 初始化 ───────────────────────────────────
config = BotConfig()

# ─── API key 优先级：环境变量 > YAML ───
# 推荐使用环境变量 DEEPSEEK_API_KEY（详见 .env.example），避免 key 进入版本控制。
# 留空时回退到 bot.yaml 中的 ai.api_key 字段（仅供本地调试）。
_api_key_from_yaml = config.ai.get("api_key", "")
api_key = os.getenv("DEEPSEEK_API_KEY") or _api_key_from_yaml
if not api_key:
    logger.warning(
        "[AIClient] 未检测到 DEEPSEEK_API_KEY 环境变量，"
        "也未在 config/bot.yaml 中配置 ai.api_key，调用将失败。"
    )

ai_client = AIClient(
    api_key=api_key,
    api_base=config.ai.get("api_base", "https://api.deepseek.com"),
    model=config.ai.get("model", "deepseek-v4-flash"),
    max_tokens=config.ai.get("max_tokens", 500),
    temperature=config.ai.get("temperature", 0.7),
    reply_budget=config.ai.get("reply_budget", 30000),
    learning_budget=config.ai.get("learning_budget", 10000),
    # DeepSeek 思考模式（读取 ai.thinking.* 子表）
    thinking_enabled=config.ai.get("thinking", {}).get("enabled", False),
    reasoning_effort=config.ai.get("thinking", {}).get("reasoning_effort", "high"),
    thinking_max_tokens=config.ai.get("thinking", {}).get("max_tokens", 2000),
    strip_reasoning_from_log=config.ai.get("thinking", {}).get("strip_reasoning_from_log", False),
)

reply_decider = ReplyDecider(config.character.get("group_behavior", {}))
# 启动时即注入分群配置（groups.<group_id>.reply_probability 等）
# 避免热重载前第一批消息用兜底概率
try:
    reply_decider.apply_groups_config(config.groups)
    if reply_decider._group_probability:
        logger.info(
            f"[ReplyDecider] 启动时注册分群概率覆盖: "
            f"{reply_decider._group_probability}"
        )
except Exception as e:
    logger.error(f"[ReplyDecider] 启动注入分群配置失败: {e}")
conscious_state = ConsciousState(ai_client=ai_client) if is_consciousness_enabled() else None

private_history: dict[int, list[dict]] = {}
MAX_PRIVATE_HISTORY = 10
PRIVATE_GROUP_ID = -999  # 私聊场景的虚拟群号

# 屏蔽的机器人账号（QQ号集合），这些账号的消息不处理、不记录。
# 默认空集；如部署在同一群存在其他机器人，请通过环境变量 QQBOT_BOT_ACCOUNT_IDS
# 或在 config/bot.yaml 的 bot_account_ids 字段中按需添加。
_bot_ids_env = os.getenv("QQBOT_BOT_ACCOUNT_IDS", "")
BOT_ACCOUNT_IDS: set[int] = set(
    int(x) for x in _bot_ids_env.split(",") if x.strip().isdigit()
)

_scheduled_registered = False


# ─── 数据库初始化 ──────────────────────────────
driver = get_driver()


@driver.on_startup
async def on_startup():
    await db.init_db()
    logger.info("[Roleplay] 数据库初始化完成")
    days = config.learning.get("message_retention_days", 7)
    cleaned = await db.cleanup_old_messages(days)
    if cleaned:
        logger.info(f"[Roleplay] 清理了 {cleaned} 条过期消息")
    _register_scheduled_tasks()


async def _safe_conscious_on_message(state, gid, uid, nick, content, raw_ctx):
    try:
        await state.on_message(gid, uid, nick, content, raw_context=raw_ctx)
    except Exception as e:
        logger.error(f"[Consciousness.on_message] {e}")


# ─── 定时任务 ──────────────────────────────────
def _register_scheduled_tasks():
    global _scheduled_registered
    if _scheduled_registered:
        return
    try:
        scheduler = require("nonebot_plugin_apscheduler").scheduler
    except Exception:
        logger.warning("[Roleplay] APScheduler不可用，跳过定时任务")
        return

    topic_interval = config.learning.get("topic_update_interval", 1800)
    personality_interval = config.learning.get("personality_update_interval", 3600)

    @scheduler.scheduled_job("interval", seconds=topic_interval, id="topic_update")
    async def scheduled_topic_update():
        try:
            group_ids = await _get_active_group_ids()
            for gid in group_ids:
                result = await process_unprocessed_for_topics(gid)
                if result["processed"] > 0:
                    summary = await generate_topic_summary(gid, ai_client)
                    logger.info(f"[Topic] 群{gid}: 处理{result['processed']}条, 话题摘要更新")
        except Exception as e:
            logger.error(f"[Topic Update Error] {e}")

    @scheduler.scheduled_job("interval", seconds=personality_interval, id="personality_update")
    async def scheduled_personality_update():
        try:
            group_ids = await _get_active_group_ids()
            for gid in group_ids:
                updated = await batch_update_personalities(gid, ai_client, max_users=5)
                if updated:
                    logger.info(f"[Personality] 群{gid}: 更新{updated}个成员性格")
        except Exception as e:
            logger.error(f"[Personality Update Error] {e}")

    @scheduler.scheduled_job("cron", hour=3, minute=0, id="daily_cleanup")
    async def scheduled_cleanup():
        try:
            days = config.learning.get("message_retention_days", 7)
            cleaned = await db.cleanup_old_messages(days)
            logger.info(f"[Cleanup] 清理了 {cleaned} 条过期消息")
            ai_client.budget.used_today = 0
            logger.info("[Cleanup] Token预算已重置")
        except Exception as e:
            logger.error(f"[Cleanup Error] {e}")

    refresh_hours = reply_decider.refresh_times
    for idx, hr in enumerate(refresh_hours):
        @scheduler.scheduled_job("cron", hour=hr, minute=0, id=f"quota_refresh_{hr}h")
        async def _refresh_quota(hour=hr, period_idx=idx):
            try:
                reply_decider.reset_period_counts()
                next_hr = refresh_hours[(period_idx + 1) % len(refresh_hours)]
                logger.info(
                    f"[Quota] {hour:02d}:00 回复配额已刷新，"
                    f"当前时段配额 {reply_decider.period_quota} 条，"
                    f"下次刷新 {next_hr:02d}:00"
                )
            except Exception as e:
                logger.error(f"[Quota Refresh Error] {e}")

    if is_consciousness_enabled():
        decay_cfg = config.consciousness.get("memory_decay", {})
        if decay_cfg.get("enabled", True):
            interval_h = decay_cfg.get("interval_hours", 6)
            @scheduler.scheduled_job("interval", hours=interval_h, id="conscious_decay")
            async def _conscious_decay():
                group_ids = await _get_active_group_ids()
                total = {"scanned": 0, "archived": 0, "updated": 0}
                for gid in group_ids:
                    try:
                        r = await run_memory_decay(gid)
                        for k in total:
                            total[k] += r.get(k, 0)
                    except Exception as e:
                        logger.error(f"[ConsciousDecay Error] g={gid}: {e}")
                logger.info(f"[Decay] 总计: {total}")

        refl_cfg = config.consciousness.get("reflection", {})
        if refl_cfg.get("enabled", True):
            @scheduler.scheduled_job("cron", hour=4, minute=0, id="conscious_reflection")
            async def _conscious_reflection():
                group_ids = await _get_active_group_ids()
                for gid in group_ids:
                    try:
                        await run_reflection(gid, ai_client)
                    except Exception as e:
                        logger.error(f"[ConsciousReflection Error] g={gid}: {e}")
                logger.info(f"[Reflection] 完成 {len(group_ids)} 个群的复盘")

        pro_cfg = config.consciousness.get("proactive", {})
        if pro_cfg.get("enabled", True):
            @scheduler.scheduled_job("interval", minutes=30, id="conscious_proactive")
            async def _conscious_proactive():
                if not conscious_state:
                    return
                try:
                    bots = get_driver().bots
                    bot = next(iter(bots.values())) if bots else None
                except Exception:
                    bot = None
                if not bot:
                    return
                group_ids = await _get_active_group_ids()
                for gid in group_ids:
                    try:
                        msg = await conscious_state.maybe_proactive(gid, bot=bot)
                        if msg:
                            try:
                                await bot.send_group_msg(group_id=gid, message=msg)
                                # 同步落库：让「我最近主动说过的话」能查到（用于风格去重）
                                try:
                                    await db.save_bot_message(
                                        group_id=gid, content=msg, trigger_user_id=0
                                    )
                                except Exception as save_err:
                                    logger.error(f"[Proactive Save Error] g={gid}: {save_err}")
                                logger.info(f"[Proactive] 群{gid}: {msg[:30]} (已发送)")
                            except Exception as send_err:
                                logger.error(f"[Proactive Send Error] g={gid}: {send_err}")
                    except Exception as e:
                        logger.error(f"[ConsciousProactive Error] g={gid}: {e}")

    _scheduled_registered = True
    logger.info("[Roleplay] 定时任务已注册")


async def _get_active_group_ids() -> list[int]:
    import aiosqlite
    from .database import DB_PATH
    try:
        async with aiosqlite.connect(str(DB_PATH)) as conn:
            cursor = await conn.execute(
                "SELECT DISTINCT group_id FROM messages WHERE created_at > datetime('now', '-1 day') AND group_id > 0"
            )
            rows = await cursor.fetchall()
            return [r[0] for r in rows]
    except Exception:
        return []


# ─── LLM 输出解析 ──────────────────────────────
def _parse_llm_decision(raw: str) -> tuple[str, str]:
    if not raw:
        return "SKIP", "LLM 无输出"
    text = raw.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text).strip()
    upper = text.upper()
    if upper in ("SKIP", "SKIP.", "SKIP。", "SKIP！", "SKIP!"):
        return "SKIP", "LLM 决定不插话"
    m = re.match(r"^SKIP[\s:：。.!！]*\s*(.*)$", text, re.IGNORECASE | re.DOTALL)
    if m:
        rest = m.group(1).strip()
        return "SKIP", rest or "LLM 决定不插话"
    return "SEND", text


# ─── 群聊消息处理 ──────────────────────────────
group_matcher = on_message(priority=10, block=False)


@group_matcher.handle()
async def handle_group_message(bot: Bot, event: GroupMessageEvent):
    """处理所有群聊消息（新架构：世界视角 prompt + LLM 自决策）"""
    group_id = event.group_id
    user_id = event.user_id

    # ── 屏蔽机器人账号 ──
    if user_id in BOT_ACCOUNT_IDS:
        logger.debug(f"[BotFilter] 屏蔽机器人消息: user_id={user_id}")
        return

    try:
        from plugins.group_admin.database import is_bot_enabled
        if not await is_bot_enabled(group_id):
            return
    except Exception:
        pass

    content = await extract_text_from_message(event.message, group_id=group_id, bot=bot)

    if not content:
        return
    if re.match(r'^(\[.*?\])+$', content):
        return

    # 解析"显示名"：群备注优先（OneBot 实时群昵称 → DB 备注 → 群友(uid) 兜底）
    # 这样 LLM 看到的、DB 里存的、日志里打的都是同一个名字（群备注）
    nickname = await resolve_display_name(
        group_id, user_id, sender_nickname=event.sender.nickname
    )

    # ── 层级1：本地处理（0 token）──
    analysis = await process_group_message(
        group_id, user_id, event.message_id, content, nickname
    )

    group_config = config.get_group_config(group_id)
    if not group_config.get("enabled", True):
        return

    # ── 意识层事件抽取 ──
    if conscious_state:
        try:
            recent_msgs = await db.get_recent_messages(group_id, limit=5)
            other_msgs = [m for m in recent_msgs if m.get("user_id") != bot.self_id]
        except Exception:
            other_msgs = []
        asyncio.create_task(_safe_conscious_on_message(
            conscious_state, group_id, user_id, nickname, content, other_msgs[-2:]
        ))

    # 检测 @ 我（两层检测，覆盖 NapCat 上游偶发把 @ 段吞成纯文本的情况）
    #   层 1：标准 segment 解析（OneBot 协议上报的 [at:qq=...]）
    #   层 2：纯文本兜底（@<昵称> / @<self_id>），用于超长消息/合并转发
    #         等场景下 NapCat 把 at 段直接渲染成 "@昔涟" 字面量的情况
    message_to_check = event.message
    if hasattr(event, 'original_message') and event.original_message:
        message_to_check = event.original_message
    is_at_me = any(
        seg.type == "at" and str(seg.data.get("qq", "")) == str(bot.self_id)
        for seg in message_to_check
    )
    if not is_at_me:
        bot_nick_for_at = (config.char_name or "").strip()
        # 收集可能的纯文本来源：原始文本 + 拼接的 segment 文本
        text_candidates = []
        if hasattr(event, 'raw_message') and event.raw_message:
            text_candidates.append(event.raw_message)
        for seg in event.message:
            if seg.type == "text":
                text_candidates.append(seg.data.get("text", ""))
        joined_text = "".join(text_candidates)
        if (
            (bot_nick_for_at and f"@{bot_nick_for_at}" in joined_text)
            or f"@{bot.self_id}" in joined_text
        ):
            is_at_me = True
    logger.info(f"[AtCheck] is_at_me={is_at_me}, to_me={event.to_me}")

    # ── 决策前置：硬过滤 ──
    sender_profile = await db.get_member_profile(group_id, user_id)
    activity = 0.5
    if sender_profile:
        activity = sender_profile.get("activity_level", 0.5)

    hard_should_ask = reply_decider.should_reply(
        group_id=group_id,
        is_at_me=is_at_me,
        content_length=len(content),
        sender_activity=activity,
        user_id=user_id,
    )
    if not hard_should_ask:
        return

    # ── 软去重 ──
    # ── 软去重 1：1 分钟内已回过同一个人（针对 @ 也生效）──
    if not is_at_me:
        recent_replies = await db.get_recent_reply_to_user(
            group_id, user_id, within_seconds=60
        )
        if recent_replies:
            logger.info(f"[SkipDedup] 1 分钟内已回过 {nickname}，跳过")
            return

    # ── 软去重 2：最近 5 条回复包含过多重复模式 → 强制 SKIP ──
    # @ 时跳过此检查（用户主动 call bot，不应被限制）
    if not is_at_me:
        recent_bot = await db.get_recent_bot_messages(
            group_id, limit=5, within_seconds=600
        )
        if len(recent_bot) >= 3:
            patterns = []
            for m in recent_bot:
                text = (m.get("content") or "").strip()
                first_two = text[:2] if text else ""
                first_three = text[:3] if text else ""
                patterns.append((first_two, first_three))
            twos = [p[0] for p in patterns if p[0]]
            threes = [p[1] for p in patterns if p[1]]
            for prefix in set(twos + threes):
                if twos.count(prefix) >= 3 or threes.count(prefix) >= 3:
                    logger.info(
                        f"[SkipDedup] 最近 5 条回复以「{prefix}」开头重复"
                        f"({twos.count(prefix)} 次)，非 @ 触发跳过"
                    )
                    return

    # ── 层级3：AI 回复（世界视角 prompt）──
    try:
        bot_nick = ""
        try:
            info = await bot.get_group_member_info(
                group_id=group_id, user_id=bot.self_id, no_cache=True
            )
            bot_nick = (info or {}).get("nickname", "") or ""
        except Exception:
            bot_nick = config.char_name

        consciousness_slow = ""
        consciousness_fast = ""
        if conscious_state:
            try:
                consciousness_slow, consciousness_fast = await conscious_state.build_context_split(
                    group_id, user_id, nickname
                )
            except Exception:
                consciousness_slow, consciousness_fast = "", ""

        char_prompt = config.character_prompt
        group_char = group_config.get("character_override", "")
        extra_blocks = []
        if group_char:
            extra_blocks.append(f"在群聊中，你还需要展现{group_char}的特质。")

        messages = await build_runtime_messages(
            group_id=group_id,
            bot_self_id=bot.self_id,
            bot_nickname=bot_nick or config.char_name,
            trigger_user_id=user_id,
            trigger_nickname=nickname,
            trigger_content=content,
            is_at_me=is_at_me,
            consciousness_slow=consciousness_slow,
            consciousness_fast=consciousness_fast,
            char_name=config.char_name,
            mes_example=config.mes_example,
            extra_system_blocks=extra_blocks,
            character_prompt_override=char_prompt,
            transcript_limit=12,
            bot_history_limit=6,
        )

        reply = await ai_client.chat(
            messages, max_tokens=300, temperature=0.65,
            frequency_penalty=0.4, presence_penalty=0.3,
        )

        if not reply or not reply.strip():
            return

        decision, payload = _parse_llm_decision(reply)
        if decision == "SKIP":
            logger.info(
                f"[SelfDecision] g={group_id} <- {nickname}: {content[:20]}... "
                f"LLM 跳过: {payload[:60]}"
            )
            reply_decider._record_reply(group_id)
            return

        await db.save_bot_message(
            group_id=group_id,
            content=payload,
            trigger_user_id=user_id,
        )
        reply_decider._record_reply(group_id)

        await group_matcher.send(payload, at_sender=False)
        logger.info(
            f"[Reply] 群{group_id} <- {nickname}: {content[:20]}... -> {payload[:30]}"
        )

    except Exception as e:
        logger.error(f"[Group Reply Error] {e}")
        try:
            fallback = "诶..."
            await group_matcher.send(fallback, at_sender=False)
        except Exception:
            pass


# ─── 私聊消息处理 ──────────────────────────────
private_matcher = on_message(priority=5, block=False)


@private_matcher.handle()
async def handle_private_message(bot: Bot, event: PrivateMessageEvent):
    """处理私聊消息（升级版：写库 + L0~L3 + 意识层 + user_profiles 共享）"""
    user_id = event.user_id

    # ── 屏蔽机器人账号 ──
    if user_id in BOT_ACCOUNT_IDS:
        logger.debug(f"[BotFilter] 屏蔽机器人私聊: user_id={user_id}")
        return

    content = await extract_text_from_message(event.message, group_id=group_id, bot=bot)

    if not content:
        return

    # ── 解析显示名（私聊没有群备注，直接用 event 里的昵称）──
    nickname = event.sender.nickname or f"用户{user_id}"
    # 也尝试从 user_profiles 拿昵称
    try:
        up = await db.get_user_profile(user_id)
        if up and up.get("nickname"):
            nickname = up["nickname"]
    except Exception:
        pass

    # ── 写入私聊消息到 DB ──
    try:
        await db.save_message(
            PRIVATE_GROUP_ID, user_id, event.message_id, content, "text", nickname
        )
        # 更新共享画像
        await db.upsert_user_profile(user_id, nickname=nickname, source="private")
    except Exception as e:
        logger.error(f"[Private DB Save Error] {e}")

    # ── 意识层事件抽取（私聊场景）──
    if conscious_state:
        try:
            asyncio.create_task(_safe_conscious_on_message(
                conscious_state, PRIVATE_GROUP_ID, user_id, nickname, content, [{"user_id": user_id, "content": content}]
            ))
        except Exception as e:
            logger.error(f"[Private Conscious Error] {e}")

    # ── 保留内存 history 作为快速滚动窗口（同时也写库了）──
    if user_id not in private_history:
        private_history[user_id] = [
            {"role": "system", "content": config.character_prompt}
        ]
    private_history[user_id].append({"role": "user", "content": content})
    if len(private_history[user_id]) > MAX_PRIVATE_HISTORY + 1:
        private_history[user_id] = (
            [private_history[user_id][0]]
            + private_history[user_id][-(MAX_PRIVATE_HISTORY):]
        )

    # ── 构建 L0~L3 四层 context ──
    try:
        bot_nick = config.char_name
        try:
            bot_info = await bot.get_login_info()
            bot_nick = bot_info.get("nickname", config.char_name) or config.char_name
        except Exception:
            pass

        consciousness_slow = ""
        consciousness_fast = ""
        if conscious_state:
            try:
                consciousness_slow, consciousness_fast = await conscious_state.build_context_split(
                    PRIVATE_GROUP_ID, user_id, nickname
                )
            except Exception:
                consciousness_slow, consciousness_fast = "", ""

        messages = await build_private_runtime_messages(
            private_group_id=PRIVATE_GROUP_ID,
            bot_self_id=bot.self_id,
            bot_nickname=bot_nick or config.char_name,
            trigger_user_id=user_id,
            trigger_nickname=nickname,
            trigger_content=content,
            consciousness_slow=consciousness_slow,
            consciousness_fast=consciousness_fast,
            char_name=config.char_name,
            mes_example=config.mes_example,
            character_prompt_override=config.character_prompt,
        )

        reply = await ai_client.chat(
            messages, max_tokens=500, temperature=0.65,
            frequency_penalty=0.3, presence_penalty=0.2,
        )

        if not reply or not reply.strip():
            await private_matcher.send("……")
            return

        # 存 bot 回复到 DB
        try:
            await db.save_bot_message(
                group_id=PRIVATE_GROUP_ID,
                content=reply.strip(),
                trigger_user_id=user_id,
            )
        except Exception as e:
            logger.error(f"[Private SaveBotMsg Error] {e}")

        # 更新内存 history
        private_history[user_id].append({"role": "assistant", "content": reply})

        # 分段发送
        parts = split_reply(reply.strip(), max_length=30)
        for part in parts:
            await private_matcher.send(part)
            await asyncio.sleep(0.3)

        logger.info(
            f"[PrivateReply] <- {nickname}: {content[:20]}... -> {reply[:30]}"
        )

    except Exception as e:
        logger.error(f"[Private Reply Error] {e}")
        try:
            await private_matcher.send("……")
        except Exception:
            pass
