"""
consciousness.py - 角色意识层

这是「持续运行、有自我感知」扮演系统的核心。包含 5 个子模块：

1. event_extractor    - 事件提取器（消息 → 结构化事件）
2. episodic_writer    - 情景记忆写入器（事件 → 数据库 + 印象演化 + 关系更新）
3. memory_decay       - 三维加权遗忘
4. reflection_engine  - 复盘引擎（每日总结）
5. proactive_engine   - 主动发起器（角色主动说话）

所有模块共用一个 ConsciousState 入口对象，方便业务层（__init__.py）调用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Optional

from . import database as db

# 关键：必须用 nonebot.log.logger 而非 logging.getLogger(__name__)，
# 否则子 logger 会被 nonebot 默认设到 WARNING，logger.info() 全部被过滤。
# 与 plugins/roleplay/__init__.py:21 保持一致。
# 注意：必须在 config import 之前定义，否则下面 try/except 里的 logger.warning() 会 NameError
try:
    from nonebot.log import logger
except Exception:  # 测试环境 fallback
    logger = logging.getLogger(__name__)

try:
    from .config import BotConfig
except Exception:  # 单独 import 时（如测试）bot_config 不可用
    BotConfig = None
    logger.warning("[consciousness] config 模块加载失败，将使用空配置")


# ══════════════════════════════════════════════
#  公共工具
# ══════════════════════════════════════════════

_config = BotConfig() if BotConfig else None
_consciousness_cfg = (_config.consciousness if _config else {}) or {}


def is_consciousness_enabled() -> bool:
    """意识层总开关"""
    return bool(_consciousness_cfg.get("enabled", False))


def get_subconfig(key: str, default: dict | None = None) -> dict:
    """读取 consciousness.<key> 子配置"""
    return _consciousness_cfg.get(key, default or {})


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ══════════════════════════════════════════════
#  1. 事件提取器
# ══════════════════════════════════════════════

# 提取冷却记录：{group_id: {user_id: last_extract_ts}}
_extract_cooldown: dict[int, dict[int, float]] = defaultdict(dict)


async def should_extract_event(
    group_id: int,
    user_id: int,
    content: str,
) -> bool:
    """
    判断是否值得对这条消息调用 AI 做事件提取。
    规则：
    1. 意识层关闭 → False
    2. 消息太短 → False
    3. 同一用户冷却中 → False
    4. 包含触发关键词 → True
    5. 默认 1/5 概率 → True
    """
    cfg = get_subconfig("event_extraction", {})
    if not cfg.get("enabled", True):
        return False

    min_len = cfg.get("min_msg_length", 5)
    if len(content.strip()) < min_len:
        return False

    # 纯图片/表情/梗图等
    if re.match(r'^[\s\[]*(图片|表情|reply|face|img)[\s\]]*$', content, re.I):
        return False

    # 冷却
    cooldown = cfg.get("cooldown_seconds", 300)
    last_ts = _extract_cooldown.get(group_id, {}).get(user_id, 0)
    if time.time() - last_ts < cooldown:
        return False

    # 触发关键词
    triggers = cfg.get("trigger_keywords", [])
    for kw in triggers:
        if kw in content:
            return True

    # 默认概率
    return random.random() < 0.2


def mark_extracted(group_id: int, user_id: int) -> None:
    """标记某用户最近已抽取过（写冷却时间戳）"""
    _extract_cooldown[group_id][user_id] = time.time()


async def extract_event(
    group_id: int,
    user_id: int,
    nickname: str,
    content: str,
    ai_client,
    raw_context: list[dict] | None = None,
) -> dict | None:
    """
    调用 AI 从一条消息中提取结构化事件。
    Returns: {"is_noteworthy": bool, "event_type": str, "summary": str,
              "valence": float, "intensity": float, "impression_delta": list}
    impression_delta 每项新增 "evidence_msg_indices": [int] 引用 ctx_lines 的序号。
    失败返回 None。
    """
    # 拉取该用户最近的情景记忆作为上下文（让 AI 不重复理解）
    recent_eps = await db.get_recent_episodes_for_user(group_id, user_id, limit=3)
    ep_lines = [
        f"- [{e['occurred_at']}] {e['summary']}"
        for e in recent_eps
    ]
    ep_context = "\n".join(ep_lines) if ep_lines else "（暂无）"

    # 给 raw_context 每条消息编号，让 LLM 在 impression_delta 里引用序号
    ctx_lines = []
    if raw_context:
        for idx, m in enumerate(raw_context[-3:]):
            nick = m.get('nickname', '?')
            text = m.get('content', '')[:60]
            ctx_lines.append(f"[{idx}] {nick}: {text}")
    ctx_str = "\n".join(ctx_lines) if ctx_lines else f"[0] {nickname}: {content[:80]}"

    # ── 改造2：写印象的 LLM 走「昔涟」人设 ──
    # 引入角色卡精简版作为 system prompt，让印象生成贴合角色视角
    system_prompt = _get_impression_system_prompt()

    prompt = f"""你正在以「{nickname or '这位群友'}」的群聊行为为依据，判断是否值得记录为结构化事件，并对 ta 形成印象。

