"""
group_admin 插件 - QQ群管理员功能

统一命令格式：/admin <子命令> [参数]

功能：
- 基础群管：禁言/解除禁言/踢出/全员禁言
- 消息管理：撤回消息
- 数据统计：活跃排行/发言统计/群信息
- Bot控制：开启/关闭/调整概率/查看状态
- 关键词过滤：自动检测并处理

与 roleplay 插件完全解耦，无冲突。
"""

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.log import logger
from nonebot.params import CommandArg

from . import database as db
from .config import AdminConfig
from .permission import can_target, require_admin
from .utils import extract_at_users, format_duration

# ─── 初始化 ───────────────────────────────────
config = AdminConfig()

# ─── 统一命令入口 ─────────────────────────────
admin_cmd = on_command("admin", priority=5, block=True)


# ─── 主命令分发 ─────────────────────────────────
@admin_cmd.handle()
async def handle_admin(bot: Bot, event: GroupMessageEvent, args=CommandArg()):
    """统一处理 /admin 命令"""
    # 所有 /admin 命令都需要管理员权限
    logger.info(f"[Admin] 收到命令 from {event.user_id}, role={event.sender.role}")
    if not require_admin_check(event):
        logger.info(f"[Admin] 权限不足: {event.user_id} 不是管理员")
        return  # 非管理员静默忽略
    logger.info(f"[Admin] 权限通过: {event.user_id}")

    text = args.extract_plain_text().strip()
    if not text:
        await show_help(bot, event)
        return

    # 解析子命令
    parts = text.split(None, 1)
    sub_cmd = parts[0].lower()
    sub_args = parts[1] if len(parts) > 1 else ""

    # 子命令分发
    handlers = {
        # 基础群管
        "禁言": handle_ban_cmd,
        "解除禁言": handle_unban_cmd,
        "解禁": handle_unban_cmd,
        "踢出": handle_kick_cmd,
        "全员禁言": handle_whole_ban_cmd,
        "解除全员禁言": handle_whole_unban_cmd,
        "撤回": handle_delete_msg_cmd,

        # 数据统计
        "活跃排行": handle_active_rank_cmd,
        "活跃榜": handle_active_rank_cmd,
        "发言统计": handle_user_stats_cmd,
        "群信息": handle_group_info_cmd,

        # Bot控制
        "bot开启": handle_bot_on_cmd,
        "bot关闭": handle_bot_off_cmd,
        "bot概率": handle_bot_prob_cmd,
        "bot状态": handle_bot_status_cmd,

        # 关键词
        "关键词": handle_keyword_cmd,
    }

    handler = handlers.get(sub_cmd)
    if handler:
        await handler(bot, event, sub_args)
    else:
        # 未识别的命令，显示帮助
        await show_help(bot, event)


# ─── 帮助 ────────────────────────────────────
async def show_help(bot: Bot, event: GroupMessageEvent, _=""):
    """显示帮助信息"""
    lines = [
        "🛠️ 群管理命令帮助",
        "",
        "📋 基础群管：",
        "  /admin 禁言 @用户 10m    - 禁言用户（支持 m/h/d）",
        "  /admin 解除禁言 @用户      - 解除禁言",
        "  /admin 踢出 @用户          - 移出群成员",
        "  /admin 全员禁言            - 开启全员禁言",
        "  /admin 解除全员禁言        - 关闭全员禁言",
        "  /admin 撤回（回复消息）    - 撤回消息",
        "",
        "📊 数据统计：",
        "  /admin 活跃排行 10         - 活跃成员排行",
        "  /admin 发言统计 @用户      - 用户发言统计",
        "  /admin 群信息              - 群基本信息",
        "",
        "🤖 Bot控制：",
        "  /admin bot开启             - 开启Bot回复",
        "  /admin bot关闭             - 关闭Bot回复",
        "  /admin bot概率 0.05        - 设置回复概率",
        "  /admin bot状态             - 查看Bot状态",
        "",
        "🔑 关键词过滤：",
        "  /admin 关键词 列表         - 查看关键词",
        "  /admin 关键词 添加 测试 delete_ban 300",
        "  /admin 关键词 删除 测试",
        "",
        "动作说明：",
        "  delete      → 仅撤回消息",
        "  ban         → 仅禁言",
        "  delete_ban  → 撤回+禁言",
        "  warn        → 仅警告",
    ]
    await bot.send(event, "\n".join(lines))


