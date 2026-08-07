"""
database.py - 异步 SQLite 数据库模块

负责管理群聊消息存储、成员画像、话题追踪、群画像和学习队列。
使用 aiosqlite 实现全部异步 IO，WAL 模式提升并发性能。
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import aiosqlite

# 数据库文件路径：项目根/data/roleplay.db
DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "roleplay.db"

# ──────────────────────────────────────────────
#  建表 SQL
# ──────────────────────────────────────────────

_CREATE_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    message_id   INTEGER,
    content      TEXT    NOT NULL DEFAULT '',
    message_type TEXT    NOT NULL DEFAULT 'text',
    nickname     TEXT    NOT NULL DEFAULT '',
    created_at   TIMESTAMP NOT NULL DEFAULT (datetime('now','localtime')),
    is_processed INTEGER NOT NULL DEFAULT 0,
    topic_id     INTEGER,
    FOREIGN KEY (topic_id) REFERENCES topics(id)
);
CREATE INDEX IF NOT EXISTS idx_messages_group_created
    ON messages(group_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_unprocessed
    ON messages(group_id, is_processed);
"""

_CREATE_MEMBER_PROFILES = """
CREATE TABLE IF NOT EXISTS member_profiles (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id          INTEGER NOT NULL,
    user_id           INTEGER NOT NULL,
    nickname          TEXT    NOT NULL DEFAULT '',
    personality_tags  TEXT    NOT NULL DEFAULT '[]',
    common_phrases    TEXT    NOT NULL DEFAULT '[]',
    emotional_tendency TEXT   NOT NULL DEFAULT 'neutral',
    activity_level    REAL   NOT NULL DEFAULT 0.0,
    favorite_topics   TEXT    NOT NULL DEFAULT '[]',
    speak_style       TEXT    NOT NULL DEFAULT '',
    last_active       TIMESTAMP,
    message_count     INTEGER NOT NULL DEFAULT 0,
    UNIQUE(group_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_member_profiles_group
    ON member_profiles(group_id);
"""

_CREATE_TOPICS = """
CREATE TABLE IF NOT EXISTS topics (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id         INTEGER NOT NULL,
    topic_name       TEXT    NOT NULL,
    keywords         TEXT    NOT NULL DEFAULT '[]',
    start_time       TIMESTAMP NOT NULL DEFAULT (datetime('now','localtime')),
    last_active      TIMESTAMP NOT NULL DEFAULT (datetime('now','localtime')),
    message_count    INTEGER NOT NULL DEFAULT 0,
    participant_count INTEGER NOT NULL DEFAULT 0,
    summary          TEXT    NOT NULL DEFAULT '',
    status           TEXT    NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_topics_group_status
    ON topics(group_id, status);
"""

_CREATE_GROUP_PROFILES = """
CREATE TABLE IF NOT EXISTS group_profiles (
    group_id      INTEGER PRIMARY KEY,
    group_name    TEXT    NOT NULL DEFAULT '',
    topic_summary TEXT    NOT NULL DEFAULT '',
    created_at    TIMESTAMP NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at    TIMESTAMP NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

_CREATE_LEARNING_QUEUE = """
CREATE TABLE IF NOT EXISTS learning_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id      INTEGER NOT NULL,
    queue_type    TEXT    NOT NULL DEFAULT 'profile',
    data_snapshot TEXT    NOT NULL DEFAULT '{}',
    priority      INTEGER NOT NULL DEFAULT 5,
    created_at    TIMESTAMP NOT NULL DEFAULT (datetime('now','localtime')),
    processed_at  TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_learning_queue_pending
    ON learning_queue(group_id, priority DESC, processed_at);
"""

_CREATE_MEMBER_MEMORIES = """
CREATE TABLE IF NOT EXISTS member_memories (
    group_id          INTEGER NOT NULL,
    user_id           INTEGER NOT NULL,
    recent_msgs       TEXT    NOT NULL DEFAULT '[]',
    snapshots         TEXT    NOT NULL DEFAULT '[]',
    msg_since_snapshot INTEGER NOT NULL DEFAULT 0,
    status            TEXT    NOT NULL DEFAULT 'active',
    remembered_at     TIMESTAMP NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at        TIMESTAMP NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (group_id, user_id)
);
"""

_CREATE_USER_PROFILES = """
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id            INTEGER PRIMARY KEY,
    nickname           TEXT    NOT NULL DEFAULT '',
    personality_tags   TEXT    NOT NULL DEFAULT '[]',
    common_phrases     TEXT    NOT NULL DEFAULT '[]',
    speak_style        TEXT    NOT NULL DEFAULT '',
    favorite_topics    TEXT    NOT NULL DEFAULT '[]',
    emotional_tendency TEXT    NOT NULL DEFAULT 'neutral',
    source             TEXT    NOT NULL DEFAULT '',
    updated_at         TIMESTAMP NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

# ──────────────────────────────────────────────
#  意识层：4 张新表（角色状态 / 情景记忆 / 语义印象 / 关系模型）
# ──────────────────────────────────────────────