【判定标准】
- 包含具体信息（推荐、分享、提问、观点、情感、回忆等）→ noteworthy
- 纯打招呼、复读、表情包、无意义闲聊 → not noteworthy
- 事件强度：0.0（无关紧要）~1.0（刻骨铭心）
- 情感正负向：-1.0（负向）~1.0（正向）

【{nickname}的历史记忆】
{ep_context}

【当前对话上下文】（每条前的 [序号] 用于 evidence 引用）
{ctx_str}

【印象生成要求——极其重要】
1. 印象必须基于上面【当前对话上下文】里**看得见的具体内容**，不要凭空推断
2. 每条印象都要在 evidence_msg_indices 里标注它依据的是哪几条消息（引用 [序号]）
3. 杜绝空泛模板：禁用「善于推理」「乐于助人」「对某事有信心」「喜欢自嘲」这类没具体内容的套话
4. 杜绝刻板标签：不要用「言论尖刻」「负面情绪强烈」「对身体/地域/外貌」贴标签
5. 杜绝隐私越界：不记录家庭、收入、住址、感情私生活等私人信息
6. 玩笑≠恶意：群友的吐槽/玩笑不要给负极性，用 +0.0~+0.3 表示"活泼"
7. 印象 note 要有具体内容（15 字内），如「聊《黑魂》时把死循环玩成梗」而非「善于推理」

输出严格 JSON（不要解释、不要 markdown）：
{{"is_noteworthy": true/false, "event_type": "chat/joke/help/conflict/compliment/share/gift/confess/question/other", "summary": "一句话描述事件（30字内，要有具体内容）", "valence": -1.0~1.0, "intensity": 0.0~1.0, "impression_delta": [{{"note": "对{nickname}的印象碎片（15字内，要有具体内容）", "polarity": -1.0~1.0, "confidence": 0.0~1.0, "evidence_msg_indices": [0]}}]}}

