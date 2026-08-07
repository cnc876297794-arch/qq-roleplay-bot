"""
message_processor.py - 消息预处理器

本地处理群聊消息（0 token消耗）：
- 存入SQLite
- 提取关键词（jieba分词）
- 更新成员活跃度统计
- 判断消息类型（文本/图片/@）
- 话题相关性检测
- @昵称解析

支持 SillyTavern 角色卡的 mes_example 注入（few-shot 学习）。
"""

import re
import time
from collections import Counter
from typing import Optional

import jieba

from . import database as db

# 中文停用词（高频无意义词）
_STOP_WORDS = {
    "的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "没有", "看", "好", "自己", "这", "他", "她", "它", "们", "那", "被",
    "从", "把", "还", "个", "之", "而", "与", "但", "如", "所", "或",
    "能", "可以", "吗", "吧", "啊", "呢", "嗯", "哦", "哈", "呀",
    "什么", "怎么", "为什么", "哪", "谁", "这个", "那个", "这些", "那些",
    "过", "下", "来", "多", "做", "用", "时", "后", "前", "里", "中",
    "大", "小", "让", "给", "对", "更", "比", "等", "没", "已经",
    "其实", "可能", "应该", "真是", "觉得", "知道", "时候", "现在",
    "如果", "因为", "所以", "虽然", "但是", "然后", "还是", "或者",
}


async def extract_text_from_message(message, group_id: int = None, bot=None) -> str:
    """
    从OneBot消息段中提取纯文本，并优化@处理。

    改进：
    1. @qq号 -> 尝试从数据库查找昵称，转换为@昵称
    2. 保留原始文本结构
    3. reply 段 -> 反查原始消息内容，拼成「[回复 XXX 说: ...]」格式
       - 用群昵称；群昵称为空/纯空格时，回退用 user_id 字符串
    """
    if isinstance(message, str):
        return message
    parts = []
    for seg in message:
        if seg.type == "text":
            text = seg.data.get("text", "")
            # 优化：将 @qq号 替换为 @昵称（如果知道群号）
            if group_id:
                text = await _replace_at_qq_with_nickname(text, group_id)
            parts.append(text)
        elif seg.type == "at":
            qq = seg.data.get('qq', '')
            try:
                qq_int = int(qq) if qq else 0
            except (TypeError, ValueError):
                qq_int = 0
            # 用 resolve_display_name：群备注优先（含空格/空值的兜底）
            nick = await resolve_display_name(group_id, qq_int)
            parts.append(f"@{nick}")
        elif seg.type == "face":
            parts.append("[表情]")
        elif seg.type == "mface":
            name = seg.data.get("name", "")
            parts.append(f"[表情{' ' + name if name else ''}]")
        elif seg.type == "image":
            parts.append("[图片]")
        elif seg.type == "reply":
            # 反查原始消息内容（如果数据库里有）
            reply_text = await _resolve_reply_segment(seg, group_id)
            parts.append(reply_text)
    return "".join(parts).strip()


async def _resolve_reply_segment(seg, group_id: int | None) -> str:
    """
    把 reply 段展开成「[回复 XXX 说: ...]」。
    找不到原始消息时，退回到「[回复]」。

    群昵称为空 / 纯空格时，回退使用「群友(user_id)」格式。
    """
    if not group_id:
        return "[回复]"

    # OneBot v11 reply 段：data 通常包含 id (message_id) 字段
    raw_id = seg.data.get("id") or seg.data.get("message_id")
    if raw_id is None:
        return "[回复]"
    try:
        target_msg_id = int(raw_id)
    except (TypeError, ValueError):
        return "[回复]"

    try:
        original = await db.get_message_by_id(group_id, target_msg_id)
    except Exception:
        original = None
    if not original:
        return "[回复]"

    # 优先用最新的群备注（DB 里可能比原消息存的更准）
    nick = await resolve_display_name(
        group_id,
        original.get("user_id", 0),
        sender_nickname=original.get("nickname"),
    )
    text = (original.get("content") or "").strip()
    if not text:
        return f"[回复 {nick} 的消息]"
    # 防止 reply 链过长：截断到 80 字
    if len(text) > 80:
        text = text[:80] + "…"
    return f"[回复 {nick} 说: {text}]"