_CREATE_ROLE_STATE = """
CREATE TABLE IF NOT EXISTS role_state (
    group_id              INTEGER PRIMARY KEY,
    mood                  TEXT    NOT NULL DEFAULT 'neutral',
    mood_intensity        REAL    NOT NULL DEFAULT 0.5,
    energy                REAL    NOT NULL DEFAULT 0.7,
    current_focus         TEXT    NOT NULL DEFAULT '',
    last_reflection       TEXT    NOT NULL DEFAULT '',
    last_active           TIMESTAMP,
    day_count             INTEGER NOT NULL DEFAULT 0,
    last_proactive_at     TIMESTAMP,
    proactive_count_today INTEGER NOT NULL DEFAULT 0,
    proactive_reset_date  TEXT    NOT NULL DEFAULT '',
    focus_at              TIMESTAMP,                       -- current_focus / last_reflection 的写入时间（用于软过期）
    updated_at            TIMESTAMP NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

_CREATE_EPISODIC_MEMORIES = """
CREATE TABLE IF NOT EXISTS episodic_memories (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id           INTEGER NOT NULL,
    user_id            INTEGER NOT NULL,
    nickname           TEXT    NOT NULL DEFAULT '',
    event_type         TEXT    NOT NULL DEFAULT 'chat',
    summary            TEXT    NOT NULL,
    raw_context        TEXT    NOT NULL DEFAULT '[]',
    topic_keywords     TEXT    NOT NULL DEFAULT '[]',
    valence            REAL    NOT NULL DEFAULT 0.0,
    intensity          REAL    NOT NULL DEFAULT 0.5,
    occurred_at        TIMESTAMP NOT NULL DEFAULT (datetime('now','localtime')),
    last_referenced_at TIMESTAMP,
    decay_score        REAL    NOT NULL DEFAULT 1.0,
    is_archived        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_episodic_group_user
    ON episodic_memories(group_id, user_id, decay_score DESC);
CREATE INDEX IF NOT EXISTS idx_episodic_active
    ON episodic_memories(group_id, is_archived, decay_score DESC);
CREATE INDEX IF NOT EXISTS idx_episodic_occurred
    ON episodic_memories(group_id, occurred_at DESC);
"""

_CREATE_IMPRESSION_NOTES = """
CREATE TABLE IF NOT EXISTS impression_notes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id         INTEGER NOT NULL,
    user_id          INTEGER NOT NULL,
    note             TEXT    NOT NULL,
    polarity         REAL    NOT NULL DEFAULT 0.0,
    confidence       REAL    NOT NULL DEFAULT 0.5,
    confirm_count    INTEGER NOT NULL DEFAULT 1,
    contradict_count INTEGER NOT NULL DEFAULT 0,
    source_event_id  INTEGER,
    evidence_msg_ids TEXT    NOT NULL DEFAULT '[]',   -- JSON 数组，记录这条印象依据的具体 messages.id
    last_updated     TIMESTAMP NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(group_id, user_id, note)
);
CREATE INDEX IF NOT EXISTS idx_impression_user
    ON impression_notes(group_id, user_id, polarity);
"""

_CREATE_RELATIONSHIP_STATE = """
CREATE TABLE IF NOT EXISTS relationship_state (
    group_id          INTEGER NOT NULL,
    user_id           INTEGER NOT NULL,
    familiarity       REAL    NOT NULL DEFAULT 0.0,
    affinity          REAL    NOT NULL DEFAULT 0.0,
    trust             REAL    NOT NULL DEFAULT 0.5,
    interaction_count INTEGER NOT NULL DEFAULT 0,
    last_interaction  TIMESTAMP,
    days_since_met    INTEGER NOT NULL DEFAULT 0,
    stage             TEXT    NOT NULL DEFAULT 'stranger',
    note              TEXT    NOT NULL DEFAULT '',
    first_met_at      TIMESTAMP,
    updated_at        TIMESTAMP NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (group_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_relationship_stage
    ON relationship_state(group_id, stage);
"""

_CREATE_BOT_MESSAGES = """
CREATE TABLE IF NOT EXISTS bot_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id     INTEGER NOT NULL,
    content      TEXT    NOT NULL DEFAULT '',
    trigger_msg_id INTEGER,                -- 触发我发言的群消息 id（外键到 messages.id）
    trigger_user_id INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMP NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_bot_messages_group_time
    ON bot_messages(group_id, created_at DESC);
"""

_ALL_CREATE_SQL = [
    _CREATE_MESSAGES,
    _CREATE_MEMBER_PROFILES,
    _CREATE_TOPICS,
    _CREATE_GROUP_PROFILES,
    _CREATE_LEARNING_QUEUE,
    _CREATE_MEMBER_MEMORIES,
    _CREATE_USER_PROFILES,
    # 意识层 4 张表
    _CREATE_ROLE_STATE,
    _CREATE_EPISODIC_MEMORIES,
    _CREATE_IMPRESSION_NOTES,
    _CREATE_RELATIONSHIP_STATE,
    # Bot 自身发言历史（用于「我最近说过什么」注入）
    _CREATE_BOT_MESSAGES,
]


# ──────────────────────────────────────────────
#  辅助：确保数据目录存在
# ──────────────────────────────────────────────

def _ensure_data_dir() -> None:
    """确保 data 目录存在"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ══════════════════════════════════════════════
#  公开接口
# ══════════════════════════════════════════════

async def init_db() -> None:
    """初始化数据库：创建目录 + 所有表 + WAL 模式 + 字段升级容错"""
    _ensure_data_dir()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        for sql in _ALL_CREATE_SQL:
            await db.executescript(sql)
        # 字段升级：给老库补列（容错）
        await _migrate_columns(db)
        await db.commit()


async def _migrate_columns(db) -> None:
    """轻量级迁移：检查并补齐缺失的列（不影响已存在的数据）"""
    # role_state.focus_at
    cur = await db.execute("PRAGMA table_info(role_state)")
    cols = {row[1] for row in await cur.fetchall()}
    if "focus_at" not in cols:
        try:
            await db.execute("ALTER TABLE role_state ADD COLUMN focus_at TIMESTAMP")
        except Exception:
            pass

    # impression_notes.evidence_msg_ids
    cur = await db.execute("PRAGMA table_info(impression_notes)")
    cols = {row[1] for row in await cur.fetchall()}
    if "evidence_msg_ids" not in cols:
        try:
            await db.execute(
                "ALTER TABLE impression_notes ADD COLUMN evidence_msg_ids TEXT NOT NULL DEFAULT '[]'"
            )
        except Exception:
            pass


# ────────────────── 消息 ──────────────────

async def save_message(
    group_id: int,
    user_id: int,
    message_id: int,
    content: str,
    message_type: str = "text",
    nickname: str = "",
) -> None:
    """保存一条群消息"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """INSERT INTO messages (group_id, user_id, message_id, content, message_type, nickname)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (group_id, user_id, message_id, content, message_type, nickname),
        )
        await db.commit()


async def get_message_by_id(
    group_id: int,
    message_id: int,
) -> dict[str, Any] | None:
    """
    根据 OneBot 消息 id 在指定群内查找历史消息。
    用于解析 reply 段：把 reply 指向的原始消息内容/作者注入上下文。
    """
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM messages
               WHERE group_id = ? AND message_id = ?
               LIMIT 1""",
            (group_id, message_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_unprocessed_messages(
    group_id: int,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """获取指定群未处理的消息，按时间升序"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM messages
               WHERE group_id = ? AND is_processed = 0
               ORDER BY created_at ASC
               LIMIT ?""",
            (group_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def mark_messages_processed(message_ids: list[int]) -> None:
    """将一批消息标记为已处理"""
    if not message_ids:
        return
    placeholders = ",".join("?" * len(message_ids))
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            f"UPDATE messages SET is_processed = 1 WHERE id IN ({placeholders})",
            message_ids,
        )
        await db.commit()


async def get_recent_messages(
    group_id: int,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """获取群内最近 N 条消息（不区分是否已处理）"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM messages
               WHERE group_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (group_id, limit),
        )
        rows = await cursor.fetchall()
        # 返回时间正序（旧→新）
        return [dict(r) for r in reversed(rows)]