impression_delta 可以是空数组。如果 is_noteworthy=false，其余字段可忽略。"""

    try:
        reply, _tokens = await ai_client.chat_for_learning(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ]
        )
        if not reply:
            return None
        m = re.search(r'\{.*\}', reply, re.DOTALL)
        if not m:
            return None
        result = json.loads(m.group())
        if not isinstance(result, dict):
            return None
        return result
    except Exception as e:
        logger.error(f"[EventExtract] {nickname}: {e}")
        return None


# ── 改造2：印象生成走角色人设 ──
# 缓存角色卡精简版 system prompt，避免每次 extract_event 都重建
_impression_system_cache: str | None = None


def _get_impression_system_prompt() -> str:
    """
    构造印象生成专用的 system prompt（角色卡精简版 + 反刻板印象约束）。
    首次调用后缓存，后续直接复用。
    """
    global _impression_system_cache
    if _impression_system_cache is not None:
        return _impression_system_cache

    parts: list[str] = []
    # 尝试从 BotConfig 获取角色卡关键字段
    try:
        from .config import BotConfig
        cfg = BotConfig()
        name = cfg.char_name or "你"
        desc = cfg.char_description or ""
        personality = cfg.char_personality or ""
        if desc:
            parts.append(f"你是「{name}」。{desc}")
        if personality:
            parts.append(f"【你的性格】\n{personality}")
    except Exception:
        # fallback：用 consciousness 模块已有的 _config
        if _config:
            name = _config.char_name if hasattr(_config, 'char_name') else "你"
            parts.append(f"你是「{name}」，正在以自己的视角观察群友。")

    # 反刻板印象 + 反空泛约束（隔离区，确保不被角色卡覆盖）
    parts.append(
        "\n【印象生成纪律——必须遵守】\n"
        "1. 你正在观察群聊，对群友形成印象。印象是你**以角色视角**的主观感受，不是 AI 总结。\n"
        "2. 每条印象必须源于看得见的具体言行，不要凭空推断。\n"
        "3. 杜绝空泛模板：禁用「善于推理」「乐于助人」「对某事有信心」「喜欢自嘲」这类套话。"
        "要写具体的事，如「聊《黑魂》时把死循环玩成梗」。\n"
        "4. 杜绝刻板标签：不要用「言论尖刻」「负面情绪强烈」给人贴标签。\n"
        "5. 杜绝隐私越界：不记录家庭、收入、住址、感情私生活等私人信息。\n"
        "6. 玩笑≠恶意：群友的吐槽/玩笑不要给负极性。玩笑称呼听着凶其实是亲近，用 +0.0~+0.3 表示。\n"
        "7. 不确定就不写：把握不到的内容跳过，宁可少记不要瞎记。\n"
        "8. 你是温柔但不完美的朋友，不会用一个词定义一个人。\n"
    )

    _impression_system_cache = "\n\n".join(parts)
    return _impression_system_cache


# ══════════════════════════════════════════════
#  2. 情景记忆写入器
# ══════════════════════════════════════════════


async def write_episode_from_event(
    group_id: int,
    user_id: int,
    nickname: str,
    event: dict,
    raw_context: list[dict] | None = None,
) -> int | None:
    """
    把事件写入情景记忆，并级联触发印象演化和关系更新。
    Returns: 写入的 episode id，失败返回 None。
    """
    if not event.get("is_noteworthy"):
        return None

    summary = (event.get("summary") or "").strip()
    if not summary:
        return None

    event_type = event.get("event_type", "chat")
    if event_type not in {"chat", "joke", "help", "conflict", "compliment",
                          "share", "gift", "confess", "question", "other"}:
        event_type = "chat"

    try:
        valence = max(-1.0, min(1.0, float(event.get("valence", 0.0))))
        intensity = max(0.0, min(1.0, float(event.get("intensity", 0.5))))
    except (TypeError, ValueError):
        valence, intensity = 0.0, 0.5

    # 1. 写入情景记忆
    ep_id = await db.insert_episode(
        group_id=group_id,
        user_id=user_id,
        nickname=nickname,
        event_type=event_type,
        summary=summary[:200],
        raw_context=raw_context or [],
        topic_keywords=[],
        valence=valence,
        intensity=intensity,
    )
    if not ep_id:
        return None

    logger.info(
        f"[Episode] g={group_id} u={nickname} "
        f"type={event_type} v={valence:+.2f} i={intensity:.2f}: {summary}"
    )

    # 2. 演化印象笔记
    for delta in event.get("impression_delta", []):
        if not isinstance(delta, dict):
            continue
        note = (delta.get("note") or "").strip()
        if not note:
            continue
        try:
            pol = max(-1.0, min(1.0, float(delta.get("polarity", 0.0))))
            conf = max(0.0, min(1.0, float(delta.get("confidence", 0.5))))
        except (TypeError, ValueError):
            pol, conf = 0.0, 0.5
        # ── 改造1-5：从 raw_context 提取 evidence_msg_ids ──
        evidence_indices = delta.get("evidence_msg_indices") or []
        evidence_ids: list[int] = []
        if raw_context and evidence_indices:
            # raw_context 最后 3 条用于 prompt，和 ctx_lines 编号一致
            ctx_tail = raw_context[-3:]
            for idx in evidence_indices:
                try:
                    idx_int = int(idx)
                    if 0 <= idx_int < len(ctx_tail):
                        mid = ctx_tail[idx_int].get("id")
                        if mid:
                            evidence_ids.append(int(mid))
                except (TypeError, ValueError, IndexError):
                    continue
        await evolve_impression(
            group_id, user_id, note, pol, conf,
            source_event_id=ep_id,
            evidence_msg_ids=evidence_ids or None,
        )

    # 3. 更新关系（familiarity 递增，affinity 随 valence 变化）
    # 单次互动 ±0.1 比较合理（再小就感知不到变化）
    delta_aff = max(-0.1, min(0.1, valence * 0.1))
    await db.bump_interaction(group_id, user_id, delta_familiarity=0.02, delta_affinity=delta_aff)

    return ep_id


async def evolve_impression(
    group_id: int,
    user_id: int,
    note: str,
    polarity: float,
    confidence: float,
    source_event_id: int | None = None,
    evidence_msg_ids: list[int] | None = None,
) -> None:
    """
    演化印象笔记：已存在则强化/修正，否则新建。
    evidence_msg_ids 会累加到已有的 evidence_msg_ids（去重）。
    """
    note = note.strip()[:80]
    if not note:
        return

    existing = await db.find_impression(group_id, user_id, note)
    if existing:
        new_pol = existing["polarity"] * 0.7 + polarity * 0.3
        new_conf = min(1.0, existing["confidence"] * 0.9 + confidence * 0.2)
        # 累加 evidence_msg_ids（去重）
        merged_ev: list[int] = []
        try:
            old_ev = json.loads(existing.get("evidence_msg_ids") or "[]")
            if isinstance(old_ev, list):
                merged_ev = [int(x) for x in old_ev]
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        if evidence_msg_ids:
            for eid in evidence_msg_ids:
                if eid not in merged_ev:
                    merged_ev.append(eid)
        await db.update_impression(
            existing["id"],
            polarity=new_pol,
            confidence=new_conf,
            confirm_count=existing["confirm_count"] + 1,
            evidence_msg_ids=merged_ev or None,
        )
        logger.info(f"[Impression] 强化: {note} pol={new_pol:+.2f} conf={new_conf:.2f} ev={len(merged_ev)}")
    else:
        # confidence 太低就不记（避免噪声）
        if confidence < 0.4:
            return
        await db.insert_impression(
            group_id, user_id, note,
            polarity=polarity, confidence=confidence,
            source_event_id=source_event_id,
            evidence_msg_ids=evidence_msg_ids,
        )
        logger.info(f"[Impression] 新建: {note} pol={polarity:+.2f} conf={confidence:.2f} ev={len(evidence_msg_ids or [])}")


# ══════════════════════════════════════════════
#  3. 三维加权遗忘
# ══════════════════════════════════════════════


def compute_decay_score(episode: dict, now: datetime | None = None) -> float:
    """
    核心衰减公式（纯函数）。

    decay_score = base × time × emotion × frequency × relation_modifier

    各因子：
    - time: 60 天线性衰减到 0
    - emotion: 强烈事件（intensity>0.7）减半衰减；轻微事件加速衰减
    - frequency: 被引用次数多保留更久
    - relation_modifier: 关系好的人记忆更顽固
    """
    cfg = get_subconfig("memory_decay", {})
    max_age = cfg.get("max_age_days", 60)
    half_life = cfg.get("half_life_days", 30)
    forget_threshold = cfg.get("forget_threshold", 0.1)

    now = now or datetime.now()
    occurred_str = episode.get("occurred_at") or now.strftime("%Y-%m-%d %H:%M:%S")
    try:
        # 兼容带或不带微秒的格式
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                occurred = datetime.strptime(occurred_str, fmt)
                break
            except ValueError:
                continue
        else:
            occurred = now
    except Exception:
        occurred = now

    days = max(0.0, (now - occurred).total_seconds() / 86400.0)

    # 时间因子：max_age 天后衰减到 0
    time_factor = max(0.0, 1.0 - days / max_age)

    # 情感因子：强烈事件保留更好
    intensity = episode.get("intensity", 0.5)
    if intensity >= 0.7:
        emotion_factor = 0.5 + 0.5 * (1.0 - time_factor) * 0  # 强事件最不怕忘
        # 简化为：衰减更慢（emotion_factor 整体 > 0.7）
        emotion_factor = 0.7 + 0.3 * (1.0 - time_factor)
    elif intensity >= 0.4:
        emotion_factor = 0.4 + 0.4 * (1.0 - time_factor)
    else:
        emotion_factor = 0.2 + 0.3 * (1.0 - time_factor)

    # 频率因子：被引用过的加分（last_referenced_at）
    ref_str = episode.get("last_referenced_at")
    freq_bonus = 0.0
    if ref_str:
        try:
            ref_dt = datetime.strptime(ref_str, "%Y-%m-%d %H:%M:%S")
            days_since_ref = max(0.0, (now - ref_dt).total_seconds() / 86400.0)
            # 7 天内被引用过 → 加 0.3
            if days_since_ref < 7:
                freq_bonus = 0.3 * (1.0 - days_since_ref / 7.0)
        except Exception:
            pass

    frequency_factor = 0.7 + min(0.5, freq_bonus)  # 范围 [0.7, 1.2]

    # 关系修正（通过 affinity）：需要外部传入
    affinity = episode.get("_affinity", 0.0)
    if affinity > 0.7:
        relation_mod = 1.5
    elif affinity < -0.3:
        relation_mod = 1.3
    else:
        relation_mod = 1.0

    score = time_factor * emotion_factor * frequency_factor * relation_mod
    return max(0.0, min(1.0, score))


async def run_memory_decay(group_id: int | None = None) -> dict:
    """
    执行一轮记忆衰减（每 6 小时跑一次）。
    Returns: {"scanned": int, "archived": int, "updated": int}
    """
    cfg = get_subconfig("memory_decay", {})
    if not cfg.get("enabled", True):
        return {"scanned": 0, "archived": 0, "updated": 0}

    threshold = cfg.get("forget_threshold", 0.1)
    episodes = await db.get_episodes_for_decay(group_id)

    # 一次性拉取所有相关关系（按 user_id 索引）
    rel_map: dict[int, float] = {}
    if episodes:
        import aiosqlite
        async with aiosqlite.connect(str(db.DB_PATH)) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT user_id, affinity FROM relationship_state "
                "WHERE group_id = ?", (group_id,) if group_id else ()
            )
            rows = await cursor.fetchall() if cursor else []
            for r in rows:
                rel_map[r["user_id"]] = r["affinity"]

    updated = 0
    archived = 0
    now = datetime.now()
    for ep in episodes:
        ep_with_aff = dict(ep)
        ep_with_aff["_affinity"] = rel_map.get(ep["user_id"], 0.0)
        new_score = compute_decay_score(ep_with_aff, now)
        need_archive = new_score < threshold
        if abs(new_score - ep["decay_score"]) > 0.01 or need_archive != bool(ep["is_archived"]):
            await db.update_episode_decay(ep["id"], new_score, archive=need_archive)
            updated += 1
            if need_archive and not ep["is_archived"]:
                archived += 1

    logger.info(
        f"[Decay] g={group_id or 'ALL'} scanned={len(episodes)} "
        f"updated={updated} archived={archived}"
    )
    return {"scanned": len(episodes), "archived": archived, "updated": updated}


# ══════════════════════════════════════════════
#  4. 复盘引擎
# ══════════════════════════════════════════════


async def run_reflection(group_id: int, ai_client) -> bool:
    """
    每日复盘：把当天经历压缩成"近期生活总结"。
    写入 role_state，并写一条 event_type=reflection 的情景记忆。
    Returns: True if 成功生成复盘
    """
    cfg = get_subconfig("reflection", {})
    if not cfg.get("enabled", True):
        return False

    max_eps = cfg.get("max_episodes_per_reflection", 20)

    # 拉取最近 24h 的非归档情景记忆
    eps = await db.get_top_episodes(group_id, include_archived=False, top_k=max_eps)

    # 当前状态
    state = await db.get_role_state(group_id) or {}

    if not eps:
        # 没事件就只做轻微调整
        new_energy = max(0.3, state.get("energy", 0.7) - 0.1)
        await db.upsert_role_state(
            group_id,
            energy=new_energy,
            current_focus="今天群里挺安静的",
        )
        return False

    ep_lines = [f"- {e['nickname']}: {e['summary']} (类型={e['event_type']}, 强度={e['intensity']:.1f})"
                for e in eps]
    ep_text = "\n".join(ep_lines)

    prompt = f"""你是「角色复盘助手」。请基于以下群聊今日事件，生成角色的心理状态总结。

