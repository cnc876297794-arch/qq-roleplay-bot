"""
group_admin 工具函数

- 时间解析
- 提取 @ 用户
- 格式化输出
"""

import re
from nonebot.adapters.onebot.v11 import Message


def parse_duration(text: str) -> int:
    """
    解析时间字符串为秒数
    
    支持格式：
    - 10s -> 10秒
    - 10m -> 10分钟
    - 1h -> 1小时
    - 1d -> 1天
    
    默认：10分钟 (600秒)
    """
    if not text:
        return 600
    
    text = text.strip().lower()
    match = re.match(r'^(\d+)([smhd])$', text)
    if not match:
        # 尝试纯数字，默认按分钟
        if text.isdigit():
            return int(text) * 60
        return 600
    
    num, unit = int(match.group(1)), match.group(2)
    multipliers = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    return num * multipliers.get(unit, 60)


def format_duration(seconds: int) -> str:
    """
    将秒数格式化为易读的时间字符串
    """
    if seconds < 60:
        return f"{seconds}秒"
    elif seconds < 3600:
        return f"{seconds // 60}分钟"
    elif seconds < 86400:
        hours = seconds // 3600
        mins = (seconds % 3600) // 60
        if mins > 0:
            return f"{hours}小时{mins}分钟"
        return f"{hours}小时"
    else:
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        if hours > 0:
            return f"{days}天{hours}小时"
        return f"{days}天"


def extract_at_users(message: Message) -> list[int]:
    """
    从消息中提取所有 @ 的用户 ID
    
    Args:
        message: OneBot Message 对象
    
    Returns:
        list[int]: 用户 ID 列表
    """
    users = []
    for seg in message:
        if seg.type == "at":
            qq = seg.data.get("qq", "")
            if qq and str(qq).isdigit():
                users.append(int(qq))
    return users


def extract_at_users_from_text(text: str) -> list[int]:
    """
    从文本中提取 @ 的用户 ID（备用方法）
    """
    users = []
    # 匹配 [CQ:at,qq=123456] 格式
    matches = re.findall(r'\[CQ:at,qq=(\d+)\]', text)
    for match in matches:
        users.append(int(match))
    return users