# ────────────────── Bot 自身发言历史 ──────────────────

async def save_bot_message(
    group_id: int,
    content: str,
    trigger_msg_id: int | None = None,
    trigger_user_id: int = 0,
) -> int:
    """保存一条 Bot 自己的发言；返回插入的行 id"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            """INSERT INTO bot_messages
               (group_id, content, trigger_msg_id, trigger_user_id)
               VALUES (?, ?, ?, ?)""",
            (group_id, content, trigger_msg_id, trigger_user_id),
        )
        await db.commit()
        return cursor.lastrowid or 0


async def get_recent_bot_messages(
    group_id: int,
    limit: int = 6,
    within_seconds: int = 1800,
) -> list[dict[str, Any]]:
    """
    获取最近 N 条 Bot 自身发言（默认 30 分钟内），按时间正序返回。
    用于在 prompt 中告诉 LLM「我最近说过什么」，避免重复/自相矛盾。
    """
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT id, content, trigger_user_id, created_at
               FROM bot_messages
               WHERE group_id = ?
                 AND created_at > datetime('now','localtime', ?)
               ORDER BY created_at DESC
               LIMIT ?""",
            (group_id, f"-{within_seconds} seconds", limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in reversed(rows)]


async def get_recent_reply_to_user(
    group_id: int,
    user_id: int,
    within_seconds: int = 600,
) -> list[dict[str, Any]]:
    """
    获取我最近对该 user 的所有回复（默认 10 分钟内）。
    用于去重：如果 1 分钟内已经回应过同一个人，就不要再说一次。
    """
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT id, content, created_at
               FROM bot_messages
               WHERE group_id = ?
                 AND trigger_user_id = ?
                 AND created_at > datetime('now','localtime', ?)
               ORDER BY created_at DESC""",
            (group_id, user_id, f"-{within_seconds} seconds"),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_recent_message_users(
    group_id: int,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    获取最近 N 条消息中活跃的用户列表（按发言频次降序）。
    Returns: [{user_id, nickname, msg_count}]，每人一条
    """
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT user_id, nickname, COUNT(*) as msg_count
               FROM (
                   SELECT user_id, nickname FROM messages
                   WHERE group_id = ?
                   ORDER BY id DESC
                   LIMIT ?
               )
               GROUP BY user_id
               ORDER BY msg_count DESC""",
            (group_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_unprocessed_count(group_id: int) -> int:
    """获取群内未处理消息数量"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM messages WHERE group_id = ? AND is_processed = 0",
            (group_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def cleanup_old_messages(days: int = 7) -> int:
    """清理 days 天前的已处理消息，返回删除行数"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            """DELETE FROM messages
               WHERE is_processed = 1
                 AND created_at < datetime('now','localtime', ?)""",
            (f"-{days} days",),
        )
        await db.commit()
        return cursor.rowcount


# ────────────────── 成员画像 ──────────────────

async def upsert_member_profile(
    group_id: int,
    user_id: int,
    nickname: str = "",
) -> None:
    """插入或更新成员基本信息（nickname 随消息更新）"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """INSERT INTO member_profiles (group_id, user_id, nickname, last_active)
               VALUES (?, ?, ?, datetime('now','localtime'))
               ON CONFLICT(group_id, user_id) DO UPDATE SET
                   nickname   = excluded.nickname,
                   last_active = datetime('now','localtime')""",
            (group_id, user_id, nickname),
        )
        await db.commit()


async def update_member_stats(
    group_id: int,
    user_id: int,
    message_length: int,
    words: int,
    punctuation_count: int,
) -> None:
    """更新成员活跃统计（消息计数 +1，活动度递增平均）"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """UPDATE member_profiles
               SET message_count  = message_count + 1,
                   activity_level  = (activity_level * message_count + ?) / (message_count + 1),
                   last_active     = datetime('now','localtime')
               WHERE group_id = ? AND user_id = ?""",
            (message_length, group_id, user_id),
        )
        await db.commit()


async def get_member_profile(
    group_id: int,
    user_id: int,
) -> dict[str, Any] | None:
    """获取单个成员画像"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM member_profiles WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_active_members(
    group_id: int,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """获取群内活跃度最高的 N 位成员"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM member_profiles
               WHERE group_id = ?
               ORDER BY activity_level DESC, message_count DESC
               LIMIT ?""",
            (group_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_all_member_profiles(
    group_id: int,
) -> list[dict[str, Any]]:
    """获取群内全部成员画像"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM member_profiles WHERE group_id = ?",
            (group_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ────────────────── 话题 ──────────────────

async def upsert_topic(
    group_id: int,
    topic_name: str,
    keywords: list[str],
) -> int:
    """
    更新或创建话题。
    - 若同名话题存在且 active → 更新 last_active 与 keywords
    - 否则插入新话题
    返回话题 id。
    """
    kw_json = json.dumps(keywords, ensure_ascii=False)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        # 先尝试更新活跃话题
        cursor = await db.execute(
            """UPDATE topics
               SET last_active   = datetime('now','localtime'),
                   keywords      = ?,
                   message_count = message_count + 1
               WHERE group_id = ? AND topic_name = ? AND status = 'active'""",
            (kw_json, group_id, topic_name),
        )
        if cursor.rowcount:
            # 获取已有 id
            cursor = await db.execute(
                "SELECT id FROM topics WHERE group_id = ? AND topic_name = ? AND status = 'active'",
                (group_id, topic_name),
            )
            row = await cursor.fetchone()
            await db.commit()
            return row[0]

        # 新增
        await db.execute(
            """INSERT INTO topics (group_id, topic_name, keywords)
               VALUES (?, ?, ?)""",
            (group_id, topic_name, kw_json),
        )
        topic_id = cursor.lastrowid  # type: ignore[assignment]
        await db.commit()

        # 用单独查询取回 id（更可靠）
        cursor = await db.execute(
            "SELECT id FROM topics WHERE group_id = ? AND topic_name = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
            (group_id, topic_name),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_active_topics(
    group_id: int,
) -> list[dict[str, Any]]:
    """获取群内所有活跃话题"""
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


async def update_topic_summary(
    topic_id: int,
    summary: str,
) -> None:
    """更新话题摘要"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "UPDATE topics SET summary = ? WHERE id = ?",
            (summary, topic_id),
        )
        await db.commit()


# ────────────────── 成员画像分析字段 ──────────────────

async def update_member_personality_tags(
    group_id: int,
    user_id: int,
    personality_tags: list[str] | None = None,
    common_phrases: list[str] | None = None,
    emotional_tendency: str | None = None,
    speak_style: str | None = None,
    favorite_topics: list[str] | None = None,
) -> None:
    """
    更新成员画像的深度分析字段（性格标签、口头禅、情绪倾向、说话风格、兴趣话题）。
    只更新传入的非 None 字段。
    """
    sets = []
    params = []
    if personality_tags is not None:
        sets.append("personality_tags = ?")
        params.append(json.dumps(personality_tags, ensure_ascii=False))
    if common_phrases is not None:
        sets.append("common_phrases = ?")
        params.append(json.dumps(common_phrases, ensure_ascii=False))
    if emotional_tendency is not None:
        sets.append("emotional_tendency = ?")
        params.append(emotional_tendency)
    if speak_style is not None:
        sets.append("speak_style = ?")
        params.append(speak_style)
    if favorite_topics is not None:
        sets.append("favorite_topics = ?")
        params.append(json.dumps(favorite_topics, ensure_ascii=False))
    if not sets:
        return
    params.extend([group_id, user_id])
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            f"UPDATE member_profiles SET {', '.join(sets)} WHERE group_id = ? AND user_id = ?",
            params,
        )
        await db.commit()


