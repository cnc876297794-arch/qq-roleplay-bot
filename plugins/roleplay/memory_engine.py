"""
memory_engine.py - 成员记忆引擎

负责：
- 根据最近消息生成记忆快照（低 token AI 调用）
- 管理快照触发逻辑（每5条消息触发一次）
- 注入成员记忆到 AI 上下文
"""

import json
import logging
from typing import Optional

from . import database as db

logger = logging.getLogger(__name__)


# ─── 快照生成 ──────────────────────────────────

def _build_snapshot_prompt(
    nickname: str,
    recent_msgs: list[dict],
    old_snapshots: list[dict],
) -> list[dict]:
    """
    构造快照生成的 prompt。
    用旧快照做参考，描述变化，保持低 token 消耗。
    """
    msg_lines = [
        f"{m['nickname']}: {m['content'][:80]}"
        for m in recent_msgs
    ]
    msg_sample = "\n".join(msg_lines)

    old_summary = ""
    if old_snapshots:
        texts = [s["text"] for s in old_snapshots]
        old_summary = "之前的印象：" + "；".join(texts)

    prompt = f"""根据以下群聊消息，用20字以内总结这个人在聊什么、什么状态。

昵称：{nickname}
最近发言：
{msg_sample}
{old_summary}

只输出总结，不输出其他内容。"""

    return [{"role": "user", "content": prompt.strip()}]


async def generate_snapshot(
    group_id: int,
    user_id: int,
    nickname: str,
    ai_client,
) -> str | None:
    """
    为某个成员生成一条记忆快照。
    读取最近消息 → AI 总结 → 写入数据库。
    """
    mem = await db.get_member_memory(group_id, user_id)
    if not mem:
        return None

    recent_msgs = json.loads(mem.get("recent_msgs", "[]"))
    if not recent_msgs:
        return None

    # 只取有内容的文本消息
    text_msgs = [m for m in recent_msgs if m.get("content", "").strip()]
    if not text_msgs:
        return None

    old_snapshots = json.loads(mem.get("snapshots", "[]"))

    messages = _build_snapshot_prompt(nickname, text_msgs, old_snapshots)

    try:
        reply, _ = await ai_client.chat_for_learning(messages)
        snapshot_text = reply.strip() if reply else ""

        # 快照太短说明没生成有效内容
        if len(snapshot_text) < 4:
            snapshot_text = f"{nickname} 最近在群内聊天"

        # 写入数据库
        await db.add_member_snapshot(group_id, user_id, snapshot_text)
        logger.info(f"[Snapshot] {nickname}: {snapshot_text}")
        return snapshot_text

    except Exception as e:
        logger.error(f"[Snapshot Error] user={user_id}: {e}")
        return None


# ─── 批量快照 ──────────────────────────────────

async def process_pending_snapshots(group_id: int, ai_client, max_users: int = 5) -> int:
    """
    处理所有需要快照的成员（msg_since_snapshot >= 5）。
    """
    import aiosqlite

    from .database import _SNAPSHOT_TRIGGER, DB_PATH

    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT m.group_id, m.user_id, p.nickname
               FROM member_memories m
               LEFT JOIN member_profiles p ON m.group_id = p.group_id AND m.user_id = p.user_id
               WHERE m.group_id = ? AND m.status = 'active' AND m.msg_since_snapshot >= ?
               ORDER BY m.updated_at DESC
               LIMIT ?""",
            (group_id, _SNAPSHOT_TRIGGER, max_users),
        )
        rows = await cursor.fetchall()

    generated = 0
    for row in rows:
        nickname = row["nickname"] or str(row["user_id"])
        result = await generate_snapshot(
            row["group_id"], row["user_id"], nickname, ai_client
        )
        if result:
            generated += 1

    return generated


async def process_all_groups_snapshots(ai_client, max_users_per_group: int = 3) -> int:
    """
    遍历所有有活跃消息的群，处理待快照成员。
    """
    import aiosqlite

    from .database import DB_PATH

    try:
        async with aiosqlite.connect(str(DB_PATH)) as conn:
            cursor = await conn.execute(
                "SELECT DISTINCT group_id FROM messages WHERE created_at > datetime('now', '-1 day')"
            )
            rows = await cursor.fetchall()

        total = 0
        for (gid,) in rows:
            count = await process_pending_snapshots(gid, ai_client, max_users_per_group)
            total += count
        return total

    except Exception as e:
        logger.error(f"[Batch Snapshot Error] {e}")
        return 0