【当前状态】
心情：{state.get('mood', 'neutral')}（强度 {state.get('mood_intensity', 0.5):.1f}）
精力：{state.get('energy', 0.7):.1f}
最近关注：{state.get('current_focus', '无')}

【今日事件】（共 {len(eps)} 条）
{ep_text}

输出严格 JSON（不要解释）：
{{
  "mood": "happy/sad/curious/irritated/peaceful/neutral",
  "mood_intensity": 0.0~1.0,
  "energy": 0.0~1.0,
  "current_focus": "最近在想什么（20字内）",
  "reflection": "今天的整体感受（40字内，自然口吻）",
  "relationship_notes": [{{"nickname": "昵称", "note": "一句话关系备注"}}]
}}"""

    try:
        reply, _ = await ai_client.chat_for_learning(
            [{"role": "user", "content": prompt}]
        )
        if not reply:
            return False
        m = re.search(r'\{.*\}', reply, re.DOTALL)
        if not m:
            return False
        result = json.loads(m.group())
    except Exception as e:
        logger.error(f"[Reflection] g={group_id}: {e}")
        return False

    # 写入 role_state（同时打 focus_at 时间戳，用于后续软过期）
    await db.upsert_role_state(
        group_id,
        mood=result.get("mood", state.get("mood", "neutral")),
        mood_intensity=float(result.get("mood_intensity", state.get("mood_intensity", 0.5))),
        energy=float(result.get("energy", state.get("energy", 0.7))),
        current_focus=result.get("current_focus", "")[:80],
        last_reflection=result.get("reflection", "")[:300],
        day_count=(state.get("day_count") or 0) + 1,
        focus_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    # 写一条 reflection 情景记忆
    await db.insert_episode(
        group_id=group_id,
        user_id=0,
        nickname="(内心独白)",
        event_type="reflection",
        summary=f"复盘：{result.get('reflection', '')[:150]}",
        raw_context=[],
        valence=0.0,
        intensity=0.3,
    )

    # 更新关系备注
    for rn in result.get("relationship_notes", []) or []:
        if not isinstance(rn, dict):
            continue
        nick = rn.get("nickname", "").strip()
        note = (rn.get("note") or "").strip()[:80]
        if not nick or not note:
            continue
        # 通过 nickname 反查 user_id（仅在本群）
        import aiosqlite
        async with aiosqlite.connect(str(db.DB_PATH)) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT user_id FROM member_profiles WHERE group_id = ? AND nickname = ? LIMIT 1",
                (group_id, nick),
            )
            row = await cursor.fetchone()
            if row:
                await db.upsert_relationship(group_id, row["user_id"], note=note)

    logger.info(f"[Reflection] g={group_id}: {result.get('current_focus', '')[:30]}")
    return True


# ══════════════════════════════════════════════
#  5. 主动发起器
# ══════════════════════════════════════════════


async def maybe_proactive(
    group_id: int,
    ai_client,
    bot,
) -> str | None:
    """
    每 30 分钟调用一次。决定是否主动说话，并返回要发送的消息文本。
    返回 None 表示不主动说。
    """
    cfg = get_subconfig("proactive", {})
    if not cfg.get("enabled", True):
        return None

    state = await db.get_role_state(group_id)
    if not state:
        return None

    now = datetime.now()
    hour = now.hour
    quiet_hours = cfg.get("quiet_hours", [0, 7])
    if quiet_hours and quiet_hours[0] <= hour < quiet_hours[1]:
        return None  # 深夜静默

    # 每日上限
    if state.get("proactive_count_today", 0) >= cfg.get("daily_max", 20):
        return None

    # 距上次主动
    last_p = state.get("last_proactive_at")
    min_interval = cfg.get("min_interval_minutes", 10)
    if last_p:
        try:
            last_dt = datetime.strptime(last_p, "%Y-%m-%d %H:%M:%S")
            if (now - last_dt).total_seconds() < min_interval * 60:
                return None
        except Exception:
            pass

    # 计算触发概率
    base_prob = cfg.get("base_probability", 0.05)
    mood = state.get("mood", "neutral")
    energy = state.get("energy", 0.7)

    if mood in ("happy", "curious"):
        base_prob *= 1.5
    elif mood in ("sad", "irritated"):
        base_prob *= 0.5
    if energy < 0.3:
        base_prob *= 0.3

    if random.random() > base_prob:
        return None

    # 决定主动说什么
    message = await _generate_proactive_message(group_id, state, ai_client)
    if not message:
        return None

    # 计数 +1
    await db.increment_proactive_count(group_id)
    return message


async def _generate_proactive_message(
    group_id: int,
    state: dict,
    ai_client,
) -> str | None:
    """生成主动发言的文本（基于当前状态和群情况）"""
    # 场景 1：群里冷清（很久没人说话）
    # 注：get_recent_messages 是 DESC（新→旧），所以 [0] 是最新、[-1] 是最旧
    # 之前误用 [-1] 会把"3 条里最旧的一条"当作最后发言时间，
    # 导致群里明明刚刷屏，proactive 仍然认为"群冷清" → 误触发"早安"问候。
    idle_threshold_hours = 2  # 配置可改
    recent_msgs = await db.get_recent_messages(group_id, limit=3)
    if not recent_msgs:
        scenario = "group_idle"
    else:
        try:
            last_ts = datetime.strptime(recent_msgs[0]["created_at"], "%Y-%m-%d %H:%M:%S")
            idle_hours = (datetime.now() - last_ts).total_seconds() / 3600
        except Exception:
            idle_hours = 0
        if idle_hours >= idle_threshold_hours:
            scenario = "group_idle"
        else:
            # 场景 2：某个熟人很久没出现
            distant = await db.get_distant_friends(group_id, threshold_hours=24)
            if distant:
                scenario = f"miss_friend:{distant[0].get('nickname', '某人')}"
            else:
                scenario = "casual"

    focus = state.get("current_focus", "")
    mood = state.get("mood", "neutral")
    energy = state.get("energy", 0.7)
    last_ref = state.get("last_reflection", "")
    focus_at_str = state.get("focus_at", "")

    # ── A 修复：focus/reflection 软过期 ──
    # 凌晨的复盘会污染一整天的 proactive。
    # 如果 focus_at 距今 > FOCUS_TTL_HOURS 小时，就当作"没 focus"用。
    FOCUS_TTL_HOURS = 4
    focus_expired = True
    if focus_at_str:
        try:
            focus_dt = datetime.strptime(focus_at_str, "%Y-%m-%d %H:%M:%S")
            age_hours = (datetime.now() - focus_dt).total_seconds() / 3600
            focus_expired = age_hours > FOCUS_TTL_HOURS
        except Exception:
            focus_expired = True
    # 兼容老库（focus_at 为空）：当作"过期"以立刻解套当前污染
    if not focus_at_str:
        focus_expired = True

    if focus_expired:
        focus_for_prompt = ""
        last_ref_for_prompt = ""
    else:
        focus_for_prompt = focus
        last_ref_for_prompt = last_ref

    # ── C 修复：近 N 条 proactive 风格去重 ──
    # 读出最近 6 条自己主动说的，避免 LLM 又复刻同一句式
    RECENT_PROACTIVE_LIMIT = 6
    try:
        recent_proactive = await db.get_recent_bot_messages(
            group_id, limit=RECENT_PROACTIVE_LIMIT, within_seconds=24 * 3600
        )
    except Exception:
        recent_proactive = []
    dedup_lines = []
    for m in recent_proactive:
        c = (m.get("content") or "").strip()
        if c:
            dedup_lines.append(f"- {c[:30]}")
    dedup_block = ""
    if dedup_lines:
        dedup_block = (
            "\n【我最近主动说过的话】（不要再用相同/相近的话题或句式）\n"
            + "\n".join(dedup_lines)
        )

    prompt = f"""你是角色，要主动在群里说一句话（不能是被动的回复）。