# ────────────────── 成员记忆 ──────────────────

_MAX_RECENT_MSGS = 10
_MAX_SNAPSHOTS = 3
_SNAPSHOT_TRIGGER = 5  # 每积累5条新消息触发快照


async def upsert_member_memory(
    group_id: int,
    user_id: int,
    content: str,
    nickname: str = "",
) -> dict:
    """
    追加一条消息到成员记忆的滚动窗口。
    自动维护最近 MAX_RECENT_MSGS 条消息，返回是否需要触发快照。
    """
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT recent_msgs, msg_since_snapshot FROM member_memories WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        row = await cursor.fetchone()

        if row:
            msgs = json.loads(row["recent_msgs"])
            snapshot_count = row["msg_since_snapshot"]
        else:
            msgs = []
            snapshot_count = 0

        # 追加新消息（最多保留 MAX_RECENT_MSGS 条）
        new_entry = {
            "content": content[:200],
            "nickname": nickname,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        msgs.append(new_entry)
        if len(msgs) > _MAX_RECENT_MSGS:
            msgs = msgs[-_MAX_RECENT_MSGS:]

        snapshot_count += 1
        need_snapshot = snapshot_count >= _SNAPSHOT_TRIGGER
        if need_snapshot:
            snapshot_count = 0

        await db.execute(
            """INSERT INTO member_memories (group_id, user_id, recent_msgs, msg_since_snapshot, updated_at)
               VALUES (?, ?, ?, ?, datetime('now','localtime'))
               ON CONFLICT(group_id, user_id) DO UPDATE SET
                   recent_msgs = excluded.recent_msgs,
                   msg_since_snapshot = excluded.msg_since_snapshot,
                   updated_at = datetime('now','localtime')""",
            (group_id, user_id, json.dumps(msgs, ensure_ascii=False), snapshot_count),
        )
        await db.commit()

    return {"need_snapshot": need_snapshot}


async def add_member_snapshot(
    group_id: int,
    user_id: int,
    snapshot_text: str,
) -> None:
    """添加一条记忆快照，保留最近 MAX_SNAPSHOTS 条"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "SELECT snapshots FROM member_memories WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        row = await cursor.fetchone()

        snapshots = json.loads(row["snapshots"]) if row else []

        snapshots.append({
            "text": snapshot_text[:300],
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        })
        if len(snapshots) > _MAX_SNAPSHOTS:
            snapshots = snapshots[-_MAX_SNAPSHOTS:]

        await db.execute(
            """UPDATE member_memories
               SET snapshots = ?, remembered_at = datetime('now','localtime'), updated_at = datetime('now','localtime')
               WHERE group_id = ? AND user_id = ?""",
            (json.dumps(snapshots, ensure_ascii=False), group_id, user_id),
        )
        await db.commit()


async def get_member_memory(
    group_id: int,
    user_id: int,
) -> dict[str, Any] | None:
    """获取成员记忆（含滚动消息 + 快照）"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM member_memories WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def mark_member_sleeping(days: int = 30) -> int:
    """标记超过 days 天未活跃的成员为休眠状态，返回处理数"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            """UPDATE member_memories SET status = 'sleeping'
               WHERE status = 'active'
                 AND updated_at < datetime('now','localtime', ?)""",
            (f"-{days} days",),
        )
        await db.commit()
        return cursor.rowcount


async def archive_deep_sleep(days: int = 90) -> int:
    """归档超过 days 天的休眠成员（只保留一条摘要）"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        # 先查出要归档的
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT group_id, user_id, snapshots FROM member_memories "
            "WHERE status = 'sleeping' AND updated_at < datetime('now','localtime', ?)",
            (f"-{days} days",),
        )
        rows = await cursor.fetchall()

        for row in rows:
            snapshots = json.loads(row["snapshots"]) if row["snapshots"] else []
            last_summary = snapshots[-1]["text"] if snapshots else "(已无活跃记录)"
            # 只保留一行归档摘要，清空消息和快照
            await db.execute(
                """UPDATE member_memories
                   SET status = 'archived',
                       recent_msgs = '[]',
                       snapshots = ?,
                       updated_at = datetime('now','localtime')
                   WHERE group_id = ? AND user_id = ?""",
                (json.dumps([{"text": f"(已归档) {last_summary}", "time": datetime.now().strftime("%Y-%m-%d")}], ensure_ascii=False),
                 row["group_id"], row["user_id"]),
            )

        await db.commit()
        return len(rows)