# ─── 禁言 ─────────────────────────────────────
async def handle_ban_cmd(bot: Bot, event: GroupMessageEvent, args: str):
    if not config.is_feature_enabled("ban"):
        await bot.send(event, "❌ 禁言功能未开启")
        return

    if not require_admin_check(event):
        await bot.send(event, "❌ 权限不足，仅群主/管理员可执行此操作")
        return

    users = extract_at_users(event.message)
    if not users:
        await bot.send(event, "❌ 请 @ 要禁言的用户")
        return

    # 解析时间参数
    duration = parse_duration(args) if args else 600
    target_id = users[0]

    if not can_target(event.sender.role, target_id, bot, event.group_id):
        await bot.send(event, "❌ 权限不足，无法对该用户执行此操作")
        return

    try:
        await bot.set_group_ban(
            group_id=event.group_id,
            user_id=target_id,
            duration=duration
        )
        await db.log_admin_action(
            group_id=event.group_id,
            admin_id=event.user_id,
            admin_role=event.sender.role,
            action="ban",
            target_id=target_id,
            duration=duration
        )
        await bot.send(event, f"✅ 已禁言 {target_id}，时长：{format_duration(duration)}")
    except Exception as e:
        logger.error(f"[Ban Error] {e}")
        await bot.send(event, f"❌ 禁言失败：{e}")


# ─── 解除禁言 ─────────────────────────────────
async def handle_unban_cmd(bot: Bot, event: GroupMessageEvent, args: str):
    if not config.is_feature_enabled("ban"):
        await bot.send(event, "❌ 禁言功能未开启")
        return

    if not require_admin_check(event):
        await bot.send(event, "❌ 权限不足，仅群主/管理员可执行此操作")
        return

    users = extract_at_users(event.message)
    if not users:
        await bot.send(event, "❌ 请 @ 要解除禁言的用户")
        return

    target_id = users[0]

    try:
        await bot.set_group_ban(
            group_id=event.group_id,
            user_id=target_id,
            duration=0
        )
        await db.log_admin_action(
            group_id=event.group_id,
            admin_id=event.user_id,
            admin_role=event.sender.role,
            action="unban",
            target_id=target_id
        )
        await bot.send(event, f"✅ 已解除 {target_id} 的禁言")
    except Exception as e:
        logger.error(f"[Unban Error] {e}")
        await bot.send(event, f"❌ 解除禁言失败：{e}")


# ─── 踢出 ─────────────────────────────────────
async def handle_kick_cmd(bot: Bot, event: GroupMessageEvent, args: str):
    if not config.is_feature_enabled("kick"):
        await bot.send(event, "❌ 踢出功能未开启")
        return

    if not require_admin_check(event):
        await bot.send(event, "❌ 权限不足，仅群主/管理员可执行此操作")
        return

    users = extract_at_users(event.message)
    if not users:
        await bot.send(event, "❌ 请 @ 要踢出的用户")
        return

    target_id = users[0]

    if not can_target(event.sender.role, target_id, bot, event.group_id):
        await bot.send(event, "❌ 权限不足，无法对该用户执行此操作")
        return

    try:
        await bot.set_group_kick(
            group_id=event.group_id,
            user_id=target_id,
            reject_add_request=False
        )
        await db.log_admin_action(
            group_id=event.group_id,
            admin_id=event.user_id,
            admin_role=event.sender.role,
            action="kick",
            target_id=target_id
        )
        await bot.send(event, f"✅ 已将 {target_id} 移出群聊")
    except Exception as e:
        logger.error(f"[Kick Error] {e}")
        await bot.send(event, f"❌ 踢出失败：{e}")


# ─── 全员禁言 ─────────────────────────────────
async def handle_whole_ban_cmd(bot: Bot, event: GroupMessageEvent, args: str):
    if not config.is_feature_enabled("whole_ban"):
        await bot.send(event, "❌ 全员禁言功能未开启")
        return

    if not require_admin_check(event):
        await bot.send(event, "❌ 权限不足，仅群主/管理员可执行此操作")
        return

    try:
        await bot.set_group_whole_ban(
            group_id=event.group_id,
            enable=True
        )
        await db.log_admin_action(
            group_id=event.group_id,
            admin_id=event.user_id,
            admin_role=event.sender.role,
            action="whole_ban"
        )
        await bot.send(event, "✅ 已开启全员禁言")
    except Exception as e:
        logger.error(f"[Whole Ban Error] {e}")
        await bot.send(event, f"❌ 全员禁言失败：{e}")


