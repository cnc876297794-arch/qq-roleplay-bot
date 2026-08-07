"""
personality_engine.py - 性格学习引擎

两层架构：
- 层级1（本地，0 token）：统计特征提取
- 层级2（AI，低token）：批量标签更新
"""

import json
import time
import logging
from collections import Counter
from typing import Optional

from . import database as db
from .message_processor import extract_keywords

logger = logging.getLogger(__name__)


def extract_local_features(messages: list[dict]) -> dict:
    """从消息列表中提取本地统计特征（0 token）"""
    if not messages:
        return {}

    lengths = [len(m.get("content", "")) for m in messages]
    hours = []
    punctuation_total = 0
    question_count = 0
    word_counter = Counter()

    for m in messages:
        content = m.get("content", "")
        # 发言时间
        created = m.get("created_at", "")
        if created:
            try:
                # 尝试从timestamp提取小时
                ts = float(created) if created.replace(".", "").isdigit() else 0
                if ts > 1e9:
                    hours.append(time.localtime(ts).tm_hour)
            except Exception:
                pass
        # 标点统计
        punctuation_total += sum(1 for c in content if c in "！？～~…♡♥❤")
        # 问句统计
        if "?" in content or "？" in content or "吗" in content:
            question_count += 1
        # 高频词
        kws = extract_keywords(content, top_k=5)
        word_counter.update(kws)

    avg_length = sum(lengths) / len(lengths) if lengths else 0
    common_words = [w for w, _ in word_counter.most_common(10)]

    # 活跃时间段
    if hours:
        hour_counter = Counter(hours)
        peak_hours = [h for h, _ in hour_counter.most_common(3)]
    else:
        peak_hours = []

    return {
        "message_count": len(messages),
        "avg_length": round(avg_length, 1),
        "punctuation_per_msg": round(punctuation_total / len(messages), 2) if messages else 0,
        "question_ratio": round(question_count / len(messages), 2) if messages else 0,
        "common_words": common_words,
        "peak_hours": peak_hours,
        "long_ratio": round(sum(1 for l in lengths if l > 50) / len(lengths), 2) if lengths else 0,
        "short_ratio": round(sum(1 for l in lengths if l < 10) / len(lengths), 2) if lengths else 0,
    }


# 矛盾标签组：同一组内的标签互斥，只保留一个
_CONTRADICTION_GROUPS = [
    {"话少", "话多", "话痨"},
    {"温柔", "毒舌", "杠精"},
    {"潜水员", "话痨", "话多"},
]


def _resolve_contradictions(tags: list[str]) -> list[str]:
    """
    去除矛盾标签。对于每组互斥标签，只保留第一个出现的。
    """
    result = []
    seen_groups = set()
    for tag in tags:
        # 找到这个标签所属的矛盾组
        group_key = None
        for g in _CONTRADICTION_GROUPS:
            if tag in g:
                group_key = frozenset(g)
                break
        if group_key:
            if group_key in seen_groups:
                continue  # 这个组已经有过标签了，跳过
            seen_groups.add(group_key)
        result.append(tag)
    return result


def infer_tags_from_features(features: dict) -> dict:
    """
    从统计特征推断性格标签（本地推断，0 token）。
    作为AI分析的补充/快速版本。
    """
    tags = []
    style = "普通"
    emotion = "neutral"

    # 说话风格（互斥）
    if features.get("short_ratio", 0) > 0.6:
        style = "简短"
        tags.append("话少")
    elif features.get("long_ratio", 0) > 0.4:
        style = "详细"
        tags.append("话多")

    # 标点使用 → 情感
    punct = features.get("punctuation_per_msg", 0)
    if punct > 3:
        tags.append("表情丰富")
    if punct > 5:
        tags.append("热情")

    # 问句比例 → 好奇心
    if features.get("question_ratio", 0) > 0.3:
        tags.append("好奇心强")

    # 高频词推断兴趣
    kw_set = set(features.get("common_words", []))
    game_words = {"游戏", "王者", "原神", "LOL", "steam", "抽卡", "攻略", "段位", "崩铁",
                  "星铁", "铁道", "鸣潮", "绝区零", "方舟", "碧蓝", "明日方舟", "崩坏",
                  "提瓦特", "深渊", "排位", "副本", "boss", "伤害", "大招", "技能",
                  "老婆", "老公", "老婆", "老婆", "今日", "打卡", "皮肤", "限定", "卡池"}
    tech_words = {"代码", "编程", "python", "bug", "电脑", "AI", "服务器", "模型",
                  "训练", "部署", "API", "接口", "前端", "后端"}
    anime_words = {"二次元", "动漫", "番剧", "cos", "漫画", "轻小说", "米哈游",
                   "hoYo", "mihoyo", "角色", "立绘", "建模", "声优", "CV"}
    music_words = {"歌", "音乐", "听歌", "网易云", "QQ音乐", "演唱会", "专辑"}

    if kw_set & game_words:
        tags.append("游戏玩家")
    if kw_set & tech_words:
        tags.append("技术宅")
    if kw_set & anime_words:
        tags.append("二次元")
    if kw_set & music_words:
        tags.append("音乐爱好者")

    # 活跃时间段推断
    peak_hours = features.get("peak_hours", [])
    if peak_hours:
        if any(0 <= h < 6 for h in peak_hours):
            tags.append("夜猫子")
        if any(9 <= h < 12 for h in peak_hours):
            tags.append("早起党")

    # 消息频率推断
    msg_count = features.get("message_count", 0)
    if msg_count > 50:
        tags.append("话痨")
    elif msg_count < 5:
        tags.append("潜水员")

    # 情感倾向（扩展规则）
    positive_words = {
        "哈哈", "开心", "太好了", "棒", "厉害", "喜欢", "爱", "666", "nb",
        "卧槽", "牛", "绝了", "好耶", "冲", "真不错", "可以", "感谢",
        "笑死", "乐了", "好评", "真香", "赚了",
    }
    negative_words = {
        "烦", "累", "讨厌", "恶心", "垃圾", "无语", "吐了", "烦死",
        "血压", "裂开", "麻了", "摆烂", "逆天", "离谱", "坑", "亏了",
        "退游", "卸载", "举报", "投诉", "退款",
    }
    pos_count = len(kw_set & positive_words)
    neg_count = len(kw_set & negative_words)
    if pos_count > neg_count:
        emotion = "positive"
    elif neg_count > pos_count:
        emotion = "negative"

    return {
        "tags": tags[:5],
        "style": style,
        "emotion": emotion,
    }