async def wake_member_memory(group_id: int, user_id: int) -> None:
    """休眠/归档成员重新发言时唤醒"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """UPDATE member_memories
               SET status = 'active', updated_at = datetime('now','localtime')
               WHERE group_id = ? AND user_id = ? AND status != 'active'""",
            (group_id, user_id),
        )
        await db.commit()


async def get_member_memory_summary(
    group_id: int,
    user_id: int,
) -> str | None:
    """获取成员记忆的简要文本摘要（用于注入 AI 上下文）"""
    mem = await get_member_memory(group_id, user_id)
    if not mem or mem.get("status") != "active":
        return None

    snapshots = json.loads(mem.get("snapshots", "[]"))
    recent_msgs = json.loads(mem.get("recent_msgs", "[]"))

    parts = []
    if snapshots:
        texts = [s["text"] for s in snapshots]
        parts.append("记忆：" + "；".join(texts))
    if recent_msgs:
        # 取最近2条
        last_two = recent_msgs[-2:]
        lines = [f"{m['nickname']}: {m['content'][:40]}" for m in last_two]
        parts.append("最近：" + " ".join(lines))

    return "\n".join(parts) if parts else None


# ────────────────── 群画像 ──────────────────

async def upsert_group_profile(
    group_id: int,
    group_name: str = "",
    topic_summary: str = "",
) -> None:
    """插入或更新群画像"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """INSERT INTO group_profiles (group_id, group_name, topic_summary, created_at, updated_at)
               VALUES (?, ?, ?, datetime('now','localtime'), datetime('now','localtime'))
               ON CONFLICT(group_id) DO UPDATE SET
                   group_name   = CASE WHEN excluded.group_name != '' THEN excluded.group_name ELSE group_profiles.group_name END,
                   topic_summary = CASE WHEN excluded.topic_summary != '' THEN excluded.topic_summary ELSE group_profiles.topic_summary END,
                   updated_at   = datetime('now','localtime')""",
            (group_id, group_name, topic_summary),
        )
        await db.commit()


async def update_group_summary(
    group_id: int,
    topic_summary: str,
) -> None:
    """更新群画像的 topic_summary"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "UPDATE group_profiles SET topic_summary = ?, updated_at = datetime('now','localtime') WHERE group_id = ?",
            (topic_summary, group_id),
        )
        await db.commit()