# ─── 解除全员禁言 ─────────────────────────────
async def handle_whole_unban_cmd(bot: Bot, event: GroupMessageEvent, args: str):
    if not config.is_feature_enabled("whole_ban"):
        await bot.send(event, "❌ 全员禁言功能未开启")
        return

    if not require_admin_check(event):
        await bot.send(event, "❌ 权限不足，仅群主/管理员可执行此操作")
        return

    try:
        await bot.set_group_whole_ban(
            group_id=event.group_id,
            enable=False
        )
        await db.log_admin_action(
            group_id=event.group_id,
            admin_id=event.user_id,
            admin_role=event.sender.role,
            action="whole_unban"
        )
        await bot.send(event, "✅ 已解除全员禁言")
    except Exception as e:
        logger.error(f"[Whole Unban Error] {e}")
        await bot.send(event, f"❌ 解除全员禁言失败：{e}")


# ─── 撤回 ─────────────────────────────────────
async def handle_delete_msg_cmd(bot: Bot, event: GroupMessageEvent, args: str):
    if not config.is_feature_enabled("delete_msg"):
        await bot.send(event, "❌ 撤回功能未开启")
        return

    if not require_admin_check(event):
        await bot.send(event, "❌ 权限不足，仅群主/管理员可执行此操作")
        return

    # 检查是否是回复消息
    if hasattr(event, 'reply') and event.reply:
        message_id = event.reply.message_id
        try:
            await bot.delete_msg(message_id=message_id)
            await db.log_admin_action(
                group_id=event.group_id,
                admin_id=event.user_id,
                admin_role=event.sender.role,
                action="delete_msg",
                target_id=event.reply.sender.user_id
            )
            await bot.send(event, "✅ 已撤回消息")
        except Exception as e:
            logger.error(f"[Delete Msg Error] {e}")
            await bot.send(event, f"❌ 撤回失败：{e}")
    else:
        await bot.send(event, "❌ 请回复要撤回的消息")


# ─── 活跃排行 ─────────────────────────────────
async def handle_active_rank_cmd(bot: Bot, event: GroupMessageEvent, args: str):
    if not config.is_feature_enabled("stats"):
        await bot.send(event, "❌ 数据统计功能未开启")
        return

    limit = 10
    if args.strip().isdigit():
        limit = int(args.strip())
        limit = max(1, min(limit, 50))

    try:
        members = await db.get_active_members_rank(event.group_id, limit)
        if not members:
            await bot.send(event, "📊 暂无活跃数据")
            return

        lines = [f"📊 活跃排行 Top {len(members)}"]
        for i, m in enumerate(members, 1):
            nickname = m.get("nickname", "未知")
            count = m.get("message_count", 0)
            activity = m.get("activity_level", 0)
            lines.append(f"{i}. {nickname} — {count}条消息 (活跃度: {activity:.1f})")

        await bot.send(event, "\n".join(lines))
    except Exception as e:
        logger.error(f"[Active Rank Error] {e}")
        await bot.send(event, f"❌ 获取排行失败：{e}")


# ─── 发言统计 ─────────────────────────────────
async def handle_user_stats_cmd(bot: Bot, event: GroupMessageEvent, args: str):
    if not config.is_feature_enabled("stats"):
        await bot.send(event, "❌ 数据统计功能未开启")
        return

    users = extract_at_users(event.message)
    if not users:
        target_id = event.user_id
    else:
        target_id = users[0]
        if target_id != event.user_id and event.sender.role not in ("owner", "admin"):
            await bot.send(event, "❌ 查看他人统计需要管理员权限")
            return

    try:
        profile = await db.get_member_profile(event.group_id, target_id)
        if not profile:
            await bot.send(event, "📊 该用户暂无发言记录")
            return

        nickname = profile.get("nickname", "未知")
        count = profile.get("message_count", 0)
        activity = profile.get("activity_level", 0)
        last_active = profile.get("last_active", "未知")
        style = profile.get("speak_style", "")

        lines = [
            f"📊 {nickname} 的发言统计",
            f"消息数量: {count}",
            f"活跃度: {activity:.2f}",
            f"最后活跃: {last_active}",
        ]
        if style:
            lines.append(f"说话风格: {style}")

        await bot.send(event, "\n".join(lines))
    except Exception as e:
        logger.error(f"[User Stats Error] {e}")
        await bot.send(event, f"❌ 获取统计失败：{e}")