【场景】{scenario}
【你的状态】心情 {mood}，精力 {energy:.1f}
【最近在想】{focus_for_prompt or '（无）'}
【复盘】{last_ref_for_prompt or '（无）'}
{dedup_block}

要求：
- 1-2 句话，不超过 30 字
- 像朋友间的随口一句，不要刻意
- 不要提"我是 AI"、"作为角色"等元话题
- 不要 @ 别人
- 如果是 miss_friend 场景：自然地想念一下 ta（不要解释为什么）
- 严禁复述【最近在想】或【复盘】里的字句
- 严禁使用与【我最近主动说过的话】相同/相近的话题或句式
- 只输出要说的话，不要其他内容。"""

    try:
        reply, _ = await ai_client.chat_for_learning(
            [{"role": "user", "content": prompt}]
        )
        if reply:
            return reply.strip()[:60]
    except Exception as e:
        logger.error(f"[Proactive Gen] g={group_id}: {e}")
    return None


# ══════════════════════════════════════════════
#  6. 意识上下文构建（注入到 prompt）
# ══════════════════════════════════════════════


async def build_consciousness_context_slow(
    group_id: int,
    current_user_id: int,
    current_nickname: str = "",
) -> str:
    """
    意识层「慢变」上下文（按 user 维度，几小时~几天才变一次）。

    包含：
    - 你对这个群友的印象笔记
    - 你和这个群友的关系
    - 你和这个群友之间的关键情景记忆
    - 这个群友的画像

    适合放在 user 消息的「长期记忆区」，不同群友之间 KV cache 互不污染，
    但同一个群友短时间内这部分内容完全相同 → 高命中率。
    """
    parts: list[str] = []

    # 1. 对当前发言者的认识
    impressions = await db.get_user_impressions(group_id, current_user_id, limit=5)
    if impressions:
        notes = [f"「{i['note']}」（{i['polarity']:+.1f}）" for i in impressions]
        parts.append(
            f"【你对{current_nickname or 'ta'}的印象】" + "；".join(notes)
        )

    # 2. 关系定位
    rel = await db.get_relationship(group_id, current_user_id)
    if rel and rel.get("interaction_count", 0) > 0:
        stage_zh = {
            "stranger": "陌生人", "acquaintance": "认识",
            "familiar": "熟悉", "intimate": "亲近", "distant": "疏远"
        }.get(rel.get("stage", "stranger"), "认识")
        affinity_str = f"{rel['affinity']:+.1f}"
        parts.append(
            f"【你和{current_nickname or 'ta'}的关系】{stage_zh}，"
            f"见过{rel.get('interaction_count', 0)}次，"
            f"好感{affinity_str}。{rel.get('note', '')}"
        )

    # 3. 关键情景记忆
    episodes = await db.get_top_episodes(group_id, current_user_id, top_k=3)
    if episodes:
        ep_lines = [f"- {ep['summary']}（{ep['occurred_at']}）" for ep in episodes]
        parts.append(
            f"【你和{current_nickname or 'ta'}之间的事】\n" + "\n".join(ep_lines)
        )

    return "\n".join(parts)


async def build_consciousness_context_fast(
    group_id: int,
) -> str:
    """
    意识层「快变」上下文（角色级，分钟级变化）。

    包含：
    - 你现在的心情/精力/最近在想什么
    - 你最近的复盘总结

    适合放在 user 消息的「当前时刻区」。
    """
    parts: list[str] = []

    state = await db.get_role_state(group_id)
    if state:
        mood_zh = {
            "happy": "开心", "sad": "低落", "curious": "好奇",
            "irritated": "烦躁", "peaceful": "平静", "neutral": "一般"
        }.get(state.get("mood", "neutral"), "一般")
        parts.append(
            f"【你现在的状态】心情：{mood_zh}（强度{state.get('mood_intensity', 0.5):.1f}），"
            f"精神：{state.get('energy', 0.7):.1f}，"
            f"最近在想：{state.get('current_focus') or '没特别想什么'}"
        )

    if state and state.get("last_reflection"):
        parts.append(f"【你最近的想法】{state['last_reflection']}")

    return "\n".join(parts)


async def build_consciousness_context(
    group_id: int,
    current_user_id: int,
    current_nickname: str = "",
) -> str:
    """
    旧版接口：合并返回快变+慢变意识上下文。
    保留用于向后兼容；新代码请直接用 _slow / _fast 两个版本。
    """
    slow, fast = await asyncio.gather(
        build_consciousness_context_slow(group_id, current_user_id, current_nickname),
        build_consciousness_context_fast(group_id),
    )
    return "\n\n".join(p for p in (slow, fast) if p)


# ══════════════════════════════════════════════
#  入口对象（业务层使用）
# ══════════════════════════════════════════════


class ConsciousState:
    """角色意识层入口，方便业务层统一调用"""

    def __init__(self, ai_client=None):
        self.ai_client = ai_client

    async def on_message(self, group_id: int, user_id: int, nickname: str,
                         content: str, raw_context: list[dict] | None = None) -> int | None:
        """
        每条群消息进来时调用：节流 + 提取 + 写入。
        Returns: 写入的 episode id（如果成功），否则 None
        """
        if not is_consciousness_enabled():
            return None
        if not self.ai_client:
            return None
        if not await should_extract_event(group_id, user_id, content):
            return None

        mark_extracted(group_id, user_id)

        event = await extract_event(
            group_id, user_id, nickname, content,
            self.ai_client, raw_context,
        )
        if not event or not event.get("is_noteworthy"):
            return None

        ep_id = await write_episode_from_event(
            group_id, user_id, nickname, event, raw_context,
        )
        return ep_id

    async def build_context(self, group_id: int, current_user_id: int,
                            current_nickname: str = "") -> str:
        """旧版接口：合并快变+慢变意识上下文（向后兼容）"""
        if not is_consciousness_enabled():
            return ""
        return await build_consciousness_context(group_id, current_user_id, current_nickname)

    async def build_context_split(self, group_id: int, current_user_id: int,
                                   current_nickname: str = "") -> tuple[str, str]:
        """
        新版接口：返回 (慢变上下文, 快变上下文) 两个独立字符串。
        慢变 → 适合放在 user 段「长期记忆区」，KV cache 高命中
        快变 → 适合放在 user 段「当前时刻区」
        """
        if not is_consciousness_enabled():
            return "", ""
        slow, fast = await asyncio.gather(
            build_consciousness_context_slow(group_id, current_user_id, current_nickname),
            build_consciousness_context_fast(group_id),
        )
        return slow, fast

    async def maybe_proactive(self, group_id: int, bot) -> str | None:
        """尝试主动说话，返回要发的文本（或 None）"""
        if not is_consciousness_enabled():
            return None
        return await maybe_proactive(group_id, self.ai_client, bot)