async def get_group_profile(
    group_id: int,
) -> dict[str, Any] | None:
    """获取群画像"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM group_profiles WHERE group_id = ?",
            (group_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


# ────────────────── 全局用户画像（跨私聊/群聊共享） ──────────────────

async def upsert_user_profile(
    user_id: int,
    nickname: str = "",
    source: str = "",
) -> None:
    """插入或更新全局用户画像基本信息"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """INSERT INTO user_profiles (user_id, nickname, source, updated_at)
               VALUES (?, ?, ?, datetime('now','localtime'))
               ON CONFLICT(user_id) DO UPDATE SET
                   nickname   = CASE WHEN excluded.nickname != '' THEN excluded.nickname ELSE user_profiles.nickname END,
                   source     = CASE WHEN excluded.source != '' THEN excluded.source ELSE user_profiles.source END,
                   updated_at = datetime('now','localtime')""",
            (user_id, nickname, source),
        )
        await db.commit()


async def get_user_profile(
    user_id: int,
) -> dict[str, Any] | None:
    """获取全局用户画像"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def update_user_profile_tags(
    user_id: int,
    personality_tags: list[str] | None = None,
    common_phrases: list[str] | None = None,
    speak_style: str | None = None,
    favorite_topics: list[str] | None = None,
    emotional_tendency: str | None = None,
) -> None:
    """更新全局用户画像的深度分析字段"""
    sets, params = [], []
    if personality_tags is not None:
        sets.append("personality_tags = ?")
        params.append(json.dumps(personality_tags, ensure_ascii=False))
    if common_phrases is not None:
        sets.append("common_phrases = ?")
        params.append(json.dumps(common_phrases, ensure_ascii=False))
    if speak_style is not None:
        sets.append("speak_style = ?")
        params.append(speak_style)
    if favorite_topics is not None:
        sets.append("favorite_topics = ?")
        params.append(json.dumps(favorite_topics, ensure_ascii=False))
    if emotional_tendency is not None:
        sets.append("emotional_tendency = ?")
        params.append(emotional_tendency)
    if not sets:
        return
    params.append(user_id)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            f"UPDATE user_profiles SET {', '.join(sets)}, updated_at = datetime('now','localtime') WHERE user_id = ?",
            params,
        )
        await db.commit()


async def get_user_profile_summary(user_id: int) -> str | None:
    """获取全局用户画像的简要文本摘要（用于注入 AI 上下文）"""
    profile = await get_user_profile(user_id)
    if not profile:
        return None
    tags = json.loads(profile.get("personality_tags", "[]"))
    phrases = json.loads(profile.get("common_phrases", "[]"))
    topics = json.loads(profile.get("favorite_topics", "[]"))
    style = profile.get("speak_style", "")
    parts = []
    if tags:
        parts.append("性格：" + "、".join(tags[:5]))
    if phrases:
        parts.append("口头禅：" + "、".join(phrases[:3]))
    if topics:
        parts.append("兴趣：" + "、".join(topics[:3]))
    if style:
        parts.append("风格：" + style)
    return "；".join(parts) if parts else None


# ══════════════════════════════════════════════
#  意识层 CRUD（role_state / episodic_memories / impression_notes / relationship_state）
# ══════════════════════════════════════════════


# ────────────────── 角色状态 ──────────────────

async def get_role_state(group_id: int) -> dict[str, Any] | None:
    """获取某群的角色状态（不存在则返回 None）"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM role_state WHERE group_id = ?", (group_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def upsert_role_state(group_id: int, **fields) -> None:
    """
    插入或更新角色状态。
    支持字段：mood, mood_intensity, energy, current_focus, last_reflection,
             last_active, day_count, last_proactive_at, proactive_count_today。
    """
    if not fields:
        return
    # 第一次插入
    existing = await get_role_state(group_id)
    if not existing:
        defaults = {
            "mood": "neutral",
            "mood_intensity": 0.5,
            "energy": 0.7,
            "current_focus": "",
            "last_reflection": "",
            "last_active": None,
            "day_count": 0,
            "last_proactive_at": None,
            "proactive_count_today": 0,
            "focus_at": None,
        }
        defaults.update(fields)
        cols = ", ".join(defaults.keys())
        placeholders = ", ".join("?" for _ in defaults)
        async with aiosqlite.connect(str(DB_PATH)) as conn:
            await conn.execute(
                f"""INSERT INTO role_state (group_id, {cols})
                    VALUES (?, {placeholders})""",
                (group_id, *defaults.values()),
            )
            await conn.commit()
        return
    # 更新
    sets = []
    params = []
    for k, v in fields.items():
        sets.append(f"{k} = ?")
        params.append(v)
    params.append(group_id)
    async with aiosqlite.connect(str(DB_PATH)) as conn:
        await conn.execute(
            f"UPDATE role_state SET {', '.join(sets)}, updated_at = datetime('now','localtime') WHERE group_id = ?",
            params,
        )
        await conn.commit()


async def increment_proactive_count(group_id: int) -> int:
    """原子地增加主动发言计数（按日期重置），返回增加后的值"""
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(str(DB_PATH)) as db:
        # 先确保行存在
        await db.execute(
            """INSERT OR IGNORE INTO role_state (group_id, proactive_reset_date)
               VALUES (?, ?)""",
            (group_id, today),
        )
        # 重置（如果跨天）
        await db.execute(
            """UPDATE role_state
               SET proactive_count_today = 0,
                   proactive_reset_date = ?
               WHERE group_id = ? AND (proactive_reset_date IS NULL OR proactive_reset_date != ?)""",
            (today, group_id, today),
        )
        # 原子 +1
        await db.execute(
            """UPDATE role_state
               SET proactive_count_today = proactive_count_today + 1,
                   last_proactive_at = datetime('now','localtime'),
                   updated_at = datetime('now','localtime')
               WHERE group_id = ?""",
            (group_id,),
        )
        cursor = await db.execute(
            "SELECT proactive_count_today FROM role_state WHERE group_id = ?",
            (group_id,),
        )
        row = await cursor.fetchone()
        await db.commit()
        return row[0] if row else 0


async def get_all_active_role_states() -> list[dict[str, Any]]:
    """获取所有有状态的角色（用于定时任务遍历）"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM role_state WHERE updated_at > datetime('now', '-7 day')"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ────────────────── 情景记忆 ──────────────────