# ─── 群信息 ───────────────────────────────────
async def handle_group_info_cmd(bot: Bot, event: GroupMessageEvent, args: str):
    if not config.is_feature_enabled("stats"):
        await bot.send(event, "❌ 数据统计功能未开启")
        return

    try:
        group_id = event.group_id
        total_members = len(await bot.get_group_member_list(group_id=group_id))
        today_msgs = await db.get_today_message_count(group_id)
        topics = await db.get_active_topics(group_id)
        settings = await db.get_group_settings(group_id)

        bot_status = "开启" if settings and settings.get("bot_enabled", 1) else "关闭"
        prob = settings.get("reply_probability", 0.02) if settings else 0.02

        lines = [
            f"📊 群 {group_id} 信息",
            f"成员数量: {total_members}",
            f"今日消息: {today_msgs}",
            f"活跃话题: {len(topics)}",
            f"Bot状态: {bot_status}",
            f"回复概率: {prob*100:.0f}%",
        ]
        if topics:
            topic_names = [t.get("topic_name", "未知") for t in topics[:3]]
            lines.append(f"热门话题: {', '.join(topic_names)}")

        await bot.send(event, "\n".join(lines))
    except Exception as e:
        logger.error(f"[Group Info Error] {e}")
        await bot.send(event, f"❌ 获取群信息失败：{e}")


# ─── Bot 开启 ─────────────────────────────────
async def handle_bot_on_cmd(bot: Bot, event: GroupMessageEvent, args: str):
    if not config.is_feature_enabled("bot_control"):
        await bot.send(event, "❌ Bot控制功能未开启")
        return

    if not require_admin_check(event):
        await bot.send(event, "❌ 权限不足，仅群主/管理员可执行此操作")
        return

    try:
        await db.set_bot_enabled(event.group_id, True)
        await bot.send(event, "✅ Bot 已开启，将恢复自动回复")
    except Exception as e:
        logger.error(f"[Bot On Error] {e}")
        await bot.send(event, f"❌ 开启失败：{e}")


# ─── Bot 关闭 ─────────────────────────────────
async def handle_bot_off_cmd(bot: Bot, event: GroupMessageEvent, args: str):
    if not config.is_feature_enabled("bot_control"):
        await bot.send(event, "❌ Bot控制功能未开启")
        return

    if not require_admin_check(event):
        await bot.send(event, "❌ 权限不足，仅群主/管理员可执行此操作")
        return

    try:
        await db.set_bot_enabled(event.group_id, False)
        await bot.send(event, "✅ Bot 已关闭，将不再自动回复（管理命令仍可用）")
    except Exception as e:
        logger.error(f"[Bot Off Error] {e}")
        await bot.send(event, f"❌ 关闭失败：{e}")


# ─── Bot 概率 ─────────────────────────────────
async def handle_bot_prob_cmd(bot: Bot, event: GroupMessageEvent, args: str):
    if not config.is_feature_enabled("bot_control"):
        await bot.send(event, "❌ Bot控制功能未开启")
        return

    if not require_admin_check(event):
        await bot.send(event, "❌ 权限不足，仅群主/管理员可执行此操作")
        return

    if not args:
        await bot.send(event, "❌ 请提供概率数值，例如：/admin bot概率 0.05")
        return

    try:
        prob = float(args.strip())
        if prob < 0 or prob > 1:
            await bot.send(event, "❌ 概率范围应为 0.0 ~ 1.0")
            return

        await db.set_reply_probability(event.group_id, prob)
        await bot.send(event, f"✅ Bot 回复概率已设置为 {prob*100:.0f}%")
    except ValueError:
        await bot.send(event, "❌ 无效的数值，请提供 0.0 ~ 1.0 之间的小数")
    except Exception as e:
        logger.error(f"[Bot Prob Error] {e}")
        await bot.send(event, f"❌ 设置失败：{e}")


