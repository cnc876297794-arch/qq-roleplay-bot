"""
Config hot-reload watcher for roleplay plugin.

Watches config/bot.yaml and the active character card for changes,
then reloads BotConfig and propagates updates to AIClient and ReplyDecider.
"""

import asyncio
import time
from pathlib import Path

try:
    from nonebot.log import logger
except ImportError:
    import logging
    logger = logging.getLogger("config_watcher")
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)

try:
    from watchdog.events import FileModifiedEvent, FileSystemEventHandler
    from watchdog.observers import Observer
    _WATCHDOG_AVAILABLE = True
except ImportError:
    _WATCHDOG_AVAILABLE = False
    logger.warning("[ConfigWatcher] watchdog not available, falling back to polling")

# File paths (relative to project root)
_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "bot.yaml"
_PROJECT_ROOT = _CONFIG_PATH.parent.parent

# Debounce interval (seconds) to avoid rapid reloads
_DEBOUNCE_SECONDS = 1.0

_last_reload_time: float = 0.0
_observer: Observer | None = None


class _ConfigReloadHandler(FileSystemEventHandler):
    """Watchdog handler that debounces and triggers reload."""

    def __init__(self, config, ai_client, reply_decider):
        self.config = config
        self.ai_client = ai_client
        self.reply_decider = reply_decider

    def on_modified(self, event):
        if isinstance(event, FileModifiedEvent):
            # Run the async reload in the bot's event loop
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_do_reload(self.config, self.ai_client, self.reply_decider))
            except RuntimeError:
                # No running loop; fire-and-forget with new loop
                asyncio.run(_do_reload(self.config, self.ai_client, self.reply_decider))


async def _do_reload(config, ai_client, reply_decider) -> None:
    """Perform the actual reload with debounce and update globals."""
    global _last_reload_time

    now = time.time()
    if now - _last_reload_time < _DEBOUNCE_SECONDS:
        return
    _last_reload_time = now

    try:
        config.reload()
    except Exception as e:
        logger.error(f"[ConfigWatcher] Failed to reload config: {e}")
        return

    # Propagate AI settings
    ai_cfg = config.ai
    ai_client.api_key = ai_cfg.get("api_key", ai_client.api_key)
    ai_client.api_base = ai_cfg.get("api_base", ai_client.api_base)
    ai_client.model = ai_cfg.get("model", ai_client.model)
    ai_client.max_tokens = ai_cfg.get("max_tokens", ai_client.max_tokens)
    ai_client.temperature = ai_cfg.get("temperature", ai_client.temperature)
    ai_client.reply_budget.daily_limit = ai_cfg.get("reply_budget", ai_client.reply_budget.daily_limit)
    ai_client.learning_budget.daily_limit = ai_cfg.get("learning_budget", ai_client.learning_budget.daily_limit)

    # 思考模式（DeepSeek thinking mode）热重载
    thinking_cfg = ai_cfg.get("thinking", {}) if isinstance(ai_cfg, dict) else {}
    if "enabled" in thinking_cfg:
        ai_client.thinking_enabled = bool(thinking_cfg["enabled"])
    if "reasoning_effort" in thinking_cfg:
        ai_client.reasoning_effort = thinking_cfg["reasoning_effort"]
    if "max_tokens" in thinking_cfg:
        ai_client.thinking_max_tokens = int(thinking_cfg["max_tokens"])
    if "strip_reasoning_from_log" in thinking_cfg:
        ai_client.strip_reasoning_from_log = bool(thinking_cfg["strip_reasoning_from_log"])

    # Propagate reply-decider settings
    group_behavior = config.character.get("group_behavior", {})
    reply_decider.base_probability = group_behavior.get("reply_probability", reply_decider.base_probability)
    reply_decider.cooldown = group_behavior.get("reply_cooldown", reply_decider.cooldown)
    reply_decider.at_debounce = group_behavior.get("at_debounce", reply_decider.at_debounce)
    reply_decider.max_daily_replies = group_behavior.get("max_daily_replies", reply_decider.max_daily_replies)
    reply_decider.refresh_times = sorted(group_behavior.get("refresh_times", reply_decider.refresh_times))
    reply_decider.period_quota = reply_decider.max_daily_replies

    # 注入分群配置：每个群单独注册 reply_probability（以及未来可扩展的字段）
    try:
        groups_cfg = config.groups
        if isinstance(groups_cfg, dict):
            # default 群作为兜底
            default_cfg = groups_cfg.get("default", {})
            if isinstance(default_cfg, dict) and "reply_probability" in default_cfg:
                # 不覆盖 default 兜底（reply_decider.base_probability 已经从 group_behavior 拿到）
                # 实际生效逻辑：未注册的群 = base_probability
                pass
            # 注入每个显式配置的非 default 群
            reply_decider.apply_groups_config(groups_cfg)
    except Exception as e:
        logger.error(f"[ConfigWatcher] Failed to apply per-group config: {e}")

    # Update character-card watcher if path changed
    _update_character_watcher(config, ai_client, reply_decider)

    logger.info(
        f"[ConfigWatcher] Config hot reload succeeded. "
        f"model={ai_client.model}, default_reply_probability={reply_decider.base_probability}, "
        f"group_overrides={len(reply_decider._group_probability)}, "
        f"character={config.char_name}"
    )


def _update_character_watcher(config, ai_client, reply_decider):
    """If the character card path changed, update the file watcher."""
    card_path_str = config._data.get("character_card", "")
    if not card_path_str:
        return
    card_path = Path(card_path_str)
    if not card_path.is_absolute():
        card_path = _PROJECT_ROOT / card_path

    # We don't dynamically swap watchdog watches here to keep it simple;
    # instead we watch the whole characters directory in start_watching.
    pass


def start_watching(config, ai_client, reply_decider) -> None:
    """
    Start the file watcher.

    :param config: BotConfig singleton
    :param ai_client: AIClient instance
    :param reply_decider: ReplyDecider instance
    """
    global _observer

    if not _WATCHDOG_AVAILABLE:
        logger.warning("[ConfigWatcher] File watching disabled (watchdog unavailable)")
        return

    handler = _ConfigReloadHandler(config, ai_client, reply_decider)

    _observer = Observer()
    # Watch config directory so bot.yaml changes are caught
    _observer.schedule(handler, str(_CONFIG_PATH.parent), recursive=False)
    # Watch characters directory so any .json card change is caught
    characters_dir = _PROJECT_ROOT / "characters"
    if characters_dir.exists():
        _observer.schedule(handler, str(characters_dir), recursive=False)

    _observer.start()
    logger.info("[ConfigWatcher] File watcher started")


def stop_watching() -> None:
    """Stop the file watcher (useful for clean shutdown)."""
    global _observer
    if _observer is not None:
        _observer.stop()
        _observer.join()
        _observer = None
        logger.info("[ConfigWatcher] File watcher stopped")