async def insert_episode(
    group_id: int,
    user_id: int,
    nickname: str,
    event_type: str,
    summary: str,
    raw_context: list[dict] | str = "[]",
    topic_keywords: list[str] | str = "[]",
    valence: float = 0.0,
    intensity: float = 0.5,
) -> int:
    """
    插入一条情景记忆。raw_context / topic_keywords 接受 list 或 JSON 字符串。
    Returns: 新行的 id
    """
    if isinstance(raw_context, list):
        raw_context = json.dumps(raw_context, ensure_ascii=False)
    if isinstance(topic_keywords, list):
        topic_keywords = json.dumps(topic_keywords, ensure_ascii=False)

    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            """INSERT INTO episodic_memories
               (group_id, user_id, nickname, event_type, summary,
                raw_context, topic_keywords, valence, intensity)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (group_id, user_id, nickname, event_type, summary,
             raw_context, topic_keywords, valence, intensity),
        )
        await db.commit()
        return cursor.lastrowid or 0


async def get_top_episodes(
    group_id: int,
    user_id: int | None = None,
    top_k: int = 3,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """
    获取情景记忆，按 decay_score 降序。
    user_id=None 表示整个群；top_k 控制返回数量。
    """
    sql = """SELECT * FROM episodic_memories
             WHERE group_id = ? AND is_archived <= ?"""
    params: list = [group_id, 0 if not include_archived else 1]
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(user_id)
    sql += " ORDER BY decay_score DESC, occurred_at DESC LIMIT ?"
    params.append(top_k)

    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_episodes_for_decay(group_id: int | None = None) -> list[dict[str, Any]]:
    """获取所有未归档的情景记忆（供衰减 tick 处理）"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        if group_id is not None:
            cursor = await db.execute(
                """SELECT * FROM episodic_memories
                   WHERE group_id = ? AND is_archived = 0""",
                (group_id,),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM episodic_memories WHERE is_archived = 0"
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def update_episode_decay(episode_id: int, new_score: float, archive: bool = False) -> None:
    """更新单条情景记忆的 decay_score，必要时归档"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """UPDATE episodic_memories
               SET decay_score = ?,
                   is_archived = CASE WHEN ? THEN 1 ELSE is_archived END
               WHERE id = ?""",
            (new_score, 1 if archive else 0, episode_id),
        )
        await db.commit()


async def touch_episode(episode_id: int) -> None:
    """标记情景记忆刚被引用（用于 frequency_factor）"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """UPDATE episodic_memories
               SET last_referenced_at = datetime('now','localtime')
               WHERE id = ?""",
            (episode_id,),
        )
        await db.commit()


async def get_recent_episodes_for_user(
    group_id: int, user_id: int, limit: int = 3
) -> list[dict[str, Any]]:
    """获取某用户最近的情景记忆（按时间倒序），用于事件抽取的上下文"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM episodic_memories
               WHERE group_id = ? AND user_id = ? AND is_archived = 0
               ORDER BY occurred_at DESC LIMIT ?""",
            (group_id, user_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ────────────────── 印象笔记 ──────────────────

async def find_impression(
    group_id: int, user_id: int, note: str
) -> dict[str, Any] | None:
    """查找已有的某条印象笔记（精确匹配 note 文本）"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM impression_notes
               WHERE group_id = ? AND user_id = ? AND note = ?""",
            (group_id, user_id, note),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def insert_impression(
    group_id: int,
    user_id: int,
    note: str,
    polarity: float = 0.0,
    confidence: float = 0.5,
    source_event_id: int | None = None,
    evidence_msg_ids: list[int] | None = None,
) -> int:
    """插入一条新印象笔记。Returns: 新行 id"""
    ev_json = json.dumps(evidence_msg_ids or [])
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            """INSERT INTO impression_notes
               (group_id, user_id, note, polarity, confidence, source_event_id, evidence_msg_ids)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (group_id, user_id, note, polarity, confidence, source_event_id, ev_json),
        )
        await db.commit()
        return cursor.lastrowid or 0


async def update_impression(
    impression_id: int,
    polarity: float | None = None,
    confidence: float | None = None,
    confirm_count: int | None = None,
    contradict_count: int | None = None,
    evidence_msg_ids: list[int] | None = None,
) -> None:
    """更新印象笔记字段（None 的字段不动）"""
    sets = []
    params = []
    if polarity is not None:
        sets.append("polarity = ?")
        params.append(polarity)
    if confidence is not None:
        sets.append("confidence = ?")
        params.append(confidence)
    if confirm_count is not None:
        sets.append("confirm_count = ?")
        params.append(confirm_count)
    if contradict_count is not None:
        sets.append("contradict_count = ?")
        params.append(contradict_count)
    if evidence_msg_ids is not None:
        sets.append("evidence_msg_ids = ?")
        params.append(json.dumps(evidence_msg_ids))
    if not sets:
        return
    sets.append("last_updated = datetime('now','localtime')")
    params.append(impression_id)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            f"UPDATE impression_notes SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        await db.commit()