# ─── Bot 状态 ─────────────────────────────────
async def handle_bot_status_cmd(bot: Bot, event: GroupMessageEvent, args: str):
    try:
        settings = await db.get_group_settings(event.group_id)
        enabled = settings.get("bot_enabled", 1) if settings else 1
        prob = settings.get("reply_probability", 0.02) if settings else 0.02

        status = "开启" if enabled else "关闭"
        lines = [
            "🤖 Bot 状态",
            f"自动回复: {status}",
            f"回复概率: {prob*100:.0f}%",
        ]
        await bot.send(event, "\n".join(lines))
    except Exception as e:
        logger.error(f"[Bot Status Error] {e}")
        await bot.send(event, f"❌ 获取状态失败：{e}")


# ─── 关键词管理 ───────────────────────────────
async def handle_keyword_cmd(bot: Bot, event: GroupMessageEvent, args: str):
    if not config.is_feature_enabled("keyword_filter"):
        await bot.send(event, "❌ 关键词过滤功能未开启")
        return

    if not require_admin_check(event):
        await bot.send(event, "❌ 权限不足，仅群主/管理员可执行此操作")
        return

    parts = args.strip().split(None, 1)
    if not parts:
        await bot.send(event, "❌ 用法：/admin 关键词 [列表/添加/删除] [参数]")
        return

    action = parts[0]
    group_id = event.group_id

    try:
        if action == "列表":
            keywords = await db.get_group_keywords(group_id)
            if not keywords:
                await bot.send(event, "📋 本群暂无关键词规则")
                return
            lines = ["📋 关键词规则列表："]
            for k in keywords:
                action_map = {
                    "delete": "仅撤回",
                    "ban": "仅禁言",
                    "delete_ban": "撤回+禁言",
                    "warn": "仅警告"
                }
                action_display = action_map.get(k['action'], k['action'])
                lines.append(f"• {k['keyword']} → {action_display} ({k['ban_duration']}秒)")
            await bot.send(event, "\n".join(lines))

        elif action == "添加":
            if len(parts) < 2:
                await bot.send(event, "❌ 用法：/admin 关键词 添加 <关键词> [动作] [时长]\n\n动作可选：delete(仅撤回), ban(仅禁言), delete_ban(撤回+禁言), warn(仅警告)")
                return
            sub_parts = parts[1].split()
            keyword = sub_parts[0]
            action_type = sub_parts[1] if len(sub_parts) > 1 else "delete_ban"
            duration = int(sub_parts[2]) if len(sub_parts) > 2 and sub_parts[2].isdigit() else 600

            # 验证动作类型
            valid_actions = ("delete", "ban", "delete_ban", "warn")
            if action_type not in valid_actions:
                await bot.send(event, f"❌ 无效动作「{action_type}」，可选：delete, ban, delete_ban, warn")
                return

            await db.add_keyword(group_id, keyword, action_type, duration)
            action_map = {
                "delete": "仅撤回",
                "ban": "仅禁言",
                "delete_ban": "撤回+禁言",
                "warn": "仅警告"
            }
            await bot.send(event, f"✅ 已添加关键词：{keyword} → {action_map.get(action_type, action_type)} ({duration}秒)")

        elif action == "删除":
            if len(parts) < 2:
                await bot.send(event, "❌ 用法：/admin 关键词 删除 <关键词>")
                return
            keyword = parts[1].split()[0]
            await db.remove_keyword(group_id, keyword)
            await bot.send(event, f"✅ 已删除关键词：{keyword}")

        else:
            await bot.send(event, "❌ 用法：/admin 关键词 [列表/添加/删除] [参数]")

    except Exception as e:
        logger.error(f"[Keyword Error] {e}")
        await bot.send(event, f"❌ 关键词操作失败：{e}")


# ─── 辅助函数 ─────────────────────────────────

def require_admin_check(event: GroupMessageEvent) -> bool:
    """检查是否为管理员"""
    return event.sender.role in ("owner", "admin")


def parse_duration(text: str) -> int:
    """解析时间字符串"""
    import re
    if not text:
        return 600

    text = text.strip().lower()
    match = re.match(r'^(\d+)([smhd])$', text)
    if not match:
        if text.isdigit():
            return int(text) * 60
        return 600

    num, unit = int(match.group(1)), match.group(2)
    multipliers = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    return num * multipliers.get(unit, 60)


# ─── 导入关键词过滤器（自动注册 on_message）──
from . import keyword_filter
