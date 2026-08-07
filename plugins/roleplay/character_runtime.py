"""
character_runtime.py - 角色运行时的"世界视角"prompt 组装 + KV cache 优化

核心思想：
- 旧架构：user role = 群友（导致 LLM 把群友的发言当成"用户对我说的话"，
  进而产生"你说了两遍呢"这种凭空指证）。
- 新架构：user role = "群聊世界/旁白"，LLM 收到的不是"一个人对我说话"，
  而是"整个世界在运转，灵魂快照附在 system prompt 里"。

【缓存分层】（越往下越容易变）
L0 静态 system：角色卡 + 反幻觉护栏 + 群聊场景微调
L1 半静态 system：few-shot 示例
L2 慢变 user：当前群友的印象/关系/情景记忆（按 user 维度独立）
L3 快变 user：角色状态 + 群聊最近消息 + 我最近说过的 + 当前触发消息

效果：
- L0+L1 在同一次会话内完全不变 → DeepSeek KV cache 100% 命中
- L2 按 user 分桶：同一个 user 在印象/关系未变时高命中
- L3 才是真正每次都变的部分，只占 prompt 30~40%
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from . import database as db
from .config import BotConfig

_config = BotConfig()


async def _async_get_topic_hint(group_id: int) -> str:
    topics = await db.get_active_topics(group_id)
    if not topics:
        return ""
    parts = []
    for t in topics[:3]:
        name = t.get("topic_name", "")
        summary = t.get("summary", "")
        if summary:
            parts.append(f"{name}: {summary[:40]}")
        elif name:
            parts.append(name)
    return " / ".join(parts)


async def _async_get_sender_profile_hint(group_id: int, user_id: int) -> str:
    profile = await db.get_member_profile(group_id, user_id)
    if not profile:
        return ""
    tags = profile.get("personality_tags") or "[]"
    try:
        tag_list = json.loads(tags) if isinstance(tags, str) else tags
    except Exception:
        tag_list = []
    if not tag_list:
        return ""
    return "、".join(tag_list[:3])


def _build_l0_system(
    char_prompt: str,
    bot_nickname: str,
    extra_blocks: list[str] | None = None,
    scene_type: str = "group",
) -> str:
    """
    L0 静态 system：配置改了才变。同一个 bot 实例下完全固定。

    Parameters
    ----------
    scene_type : str
        "group" - 群聊场景（默认）
        "private" - 私聊场景
    """
    if scene_type == "private":
        scene_anchor = (
            f"\n【当前场景】你现在正在和一位朋友进行 QQ 私聊。"
            f"你的名字是「{bot_nickname}」。这是一对一的私密对话，"
            f"你像朋友一样自然地聊天，放松、真诚、有性格。"
        )
        anti_hallucination = (
            "\n【反幻觉护栏 - 极其重要】\n"
            "- 下面「我最近说过的话」和「聊天历史」是你拥有事实的全部来源。\n"
            "- 如果一条消息在窗口里没出现过，绝不能说'你刚才说过'、'你说了两遍'、'你上次提过'。\n"
            "- 看到梗图（窗口里会显示为 [图片]）时，老实承认看不清，不要瞎猜内容。\n"
            "- 不要复述对方的话，不要总结，不要解释。\n"
            "- 你每次只能输出一段纯文本回复，绝不能拆分多条消息。"
        )
    else:
        scene_anchor = (
            f"\n【当前场景】你现在正在一个 QQ 群里潜水/聊天的某一刻。"
            f"群昵称是「{bot_nickname}」。你是一名群友，"
            f"像朋友一样自然地存在，而不是在'被用户单独聊天'。"
        )
        anti_hallucination = (
            "\n【反幻觉护栏 - 极其重要】\n"
            "- 下面「我最近说过的话」和「群聊最近消息」是你拥有事实的全部来源。\n"
            "- 如果一条消息在窗口里没出现过，绝不能说'你刚才说过'、'你说了两遍'、'你上次提过'。\n"
            "- 群友之间互相说的话不是'用户对你说的'，你不能假装自己是那段对话的参与者。\n"
            "- 看到梗图（窗口里会显示为 [图片]）时，老实承认看不清，不要瞎猜内容。\n"
            "- 如果你不确定该回什么，输出 SKIP 让系统跳过本次发言；不要硬凑。\n"
            "- 你每次只能输出一段纯文本回复，绝不能拆分多条消息。\n"
            "- 「这人是X」「他/她/这个B」等带 @某人 的句子里，主语是紧跟的 @对象，"
            "不是机器人。如果一条消息同时 @ 了你和别人，那个人@是在喊别人、不是骂你。"
        )

    soul = char_prompt + scene_anchor + anti_hallucination
    if extra_blocks:
        for blk in extra_blocks:
            if blk:
                soul += "\n\n" + blk
    return soul


async def _build_l2_user_long_term(
    group_id: int,
    current_user_id: int,
    current_nickname: str,
    consciousness_slow: str = "",
) -> str:
    """
    L2 慢变 user 段：按 user 维度独立，几小时~几天才变一次。
    同一群友短时间内这部分完全相同 → 高缓存命中。

    包含：
    - 触发者性格画像（personality_tags）
    - 意识层 per-user 慢变：印象/关系/情景记忆
    """
    parts: list[str] = []

    # 1. 触发者画像（性格标签）
    sender_hint = await _async_get_sender_profile_hint(group_id, current_user_id)
    if sender_hint:
        parts.append(f"【长期记忆：当前这位群友的性格画像】{sender_hint}")

    # 2. 意识层 per-user 慢变（印象/关系/情景）
    if consciousness_slow:
        parts.append(consciousness_slow)

    # 3. 如果两个都没有，跳过整段（不输出空标题）
    return "\n\n".join(parts)


async def _build_l3_user_present(
    group_id: int,
    bot_self_id: int,
    trigger_user_id: int,
    trigger_nickname: str,
    trigger_content: str,
    is_at_me: bool,
    consciousness_fast: str = "",
    transcript_limit: int = 12,
    bot_history_limit: int = 6,
) -> str:
    """
    L3 快变 user 段：每次都变。
    包含：角色状态、群聊快照、我最近说过的、当前触发消息、决策指令。
    不包含 per-user 长期记忆（L2 已单独成段）。
    """
    parts: list[str] = []

    # 1. 快变意识上下文（角色级）
    if consciousness_fast:
        parts.append(consciousness_fast)

    # 3. 我最近说过的（必须保留「这条当时是对谁说的」信息）
    #    不带 trigger 信息会让 LLM 把上一轮对 X 说的话里的代词「你」
    #    错配到当前触发者 Y 头上（例如把白鱼讲的故事归属给 Welt）。
    bot_history = await db.get_recent_bot_messages(
        group_id, limit=bot_history_limit, within_seconds=1800
    )
    if bot_history:
        lines = []
        for h in bot_history:
            ts = h.get("created_at", "")[:16]
            tuid = h.get("trigger_user_id")
            # 用 trigger_user_id 反查 nickname（最稳妥，避免存的消息里就是错的）
            tnick = ""
            if tuid:
                try:
                    sender_info = await db.get_member_profile(group_id, tuid)
                    tnick = (sender_info or {}).get("nickname", "") or ""
                except Exception:
                    tnick = ""
            if not tnick:
                tnick = f"uid:{tuid}" if tuid else "?"
            lines.append(f"- [{ts}] 我对 {tnick} 说: {h.get('content', '')[:60]}")
        parts.append(
            "【我最近 30 分钟说过的】(每条都标了当时是对谁说的；"
            "你接下来对当前触发者说话时，不要把上一条对别人说过的"
            "话里的「你」错配给他)\n"
            + "\n".join(lines)
        )

    # 4. 群聊最近动态
    recent_msgs = await db.get_recent_messages(group_id, limit=transcript_limit)
    others = [m for m in recent_msgs if m.get("user_id") != bot_self_id]

    topic_hint = await _async_get_topic_hint(group_id)
    snapshot_lines = ["【群聊最近动态】（按时间正序，最新的在最后）"]
    if topic_hint:
        snapshot_lines.append(f"当前活跃话题：{topic_hint}")
    snapshot_lines.append("")
    for m in others:
        nick = m.get("nickname", "某人")
        text = m.get("content", "")
        ts = m.get("created_at", "")[11:16]
        snapshot_lines.append(f"[{ts}] {nick}: {text}")

    trigger_ts = datetime.now().strftime("%H:%M")
    at_flag = "（@了你）" if is_at_me else "（没@你）"
    snapshot_lines.append("")
    snapshot_lines.append(f"--- {trigger_ts} ---")
    snapshot_lines.append(
        f"刚刚 {trigger_nickname} {at_flag} 说: {trigger_content}\n"
        f"（【必读】这条触发消息如果 @ 了别人（不是你的 @），"
        f"那是别人在群里骂/喊/逗另一位群友，跟你没关系——"
        f"中文里「这人是X」「他/她/这个B」紧跟 @某人 时主语就是那位 @，不是机器人。"
        f"不要把自己代进去。）"
    )

    parts.append("\n".join(snapshot_lines))

    # 5. 决策指令
    parts.append(
        "\n【请你作为角色决定】\n"
        "1. 是否要插话？如果群友在自嗨、跟自己无关、或 1 分钟内你已经说过类似话 → 输出 SKIP\n"
        "2. 如果要回：只输出一段纯文本（不超过两句，< 50 字），用群聊口吻\n"
        "3. 不要 @ 别人（除非被 @ 时可以）\n"
        "4. 不要复述对方的话，不要总结，不要解释\n"
        "5. 如果 [图片] / [表情] 让你看不懂内容，承认看不懂，不要瞎接\n"
        "6. 唯一合法输出：要么是一段要发的中文消息，要么就是 SKIP 这两个字母\n"
        "7. 【事实归属铁律】当你提到「你」「他」「刚才 X 讲的故事」时，"
        "必须严格按「群聊最近动态」里标注的人名归属——白鱼讲过的就是白鱼讲的，"
        "Welt 没讲过任何故事。不要把别人做过的事错挂到当前触发者头上。"
    )

    return "\n\n".join(p for p in parts if p)


async def build_runtime_messages(
    *,
    group_id: int,
    bot_self_id: int,
    bot_nickname: str,
    trigger_user_id: int,
    trigger_nickname: str,
    trigger_content: str,
    is_at_me: bool,
    consciousness_slow: str = "",
    consciousness_fast: str = "",
    char_name: str = "",
    mes_example: list | None = None,
    extra_system_blocks: list[str] | None = None,
    character_prompt_override: str = "",
    transcript_limit: int = 12,
    bot_history_limit: int = 6,
) -> list[dict]:
    """
    构造发给 LLM 的完整消息列表（"世界视角" + KV cache 友好分层）。

    Parameters
    ----------
    group_id, bot_self_id, bot_nickname       群号/自己 QQ/自己群昵称
    trigger_user_id, trigger_nickname, trigger_content  触发消息的发送者
    is_at_me                                  是否 @ 了我
    consciousness_slow                        慢变意识上下文（per-user）
    consciousness_fast                         快变意识上下文（角色级）
    char_name                                 角色名（默认从配置取）
    mes_example                               few-shot 示例对话
    extra_system_blocks                       在 L0 后追加的自定义 system 段
    character_prompt_override                 覆盖默认角色 prompt
    transcript_limit                          取多少条群聊近期消息
    bot_history_limit                         取多少条自己最近的发言
    """
    # ── L0 静态 system（角色灵魂）────────────────────────
    char_prompt = character_prompt_override or _config.character_prompt
    l0_system = _build_l0_system(char_prompt, bot_nickname, extra_system_blocks)

    # ── L1 半静态 system（few-shot）─────────────────────
    l1_messages: list[dict] = []
    if mes_example:
        for ex in mes_example[:6]:
            l1_messages.append(ex)

    # ── L2 慢变 user 段（per-user 长期记忆）──────────────
    l2_user = await _build_l2_user_long_term(
        group_id, trigger_user_id, trigger_nickname,
        consciousness_slow=consciousness_slow,
    )

    # ── L3 快变 user 段（当前时刻）──────────────────────
    l3_user = await _build_l3_user_present(
        group_id=group_id,
        bot_self_id=bot_self_id,
        trigger_user_id=trigger_user_id,
        trigger_nickname=trigger_nickname,
        trigger_content=trigger_content,
        is_at_me=is_at_me,
        consciousness_fast=consciousness_fast,
        transcript_limit=transcript_limit,
        bot_history_limit=bot_history_limit,
    )

    # ── 拼装最终消息列表（统一为 [system + user] 格式）──
    # 核心原则：跟私聊保持一致的结构
    #   私聊：[system] 角色卡 + [user] 对话 + [assistant] 回复
    #   群聊：[system] 角色卡+L0+L1+场景 + [user] L2+L3+触发
    # 不允许出现连续的 user role 段，LLM 训练时看到连续 user 会混淆
    #
    # L1（few-shot）嵌到 system 末尾：先注入角色卡，再注入对话示例
    l1_text = ""
    if l1_messages:
        l1_lines = []
        for m in l1_messages:
            role_label = "用户" if m["role"] == "user" else "昔涟"
            l1_lines.append(f"{role_label}: {m.get('content','')}")
        l1_text = "\n\n【对话风格示例】\n" + "\n".join(l1_lines)

    system_content = l0_system
    if l1_text:
        system_content += l1_text

    # 合并 L2 + L3 为一条 user 消息（不再拆成两条连续的 user）
    user_parts = []
    if l2_user:
        user_parts.append(l2_user)
    user_parts.append(l3_user)
    user_content = "\n\n".join(user_parts) if user_parts else "."

    msgs: list[dict] = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]

    # ── 调试钩子：把最后一次发往 LLM 的 prompt 落盘 ──
    await _dump_last_prompt(
        msgs,
        meta={
            "group_id": group_id,
            "trigger_user_id": trigger_user_id,
            "trigger_nickname": trigger_nickname,
            "trigger_content": trigger_content,
            "is_at_me": is_at_me,
        },
    )

    return msgs


async def build_private_runtime_messages(
    *,
    private_group_id: int,
    bot_self_id: int,
    bot_nickname: str,
    trigger_user_id: int,
    trigger_nickname: str,
    trigger_content: str,
    consciousness_slow: str = "",
    consciousness_fast: str = "",
    char_name: str = "",
    mes_example: list | None = None,
    character_prompt_override: str = "",
) -> list[dict]:
    """
    构建私聊场景的 L0~L3 消息列表。

    与群聊版的区别：
    - L0 场景描述为"私聊"而非"QQ群"
    - L3 无群聊 transcript，改用私聊历史
    - 无 @ 检测、无 SKIP 决策指令（私聊永远回复）
    - 无话题提示、无群友列表
    - 无 extra_system_blocks（私聊不需要群角色覆盖）
    """
    # ── L0 静态 system（角色灵魂，私聊版场景）───────────────
    char_prompt = character_prompt_override or _config.character_prompt
    l0_system = _build_l0_system(char_prompt, bot_nickname, scene_type="private")

    # ── L1 半静态 system（few-shot）─────────────────────
    l1_text = ""
    if mes_example:
        l1_lines = []
        for m in mes_example[:6]:
            role_label = "用户" if m["role"] == "user" else (char_name or "昔涟")
            l1_lines.append(f"{role_label}: {m.get('content','')}")
        l1_text = "\n\n【对话风格示例】\n" + "\n".join(l1_lines)

    system_content = l0_system
    if l1_text:
        system_content += l1_text

    # ── L2 慢变 user 段（per-user 长期记忆）──────────────
    l2_parts: list[str] = []

    # 1. 全局共享画像（user_profiles）
    profile = await db.get_user_profile_summary(trigger_user_id)
    if profile:
        l2_parts.append(f"【长期记忆：这位朋友的画像】{profile}")

    # 2. 私聊场景的印象/关系
    if consciousness_slow:
        l2_parts.append(consciousness_slow)

    l2_user = "\n\n".join(l2_parts)

    # ── L3 快变 user 段（当前时刻）────────────────────────
    l3_parts: list[str] = []

    # 1. 快变意识上下文
    if consciousness_fast:
        l3_parts.append(consciousness_fast)

    # 2. 我最近说过的（私聊场景）
    bot_history = await db.get_recent_bot_messages(
        private_group_id, limit=6, within_seconds=1800
    )
    if bot_history:
        lines = []
        for h in bot_history:
            ts = h.get("created_at", "")[:16]
            lines.append(f"- [{ts}] 我说: {h.get('content', '')[:60]}")
        l3_parts.append("【我最近说过的话】（回忆）\n" + "\n".join(lines))

    # 3. 私聊最近消息（类似 transcript，但只含双方）
    recent_msgs = await db.get_recent_messages(private_group_id, limit=10)
    personal_msgs = [
        m for m in recent_msgs
        if m.get("user_id") in (trigger_user_id, bot_self_id)
    ]

    from datetime import datetime as _dt
    snapshot_lines = ["【聊天历史】（按时间正序，最新的在最后）"]
    for m in personal_msgs:
        nick = m.get("nickname", "对方")
        text = m.get("content", "")
        ts = m.get("created_at", "")[11:16]
        snapshot_lines.append(f"[{ts}] {nick}: {text}")

    trigger_ts = _dt.now().strftime("%H:%M")
    snapshot_lines.append("")
    snapshot_lines.append(f"--- {trigger_ts} ---")
    snapshot_lines.append(f"刚刚 {trigger_nickname} 说: {trigger_content}")
    l3_parts.append("\n".join(snapshot_lines))

    # 4. 私聊回复指令（简单直接，不需要 SKIP 选项）
    l3_parts.append(
        "\n【回复要求】\n"
        "1. 用一段自然的口语回复对方，2~3 句话，< 80 字\n"
        "2. 保持角色性格，像朋友聊天一样自然\n"
        "3. 不要复述对方的话，不要总结，不要解释\n"
        "4. 如果 [图片] / [表情] 让你看不懂，老实承认"
    )

    l3_user = "\n\n".join(p for p in l3_parts if p)

    # ── 合并为一条 user 消息 ──
    user_parts = []
    if l2_user:
        user_parts.append(l2_user)
    user_parts.append(l3_user)
    user_content = "\n\n".join(user_parts) if user_parts else "."

    msgs: list[dict] = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]

    await _dump_last_prompt(
        msgs,
        meta={
            "group_id": private_group_id,
            "trigger_user_id": trigger_user_id,
            "trigger_nickname": trigger_nickname,
            "trigger_content": trigger_content,
        },
    )

    return msgs


# ════════════════════════════════════════════════════════
#  调试：把发给 LLM 的最后一份 prompt 写到 data/last_llm_prompt.json
# ════════════════════════════════════════════════════════

import json as _json
import os as _os
from datetime import datetime as _datetime
from pathlib import Path as _Path

_DUMP_PATH = _Path(__file__).resolve().parent.parent.parent / "data" / "last_llm_prompt.json"


async def _dump_last_prompt(msgs: list[dict], meta: dict) -> None:
    """
    把最近一次构造的 LLM 请求写到 data/last_llm_prompt.json。

    行为：
    - 默认禁用（避免在生产里频繁写盘）
    - 启用方式（二选一）：
        1) 环境变量：QQBOT_DUMP_LLM_PROMPT=1
        2) bot.yaml 里：debug.dump_llm_prompt: true
    - 写入失败不影响主流程
    """
    enabled = _os.environ.get("QQBOT_DUMP_LLM_PROMPT") == "1"
    if not enabled:
        try:
            enabled = bool(_config.get("debug.dump_llm_prompt", False))
        except Exception:
            enabled = False
    if not enabled:
        return

    try:
        payload = {
            "ts": _datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "meta": meta,
            "messages": msgs,
            "stats": {
                "total_msgs": len(msgs),
                "total_chars": sum(len(m.get("content", "")) for m in msgs),
                "by_role": {
                    role: sum(len(m.get("content", "")) for m in msgs if m.get("role") == role)
                    for role in ("system", "user", "assistant")
                },
            },
        }
        _DUMP_PATH.parent.mkdir(parents=True, exist_ok=True)
        # 用临时文件 + 原子替换，避免半写状态
        tmp = _DUMP_PATH.with_suffix(".json.tmp")
        tmp.write_text(
            _json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(_DUMP_PATH)
    except Exception as e:
        from nonebot.log import logger as _logger
        _logger.warning(f"[DumpLLMPrompt] 落盘失败: {e}")