async def delete_impression(impression_id: int) -> None:
    """删除一条印象笔记"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("DELETE FROM impression_notes WHERE id = ?", (impression_id,))
        await db.commit()


async def get_user_impressions(
    group_id: int, user_id: int, limit: int = 5
) -> list[dict[str, Any]]:
    """获取某用户的所有印象笔记（按 confidence × |polarity| 排序）"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM impression_notes
               WHERE group_id = ? AND user_id = ?
               ORDER BY (confidence * (ABS(polarity) + 0.1)) DESC, last_updated DESC
               LIMIT ?""",
            (group_id, user_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ────────────────── 关系模型 ──────────────────

async def get_relationship(group_id: int, user_id: int) -> dict[str, Any] | None:
    """获取某用户的关系状态"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM relationship_state WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def upsert_relationship(
    group_id: int, user_id: int, **fields
) -> None:
    """
    插入或更新关系状态。fields 接受的键：
    familiarity / affinity / trust / interaction_count / last_interaction /
    days_since_met / stage / note / first_met_at
    """
    async with aiosqlite.connect(str(DB_PATH)) as conn:
        existing = await conn.execute(
            "SELECT 1 FROM relationship_state WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        row = await existing.fetchone()
        if row is None:
            defaults = {
                "familiarity": 0.0,
                "affinity": 0.0,
                "trust": 0.5,
                "interaction_count": 0,
                "last_interaction": None,
                "days_since_met": 0,
                "stage": "stranger",
                "note": "",
                "first_met_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            defaults.update(fields)
            cols = ", ".join(["group_id", "user_id"] + list(defaults.keys()))
            placeholders = ", ".join("?" for _ in range(2 + len(defaults)))
            values = [group_id, user_id] + list(defaults.values())
            await conn.execute(
                f"""INSERT INTO relationship_state ({cols})
                    VALUES ({placeholders})""",
                values,
            )
        else:
            if not fields:
                await conn.commit()
                return
            sets = [f"{k} = ?" for k in fields.keys()]
            params = list(fields.values()) + [group_id, user_id]
            await conn.execute(
                f"""UPDATE relationship_state
                    SET {', '.join(sets)},
                        updated_at = datetime('now','localtime')
                    WHERE group_id = ? AND user_id = ?""",
                params,
            )
        await conn.commit()


async def bump_interaction(
    group_id: int, user_id: int, delta_familiarity: float = 0.01, delta_affinity: float = 0.0
) -> None:
    """
    记录一次互动：interaction_count +1，familiarity 按对数曲线递增，affinity 应用 delta。
    """
    import math
    existing = await get_relationship(group_id, user_id)
    if not existing:
        await upsert_relationship(
            group_id, user_id,
            interaction_count=1,
            first_met_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            last_interaction=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        return

    new_count = existing["interaction_count"] + 1
    # familiarity 用对数曲线：1 -> 0.1, 10 -> 0.4, 50 -> 0.6, 100 -> 0.7
    new_familiarity = min(1.0, 0.1 * math.log10(max(1, new_count)) + 0.1)
    new_affinity = max(-1.0, min(1.0, existing["affinity"] + delta_affinity))

    await upsert_relationship(
        group_id, user_id,
        interaction_count=new_count,
        familiarity=new_familiarity,
        affinity=new_affinity,
        last_interaction=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


async def infer_relationship_stage(
    familiarity: float, affinity: float, days_since_met: int, interaction_count: int
) -> str:
    """根据数值推断关系阶段（纯函数，便于测试）"""
    if days_since_met > 30 and interaction_count < 5:
        return "distant"
    if familiarity >= 0.7 and affinity > 0.5:
        return "intimate"
    if familiarity >= 0.4:
        return "familiar"
    if familiarity >= 0.1:
        return "acquaintance"
    return "stranger"


async def refresh_relationship_stages(group_id: int) -> int:
    """重算某群所有关系的 stage，返回处理数"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM relationship_state WHERE group_id = ?", (group_id,)
        )
        rows = await cursor.fetchall()
    count = 0
    for r in rows:
        d = dict(r)
        new_stage = await infer_relationship_stage(
            d["familiarity"], d["affinity"],
            d["days_since_met"], d["interaction_count"],
        )
        if new_stage != d["stage"]:
            await upsert_relationship(group_id, d["user_id"], stage=new_stage)
            count += 1
    return count


async def get_relationships_by_stage(
    group_id: int, stage: str
) -> list[dict[str, Any]]:
    """按关系阶段获取用户列表（用于主动发起器）"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT r.*, p.nickname FROM relationship_state r
               LEFT JOIN member_profiles p
                 ON r.group_id = p.group_id AND r.user_id = p.user_id
               WHERE r.group_id = ? AND r.stage = ?
               ORDER BY r.familiarity DESC""",
            (group_id, stage),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_distant_friends(group_id: int, threshold_hours: int = 24) -> list[dict[str, Any]]:
    """获取亲近但最近未互动的人（用于主动发起问候）"""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT r.*, p.nickname FROM relationship_state r
               LEFT JOIN member_profiles p
                 ON r.group_id = p.group_id AND r.user_id = p.user_id
               WHERE r.group_id = ?
                 AND r.familiarity > 0.4
                 AND r.affinity > 0.2
                 AND (r.last_interaction IS NULL
                      OR r.last_interaction < datetime('now','localtime', ?))""",
            (group_id, f"-{threshold_hours} hours"),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