async def resolve_display_name(
    group_id: int,
    user_id: int,
    sender_nickname: str | None = None,
) -> str:
    """
    解析"显示名"（群备注优先）。

    优先级：
    1. event.sender.nickname（OneBot 实时给的群昵称）—— 如果非空且非纯空白
    2. member_profiles.nickname（数据库存的最新群备注）—— 如果非空且非纯空白
    3. 兜底：f"群友({user_id})"

    注意：
    - "纯空白" 也会触发兜底（处理群里"备注=空格"的人）
    - 如果数据库里也没记录，会走第 3 步兜底
    """
    def _is_valid(s: str | None) -> bool:
        return bool(s) and s.strip() != ""

    if _is_valid(sender_nickname):
        return sender_nickname.strip()

    if group_id and user_id:
        try:
            profile = await db.get_member_profile(group_id, user_id)
            if profile:
                db_nick = profile.get("nickname")
                if _is_valid(db_nick):
                    return db_nick.strip()
        except Exception:
            pass

    return f"群友({user_id})"


async def _replace_at_qq_with_nickname(text: str, group_id: int) -> str:
    """
    将文本中的 @qq号 替换为 @昵称（用群备注）。
    支持 @123456 和 @123456789 格式。
    """
    # 匹配 @数字 模式
    at_pattern = re.compile(r'@(\d{5,12})')

    async def replace_match(match):
        qq_str = match.group(1)
        try:
            qq = int(qq_str)
            profile = await db.get_member_profile(group_id, qq)
            if profile and profile.get('nickname'):
                return f"@{profile['nickname']}"
        except Exception:
            pass
        return match.group(0)  # 保持原样

    # 由于正则替换是同步的，我们需要先找出所有匹配
    matches = at_pattern.findall(text)
    if not matches:
        return text

    # 逐个替换
    result = text
    for qq_str in set(matches):
        try:
            qq = int(qq_str)
            profile = await db.get_member_profile(group_id, qq)
            if profile and profile.get('nickname'):
                result = result.replace(f"@{qq_str}", f"@{profile['nickname']}")
        except Exception:
            pass

    return result


# 兼容旧版本的同步调用（不带group_id时）
def extract_text_from_message_sync(message) -> str:
    """同步版本，不处理@昵称替换、不反查 reply"""
    if isinstance(message, str):
        return message
    parts = []
    for seg in message:
        if seg.type == "text":
            parts.append(seg.data.get("text", ""))
        elif seg.type == "at":
            parts.append(f"@{seg.data.get('qq', '')}")
        elif seg.type == "face":
            parts.append("[表情]")
        elif seg.type == "mface":
            name = seg.data.get("name", "")
            parts.append(f"[表情{' ' + name if name else ''}]")
        elif seg.type == "image":
            parts.append("[图片]")
        elif seg.type == "reply":
            parts.append("[回复]")
    return "".join(parts).strip()


def classify_message(text: str) -> str:
    """分类消息类型"""
    if re.search(r'\[图片\]', text):
        if text.replace("[图片]", "").strip():
            return "text_with_image"
        return "image"
    if re.search(r'\[表情\]', text):
        return "emoji"
    if re.search(r'@\d+', text):
        return "at"
    return "text"


def extract_keywords(text: str, top_k: int = 10) -> list[str]:
    """使用jieba提取关键词（本地处理，0 token）"""
    # 清理文本
    clean = re.sub(r'@\d+', '', text)
    clean = re.sub(r'\[.*?\]', '', clean)  # 去除[图片][表情]等
    clean = re.sub(r'https?://\S+', '', clean)  # 去除链接
    clean = re.sub(r'[^\w\u4e00-\u9fff]+', ' ', clean)  # 保留中英文和数字

    if not clean.strip():
        return []

    words = jieba.cut(clean)
    word_count = Counter()

    for w in words:
        w = w.strip()
        if len(w) < 2:
            continue
        if w in _STOP_WORDS:
            continue
        word_count[w] += 1

    return [w for w, _ in word_count.most_common(top_k)]


def count_punctuation(text: str) -> int:
    """统计特殊标点符号使用量"""
    special = re.findall(r'[！？～~…。、，；：""''【】（）♡♥❤💕✨🌟⭐🔥😊😂🤣😭😘🥰]', text)
    return len(special)


def count_emoticons(text: str) -> int:
    """统计颜文字和emoji"""
    kaomoji = re.findall(r'\([^)]*\)|\[[^\]]*\]|₍[^₎]*₎|₍[^₎]*₎', text)
    emoji = re.findall(r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F900-\U0001F9FF]', text)
    return len(kaomoji) + len(emoji)


def analyze_message(text: str) -> dict:
    """完整分析一条消息（全部本地处理，0 token）"""
    return {
        "length": len(text),
        "word_count": len(text.split()),
        "keywords": extract_keywords(text, top_k=5),
        "message_type": classify_message(text),
        "punctuation_count": count_punctuation(text),
        "emoticon_count": count_emoticons(text),
        "has_question": "?" in text or "？" in text or "吗" in text or "呢" in text,
        "is_short": len(text) < 10,
        "is_long": len(text) > 50,
        "hour": time.localtime().tm_hour,
    }