async def update_member_personality(group_id: int, user_id: int,
                                     nickname: str, ai_client=None) -> dict:
    """
    更新单个成员的性格档案。
    本地推断（0 token）+ AI分析（低token），结果写入数据库。
    """
    profile = await db.get_member_profile(group_id, user_id)
    all_msgs = await db.get_recent_messages(group_id, limit=100)
    user_msgs = [m for m in all_msgs if m.get("user_id") == user_id]
    if not user_msgs:
        return {}
    # 本地特征提取
    features = extract_local_features(user_msgs)
    local_tags = infer_tags_from_features(features)
    existing_tags = []
    if profile:
        try:
            existing_tags = json.loads(profile.get("personality_tags", "[]")) if profile.get("personality_tags") else []
        except Exception:
            existing_tags = []
    merged_tags = _resolve_contradictions(list(dict.fromkeys(local_tags["tags"] + existing_tags)))[:5]
    await db.upsert_member_profile(group_id, user_id, nickname)
    # AI 分析
    ai_style = ai_emotion = None
    ai_phrases = ai_topics = None
    if ai_client and len(user_msgs) >= 10:
        msg_sample = "\n".join(
            f"{m.get('nickname', nickname)}: {m.get('content', '')[:80]}"
            for m in user_msgs[-15:]
        )
        prompt = f"""分析以下群聊用户的发言特征，输出JSON：
用户昵称：{nickname}
最近发言样本（最近{min(len(user_msgs), 15)}条）：
{msg_sample}
统计数据：平均消息长度{features.get('avg_length', 0)}字，问句占比{features.get('question_ratio', 0)}，高频词{features.get('common_words', [])[:8]}
输出格式（严格JSON）：{{"tags":["标签1","标签2"],"style":"简短/详细/幽默/严肃/温柔/毒舌/傲娇/话痨","emotion":"positive/neutral/negative","common_phrases":["口头禅1","口头禅2"],"favorite_topics":["话题1","话题2"]}}
tags从以下选择：话少、话多、话痨、表情丰富、热情、好奇心强、游戏玩家、技术宅、二次元、音乐爱好者、幽默、毒舌、傲娇、温柔、吐槽、杠精、潜水员、夜猫子、早起党。
common_phrases提取2-3个该用户反复使用的口头禅。favorite_topics提取2-3个最感兴趣的话题。只输出JSON。"""
        try:
            import re as _re
            reply, tokens = await ai_client.chat_for_learning([{"role": "user", "content": prompt}])
            if reply:
                json_match = _re.search(r'\{.*\}', reply, _re.DOTALL)
                if json_match:
                    ai_result = json.loads(json_match.group())
                    merged_tags = _resolve_contradictions(list(dict.fromkeys(ai_result.get("tags", []) + merged_tags)))[:5]
                    ai_style = ai_result.get("style")
                    ai_emotion = ai_result.get("emotion")
                    ai_phrases = ai_result.get("common_phrases", [])
                    ai_topics = ai_result.get("favorite_topics", [])
        except Exception as e:
            logger.error(f"[Personality AI Error] user={user_id}: {e}")
    # 写入数据库
    final_style = ai_style or local_tags.get("style", "普通")
    final_emotion = ai_emotion or local_tags.get("emotion", "neutral")
    local_phrases = features.get("common_words", [])[:3]
    merged_phrases = list(dict.fromkeys((ai_phrases or []) + local_phrases))[:5]
    await db.update_member_personality_tags(
        group_id=group_id, user_id=user_id,
        personality_tags=merged_tags, common_phrases=merged_phrases,
        emotional_tendency=final_emotion, speak_style=final_style,
        favorite_topics=ai_topics[:3] if ai_topics else features.get("common_words", [])[:3],
    )
    return {"tags": merged_tags, "style": final_style, "emotion": final_emotion,
            "common_phrases": merged_phrases, "features": features}


async def batch_update_personalities(group_id: int, ai_client=None,
                                      max_users: int = 5) -> int:
    """
    批量更新最近活跃成员的性格（每小时一次）。
    从最近 100 条消息中取发言最多的用户，而非按总活跃度排序。
    节省 token，确保只更新最近说过话的人。
    
    Returns: 更新的用户数
    """
    members = await db.get_recent_message_users(group_id, limit=100)
    members = members[:max_users]
    updated = 0
    
    for member in members:
        user_id = member.get("user_id")
        nickname = member.get("nickname", "未知")
        if user_id:
            try:
                await update_member_personality(group_id, user_id, nickname, ai_client)
                updated += 1
            except Exception as e:
                logger.error(f"[Personality Batch Error] user={user_id}: {e}")
    
    return updated
