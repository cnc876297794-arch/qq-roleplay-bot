"""
group_admin 关键词过滤模块

自动监听群消息，匹配关键词并执行相应操作。
独立于命令系统，通过 on_message 注册，优先级高于角色扮演。
"""

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.log import logger

from . import database as db
from .config import AdminConfig

config = AdminConfig()

# 关键词过滤器：priority=8，高于角色扮演的 priority=10
keyword_filter = on_message(priority=8, block=False)


@keyword_filter.handle()
async def handle_keyword_filter(bot: Bot, event: GroupMessageEvent):
    """自动检查群消息是否包含关键词"""
    if not config.is_feature_enabled("keyword_filter"):
        return

    # 检查该群是否开启关键词过滤
    settings = await db.get_group_settings(event.group_id)
    if settings and not settings.get("keyword_filter", 1):
        return

    # 提取文本内容
    text = ""
    for seg in event.message:
        if seg.type == "text":
            text += seg.data.get("text", "")

    if not text:
        return

    # 检查关键词匹配
    matched = await db.check_keyword_match(event.group_id, text)
    if not matched:
        return

    keyword = matched["keyword"]
    action = matched["action"]
    ban_duration = matched.get("ban_duration", 600)

    logger.info(f"[KeywordFilter] 群{event.group_id} 用户{event.user_id} 触发关键词: {keyword} → 动作: {action}")

    try:
        # 判断是否需要撤回
        need_delete = action in ("delete", "delete_ban")
        # 判断是否需要禁言
        need_ban = action in ("ban", "delete_ban")
        # 判断是否需要警告
        need_warn = action == "warn"

        delete_success = False
        ban_success = False

        # 1. 撤回消息
        if need_delete:
            try:
                await bot.delete_msg(message_id=event.message_id)
                delete_success = True
                await db.log_admin_action(
                    group_id=event.group_id,
                    admin_id=0,
                    admin_role="system",
                    action="keyword_delete",
                    target_id=event.user_id,
                    reason=f"触发关键词: {keyword}"
                )
                logger.info(f"[KeywordFilter] 撤回消息成功: {event.message_id}")
            except Exception as e:
                logger.warning(f"[KeywordFilter] 撤回消息失败: {event.message_id} - {e}")

        # 2. 禁言
        if need_ban:
            try:
                await bot.set_group_ban(
                    group_id=event.group_id,
                    user_id=event.user_id,
                    duration=ban_duration
                )
                ban_success = True
                await db.log_admin_action(
                    group_id=event.group_id,
                    admin_id=0,
                    admin_role="system",
                    action="keyword_ban",
                    target_id=event.user_id,
                    duration=ban_duration,
                    reason=f"触发关键词: {keyword}"
                )
            except Exception as e:
                logger.warning(f"[KeywordFilter] 禁言失败: {e}")

        # 3. 发送提示消息
        if action == "delete_ban":
            if delete_success and ban_success:
                await keyword_filter.send(f"⚠️ 检测到敏感词「{keyword}」，已撤回并禁言 {ban_duration // 60} 分钟")
            elif ban_success:
                await keyword_filter.send(f"⚠️ 检测到敏感词「{keyword}」，已禁言 {ban_duration // 60} 分钟")
            elif delete_success:
                await keyword_filter.send(f"⚠️ 检测到敏感词「{keyword}」，已撤回消息")
            else:
                await keyword_filter.send(f"⚠️ 检测到敏感词「{keyword}」，处理失败")

        elif action == "delete":
            if delete_success:
                # 静默撤回，不发送提示（避免刷屏）
                pass
            else:
                logger.warning("[KeywordFilter] 撤回失败，未发送提示")

        elif action == "ban":
            if ban_success:
                await keyword_filter.send(f"⚠️ 检测到敏感词「{keyword}」，已禁言 {ban_duration // 60} 分钟")

        elif action == "warn":
            await keyword_filter.send(f"⚠️ {event.sender.nickname or event.user_id}，请注意言辞，检测到敏感词「{keyword}」")
            await db.log_admin_action(
                group_id=event.group_id,
                admin_id=0,
                admin_role="system",
                action="keyword_warn",
                target_id=event.user_id,
                reason=f"触发关键词: {keyword}"
            )

    except Exception as e:
        logger.error(f"[KeywordFilter Error] {e}")
