"""
topic_tracker.py - 话题跟踪引擎

两层架构：
- 层级1（本地，0 token）：jieba关键词提取 + 话题匹配
- 层级2（AI，低token）：每30分钟批量摘要
"""

import json
import logging
import time

logger = logging.getLogger(__name__)
from collections import Counter
from typing import Optional

import jieba.analyse

from . import database as db
from .message_processor import extract_keywords


def extract_topic_keywords(texts: list[str], top_k: int = 8) -> list[str]:
    """从一批消息中提取话题关键词（本地，0 token）"""
    combined = " ".join(texts)
    # 用jieba TF-IDF提取关键词
    keywords = jieba.analyse.extract_tags(combined, topK=top_k, withWeight=False)
    # 补充高频词
    local_kw = extract_keywords(combined, top_k=5)
    seen = set(keywords)
    for kw in local_kw:
        if kw not in seen and len(kw) >= 2:
            keywords.append(kw)
            seen.add(kw)
    return keywords[:top_k]


def match_existing_topic(new_keywords: list[str], existing_topics: list[dict],
                          threshold: int = 2) -> int | None:
    """
    判断新消息关键词是否匹配已有话题。
    返回匹配的话题ID，无匹配返回None。
    """
    new_set = set(new_keywords)
    best_match = None
    best_score = 0

    for topic in existing_topics:
        try:
            topic_kw = json.loads(topic.get("keywords", "[]"))
        except Exception:
            topic_kw = []
        if not topic_kw:
            continue
        overlap = len(new_set & set(topic_kw))
        if overlap >= threshold and overlap > best_score:
            best_score = overlap
            best_match = topic.get("id")

    return best_match


async def process_unprocessed_for_topics(group_id: int, batch_size: int = 50) -> dict:
    """
    本地处理未分析的消息，提取/更新话题（0 token）。

    Returns: {"processed": int, "new_keywords": list, "matched_topic": int|None}
    """
    messages = await db.get_unprocessed_messages(group_id, limit=batch_size)
    if not messages:
        return {"processed": 0, "new_keywords": [], "matched_topic": None}

    # 提取所有文本
    texts = [m.get("content", "") for m in messages if m.get("content")]
    if not texts:
        msg_ids = [m["id"] for m in messages]
        await db.mark_messages_processed(msg_ids)
        return {"processed": len(messages), "new_keywords": [], "matched_topic": None}

    # 提取关键词
    keywords = extract_topic_keywords(texts, top_k=8)

    # 获取已有活跃话题
    existing = await db.get_active_topics(group_id)

    # 匹配话题
    matched_id = match_existing_topic(keywords, existing)

    if matched_id:
        # 更新已有话题
        for t in existing:
            if t["id"] == matched_id:
                try:
                    old_kw = json.loads(t.get("keywords", "[]"))
                except Exception:
                    old_kw = []
                merged = list(dict.fromkeys(old_kw + keywords))[:10]
                await db.upsert_topic(group_id, t.get("topic_name", ""), merged)
                break
    else:
        # 创建新话题（用第一个关键词作为话题名）
        topic_name = keywords[0] if keywords else "闲聊"
        await db.upsert_topic(group_id, topic_name, keywords)

    # 标记已处理
    msg_ids = [m["id"] for m in messages]
    await db.mark_messages_processed(msg_ids)

    return {
        "processed": len(messages),
        "new_keywords": keywords,
        "matched_topic": matched_id,
    }


async def generate_topic_summary(group_id: int, ai_client) -> str:
    """
    调用AI生成话题摘要（低token，每30分钟一次）。
    覆盖最多5个活跃话题，为每个话题生成独立摘要。
    Returns: 最新的话题摘要
    """
    topics = await db.get_active_topics(group_id)
    if not topics:
        return ""

    recent_msgs = await db.get_recent_messages(group_id, limit=30)
    if not recent_msgs:
        return ""

    # 为每个活跃话题单独生成摘要（最多5个）
    last_summary = ""
    for t in topics[:5]:
        try:
            kw = json.loads(t.get("keywords", "[]"))
        except Exception:
            kw = []
        kw_str = "、".join(kw[:5])
        topic_name = t.get("topic_name", "未知")

        # 从消息中筛选与该话题关键词相关的内容
        relevant_msgs = []
        for m in recent_msgs:
            content = m.get("content", "")
            if any(k in content for k in kw[:3]):
                relevant_msgs.append(m)
        if not relevant_msgs:
            relevant_msgs = recent_msgs[-10:]

        msg_sample = "\n".join(
            f"{m.get('nickname', '?')}: {m.get('content', '')[:50]}"
            for m in relevant_msgs[-10:]
        )

        prompt = f"""分析以下群聊话题，用一段话（40字以内）总结：

话题名：{topic_name}
关键词：{kw_str}
相关消息：
{msg_sample}

要求：总结这个话题的讨论内容和氛围。只输出摘要。"""

        try:
            messages = [{"role": "user", "content": prompt}]
            summary, tokens = await ai_client.chat_for_learning(messages)
            if summary:
                summary = summary.strip()[:150]
                await db.update_topic_summary(t["id"], summary)
                last_summary = summary
        except Exception as e:
            logger.error(f"[TopicSummary Error] topic={topic_name}: {e}")

    return last_summary


async def cleanup_old_topics(group_id: int, max_archived: int = 10):
    """归档旧话题，保留最近的活跃话题"""
    topics = await db.get_active_topics(group_id)
    active = [t for t in topics if t.get("status") == "active"]
    fading = [t for t in topics if t.get("status") == "fading"]

    # 超过15个话题，将最旧的归档
    if len(active) > max_archived:
        to_archive = active[max_archived:]
        for t in to_archive:
            # 简单处理：在数据库中标记（这里暂用summary字段标记）
            pass