async def process_group_message(group_id: int, user_id: int, message_id: int,
                                 content: str, nickname: str) -> dict | None:
    """
    处理一条群聊消息（0 token消耗）。

    1. 存入SQLite
    2. 更新成员档案基础统计
    3. 返回消息分析结果
    """
    # 分析消息
    analysis = analyze_message(content)

    # 存入数据库
    msg_type = analysis["message_type"]
    await db.save_message(group_id, user_id, message_id, content, msg_type, nickname)

    # 更新成员档案
    await db.upsert_member_profile(group_id, user_id, nickname)
    await db.update_member_stats(
        group_id, user_id,
        message_length=analysis["length"],
        words=analysis["word_count"],
        punctuation_count=analysis["punctuation_count"]
    )

    return analysis


def build_reply_context_messages(character_prompt: str, group_id: int,
                                  topic_summary: str = "",
                                  active_members: list = None,
                                  recent_messages: list = None,
                                  current_nickname: str = "",
                                  current_text: str = "",
                                  current_user_id: int = 0,
                                  mes_example: list = None,
                                  ai_history: list = None,
                                  group_character_override: str = "",
                                  mentioned_member_info: dict = None,
                                  consciousness_context: str = "") -> list[dict]:
    """
    构建精简的AI回复上下文（优化 DeepSeek KV Cache 命中率）。

    缓存优化策略：
    - System prompt 保持固定，不包含任何动态内容
    - 动态内容（话题、成员信息）放到单独的 user message 中
    - 示例对话放在 system prompt 之后，保持固定
    - AI 历史和群聊消息放在末尾（动态部分）

    消息顺序：
    1. System prompt（固定：角色设定，~300 token）
    2. Context message（动态：群聊场景补充）
    3. mes_example（固定：示例对话，few-shot 学习）
    4. AI 自己之前的回复（对话记忆，保证连贯性）
    5. 最近群聊消息（动态）
    6. 当前消息（动态）

    Returns: list of {role, content} dicts for chat API
    """
    messages = []

    # 1. System prompt（固定，确保缓存命中）
    messages.append({"role": "system", "content": character_prompt.strip()})

    # 2. Context message（将动态内容放到 user message 中，不影响 system prompt 缓存）
    context_parts = []
    if group_character_override:
        context_parts.append(f"在群聊中，你还需要展现{group_character_override}的特质。")
    if topic_summary:
        context_parts.append(f"当前群聊话题：{topic_summary}")
    if consciousness_context:
        context_parts.append(consciousness_context)

    # 解析成员标签的辅助函数
    def _parse_tags(m):
        tags = m.get("personality_tags", "")
        if isinstance(tags, str):
            try:
                import json
                tags = json.loads(tags) if tags else []
            except Exception:
                tags = []
        return tags

    # 如果有被@的成员，重点介绍
    if mentioned_member_info:
        nm = mentioned_member_info
        nm_tags = _parse_tags(nm)
        nm_style = nm.get("speak_style", "")
        nm_phrases = nm.get("common_phrases", [])
        if isinstance(nm_phrases, str):
            try:
                import json
                nm_phrases = json.loads(nm_phrases) if nm_phrases else []
            except Exception:
                nm_phrases = []

        nm_name = nm.get("nickname", "未知")
        desc_parts = []
        if nm_tags:
            desc_parts.append("、".join(nm_tags[:5]))
        if nm_style:
            desc_parts.append(f"说话风格{nm_style}")
        if nm_phrases:
            desc_parts.append(f"口头禅「{'、'.join(nm_phrases[:3])}」")
        desc_str = "，".join(desc_parts) if desc_parts else "你对Ta还不太了解"

        context_parts.append(f"你认识{nm_name}，{nm_name}{desc_str}。")

    # 群友列表（作为补充背景）
    if active_members:
        member_lines = []
        for m in active_members:
            tags = _parse_tags(m)
            tag_str = "、".join(tags[:3]) if tags else "普通群友"
            member_lines.append(f"- {m.get('nickname', '未知')}: {tag_str}")
        if member_lines:
            context_parts.append("群友性格特征：\n" + "\n".join(member_lines))

    if context_parts:
        messages.append({"role": "user", "content": "\n\n".join(context_parts)})

    # 3. 示例对话（固定，能被缓存）
    if mes_example:
        # 限制示例对话数量，避免超长上下文
        for ex in mes_example[:6]:  # 最多6轮示例，节省token
            messages.append(ex)

    # 4. AI 自己之前的回复（对话记忆，保证连贯性）
    if ai_history:
        # 最多保留最近 2 轮对话（4条消息：2条user + 2条assistant）
        for entry in ai_history[-4:]:
            if entry.get("role") == "user":
                text = entry.get("content", "")
                # 使用历史消息中保存的昵称，而不是当前发言者的昵称
                entry_nickname = entry.get("nickname", current_nickname)
                if text and not text.startswith(f"{entry_nickname}:"):
                    messages.append({"role": "user", "content": f"{entry_nickname}: {text}"})
                elif text:
                    messages.append(entry)
            else:
                messages.append(entry)

    # 5. 最近群聊消息（动态，但只取与当前话题相关的）
    if recent_messages:
        # 过滤：只保留与当前消息相关的历史消息
        # 使用传入的 related_messages 参数（如果可用）
        display_msgs = recent_messages[-2:] if len(recent_messages) <= 2 else recent_messages[-2:]
        for msg in display_msgs:
            nick = msg.get("nickname", "某人")
            text = msg.get("content", "")
            # 如果内容已经包含昵称前缀，就不再添加
            if text and not text.startswith(f"{nick}:"):
                messages.append({"role": "user", "content": f"{nick}: {text}"})
            elif text:
                messages.append({"role": "user", "content": text})

    # 6. 当前消息（动态）
    if current_nickname and current_text:
        # 如果内容已经包含昵称前缀，就不再添加
        if not current_text.startswith(f"{current_nickname}:"):
            messages.append({"role": "user", "content": f"{current_nickname}: {current_text}"})
        else:
            messages.append({"role": "user", "content": current_text})

    return messages


