"""
group_admin 数据库模块

复用 roleplay 的数据库文件 (data/roleplay.db)，但使用完全独立的表。
新增表：
- group_admin_settings: 群管理设置
- group_keywords: 关键词列表
- admin_logs: 管理操作日志
"""

import json
from pathlib import Path
from typing import Any, Optional

import aiosqlite

# 复用 roleplay 的数据库路径
DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "roleplay.db"

# ──────────────────────────────────────────────
#  群管理相关表 SQL
# ──────────────────────────────────────────────

_CREATE_GROUP_ADMIN_SETTINGS = """
CREATE TABLE IF NOT EXISTS group_admin_settings (
    group_id        INTEGER PRIMARY KEY,
    bot_enabled     INTEGER NOT NULL DEFAULT 1,
    reply_probability REAL NOT NULL DEFAULT 0.02,
    keyword_filter  INTEGER NOT NULL DEFAULT 1,
    updated_at      TIMESTAMP NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

_CREATE_GROUP_KEYWORDS = """
CREATE TABLE IF NOT EXISTS group_keywords (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    INTEGER NOT NULL,
    keyword     TEXT    NOT NULL,
    action      TEXT    NOT NULL DEFAULT 'ban',
    ban_duration INTEGER NOT NULL DEFAULT 600,
    created_at  TIMESTAMP NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(group_id, keyword)
);
CREATE INDEX IF NOT EXISTS idx_group_keywords_group ON group_keywords(group_id);
"""

_CREATE_ADMIN_LOGS = """
CREATE TABLE IF NOT EXISTS admin_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    INTEGER NOT NULL,
    admin_id    INTEGER NOT NULL,
    admin_role  TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    target_id   INTEGER,
    duration    INTEGER,
    reason      TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_admin_logs_group ON admin_logs(group_id, created_at DESC);
"""

_ADMIN_TABLES = [
    _CREATE_GROUP_ADMIN_SETTINGS,
    _CREATE_GROUP_KEYWORDS,
    _CREATE_ADMIN_LOGS,
]


# ──────────────────────────────────────────────
#  公开接口
# ──────────────────────────────────────────────

async def init_admin_tables() -> None:
    """初始化群管理相关表（安全调用，已存在则跳过）"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        for sql in _ADMIN_TABLES:
            await db.executescript(sql)
        await db.commit()


# ─── 群设置 ───────────────────────────────────

async def get_group_settings(group_id: int) -> dict[str, Any] | None:
    """获取群管理设置"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM group_admin_settings WHERE group_id = ?",
            (group_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def ensure_group_settings(group_id: int) -> None:
    """确保群设置记录存在（不存在则插入默认值）"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """INSERT OR IGNORE INTO group_admin_settings (group_id)
               VALUES (?)""",
            (group_id,),
        )
        await db.commit()


async def set_bot_enabled(group_id: int, enabled: bool) -> None:
    """设置 Bot 开关状态"""
    await ensure_group_settings(group_id)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "UPDATE group_admin_settings SET bot_enabled = ?, updated_at = datetime('now','localtime') WHERE group_id = ?",
            (1 if enabled else 0, group_id),
        )
        await db.commit()


async def is_bot_enabled(group_id: int) -> bool:
    """检查 Bot 是否在该群开启"""
    settings = await get_group_settings(group_id)
    if settings is None:
        return True  # 默认开启
    return bool(settings.get("bot_enabled", 1))


async def set_reply_probability(group_id: int, probability: float) -> None:
    """设置回复概率"""
    await ensure_group_settings(group_id)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "UPDATE group_admin_settings SET reply_probability = ?, updated_at = datetime('now','localtime') WHERE group_id = ?",
            (probability, group_id),
        )
        await db.commit()


async def get_reply_probability(group_id: int) -> float:
    """获取回复概率"""
    settings = await get_group_settings(group_id)
    if settings is None:
        return 0.02  # 默认 2%
    return float(settings.get("reply_probability", 0.02))


# ─── 关键词 ───────────────────────────────────

async def get_group_keywords(group_id: int) -> list[dict[str, Any]]:
    """获取群关键词列表"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM group_keywords WHERE group_id = ?",
            (group_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def add_keyword(group_id: int, keyword: str, action: str = "ban", ban_duration: int = 600) -> None:
    """添加关键词"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """INSERT INTO group_keywords (group_id, keyword, action, ban_duration)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(group_id, keyword) DO UPDATE SET
                   action = excluded.action,
                   ban_duration = excluded.ban_duration""",
            (group_id, keyword, action, ban_duration),
        )
        await db.commit()


async def remove_keyword(group_id: int, keyword: str) -> None:
    """删除关键词"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "DELETE FROM group_keywords WHERE group_id = ? AND keyword = ?",
            (group_id, keyword),
        )
        await db.commit()


async def check_keyword_match(group_id: int, text: str) -> dict[str, Any] | None:
    """检查文本是否匹配关键词，返回匹配的关键词规则"""
    keywords = await get_group_keywords(group_id)
    for kw in keywords:
        if kw["keyword"] in text:
            return kw
    return None


# ─── 管理日志 ─────────────────────────────────

async def log_admin_action(
    group_id: int,
    admin_id: int,
    admin_role: str,
    action: str,
    target_id: int | None = None,
    duration: int | None = None,
    reason: str | None = None,
) -> None:
    """记录管理操作日志"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """INSERT INTO admin_logs (group_id, admin_id, admin_role, action, target_id, duration, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (group_id, admin_id, admin_role, action, target_id, duration, reason),
        )
        await db.commit()


# ─── 统计查询 ─────────────────────────────────

async def get_active_members_rank(group_id: int, limit: int = 10) -> list[dict[str, Any]]:
    """获取活跃成员排行（基于 member_profiles 表）"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM member_profiles
               WHERE group_id = ?
               ORDER BY message_count DESC, activity_level DESC
               LIMIT ?""",
            (group_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_member_profile(group_id: int, user_id: int) -> dict[str, Any] | None:
    """获取成员画像（复用 roleplay 的表）"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM member_profiles WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_active_topics(group_id: int) -> list[dict[str, Any]]:
    """获取活跃话题（复用 roleplay 的表）"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM topics
               WHERE group_id = ? AND status = 'active'
               ORDER BY last_active DESC""",
            (group_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_today_message_count(group_id: int) -> int:
    """获取今日消息数量"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            """SELECT COUNT(*) FROM messages
               WHERE group_id = ?
               AND created_at > datetime('now', 'start of day')""",
            (group_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0
