"""
group_admin 配置管理

读取 config/admin.yaml，管理功能开关
"""

from pathlib import Path
from typing import Any

import yaml

_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "admin.yaml"

_DEFAULT_CONFIG = {
    "enabled": True,
    "features": {
        "ban": True,
        "kick": True,
        "whole_ban": True,
        "delete_msg": True,
        "keyword_filter": True,
        "stats": True,
        "bot_control": True,
    },
    "keyword_filter": {
        "action": "ban",
        "ban_duration": 600,
        "keywords": [],
    },
    "default_permissions": {
        "admin_can_ban": True,
        "admin_can_kick": True,
        "admin_can_whole_ban": True,
    },
}


class AdminConfig:
    """群管理配置管理器"""

    def __init__(self):
        self._config = self._load_config()

    def _load_config(self) -> dict:
        """加载配置文件，不存在则使用默认值"""
        if _CONFIG_PATH.exists():
            try:
                with open(_CONFIG_PATH, encoding="utf-8") as f:
                    loaded = yaml.safe_load(f) or {}
                    return self._merge_defaults(loaded)
            except Exception:
                return _DEFAULT_CONFIG.copy()
        return _DEFAULT_CONFIG.copy()

    def _merge_defaults(self, loaded: dict) -> dict:
        """合并默认值，确保所有必要字段存在"""
        merged = _DEFAULT_CONFIG.copy()
        for key, value in loaded.items():
            if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
                merged[key].update(value)
            else:
                merged[key] = value
        return merged

    def is_feature_enabled(self, feature_name: str) -> bool:
        """检查某个功能是否开启"""
        if not self._config.get("enabled", True):
            return False
        features = self._config.get("features", {})
        return features.get(feature_name, True)

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置项"""
        return self._config.get(key, default)