# ════════════════════════════════════════════════════════
#  话题相关性工具（新增）
# ════════════════════════════════════════════════════════

def get_message_keywords(text: str, top_k: int = 8) -> set[str]:
    """
    提取消息的关键词集合（用于话题相关性比较）。
    返回去重后的关键词集合。
    """
    if not text:
        return set()

    # 清理文本
    clean = re.sub(r'@\S+', '', text)
    clean = re.sub(r'\[.*?\]', '', clean)
    clean = re.sub(r'https?://\S+', '', clean)
    clean = re.sub(r'[^\w\u4e00-\u9fff]+', ' ', clean)

    if not clean.strip():
        return set()

    words = jieba.cut(clean)
    keywords = set()

    for w in words:
        w = w.strip()
        if len(w) < 2:
            continue
        if w in _STOP_WORDS:
            continue
        keywords.add(w)

    return keywords


def is_topic_related(current_text: str, history_text: str, min_overlap: int = 1) -> bool:
    """
    判断两条消息是否属于同一话题。

    策略：
    1. 提取两者关键词
    2. 计算交集
    3. 如果有 min_overlap 个以上共同词，认为是相关话题

    特殊情况：
    - 如果历史消息中包含当前消息的关键词，也认为相关
    - 如果历史消息是当前消息的直接回复（包含@当前用户），也相关
    """
    if not current_text or not history_text:
        return False

    current_kw = get_message_keywords(current_text, top_k=12)
    history_kw = get_message_keywords(history_text, top_k=12)

    if not current_kw or not history_kw:
        return False

    overlap = len(current_kw & history_kw)
    return overlap >= min_overlap


def detect_topic_shift(current_text: str, history_messages: list[dict], min_related: int = 1) -> bool:
    """
    检测当前消息是否代表话题切换。

    如果历史消息中至少有 min_related 条与当前消息相关，
    则认为不是话题切换（继续旧话题）。

    返回：True = 话题切换（历史消息不相关），False = 继续旧话题
    """
    if not history_messages:
        return True  # 没有历史，视为话题切换（避免引入旧话题消息）

    related_count = 0
    for msg in history_messages:
        text = msg.get("content", "")
        if is_topic_related(current_text, text):
            related_count += 1

    return related_count < min_related


def filter_related_messages(current_text: str, messages: list[dict], max_results: int = 3) -> list[dict]:
    """
    从消息列表中筛选出与当前消息话题相关的消息。

    策略：
    1. 先筛选出话题相关的消息
    2. 如果结果太少（<2条），则补充最近的2条作为背景
    3. 限制返回数量
    """
    if not messages:
        return []

    related = []
    for msg in messages:
        text = msg.get("content", "")
        if is_topic_related(current_text, text):
            related.append(msg)

    # 如果相关消息太少，不补充背景消息（避免不相关消息污染上下文）
    # 只保留真正相关的消息
    return related[-max_results:] if len(related) > max_results else related
