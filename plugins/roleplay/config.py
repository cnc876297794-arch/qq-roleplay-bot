"""
config.py - Bot 配置管理模块

支持：
1. 从 config/bot.yaml 读取基础配置（API、群设置等）
2. 从 characters/*.json 加载 SillyTavern 角色卡
3. 组合高质量 system prompt 和示例对话

角色卡格式（SillyTavern 标准）::
    {
        "name": "角色名",
        "description": "角色描述",
        "personality": "性格特征",
        "scenario": "场景设定",
        "first_mes": "开场白",
        "mes_example": "示例对话",
        "post_history_instructions": "后置指令/Jailbreak",
        "creatorcomment": "创作者注释",
        "tags": ["标签"]
    }
"""

import json
import re
from pathlib import Path
from typing import Any

import yaml

# 配置文件路径
_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "bot.yaml"
_CHARACTERS_DIR = Path(__file__).resolve().parent.parent.parent / "characters"


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并字典，override 中非 None 的值优先"""
    merged = base.copy()
    for k, v in override.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = _deep_merge(merged[k], v)
        elif v is not None:
            merged[k] = v
    return merged


class BotConfig:
    """
    单例式配置管理器。
    支持 YAML 基础配置 + SillyTavern 角色卡。

    用法::

        cfg = BotConfig()
        print(cfg.character_prompt)   # 完整的 system prompt
        print(cfg.mes_example)         # 解析后的示例对话列表
    """

    _instance: "BotConfig | None" = None

    def __new__(cls) -> "BotConfig":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if not hasattr(self, "_data"):
            self._data: dict[str, Any] = {}
            self._character_card: dict[str, Any] = {}
            self.load()

    # ────────────────── 加载 ──────────────────

    def load(self) -> None:
        """从 YAML 重新加载配置，并加载角色卡"""
        # 加载 YAML
        try:
            with open(_CONFIG_PATH, encoding="utf-8") as f:
                self._data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            self._data = {}

        # 加载角色卡
        self._load_character_card()

    def reload(self) -> None:
        """别名，与 load 一致"""
        self.load()

    def _load_character_card(self) -> None:
        """加载 SillyTavern 格式的角色卡"""
        card_path = self._data.get("character_card", "")
        if not card_path:
            return

        # 支持相对路径（相对于项目根目录）
        path = Path(card_path)
        if not path.is_absolute():
            path = _CONFIG_PATH.parent.parent / path

        try:
            with open(path, encoding="utf-8") as f:
                self._character_card = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._character_card = {}

    # ────────────────── 属性访问 ──────────────────

    @property
    def character(self) -> dict[str, Any]:
        """角色设定字典（兼容旧版 YAML character 字段）"""
        return self._data.get("character", {})

    @property
    def ai(self) -> dict[str, Any]:
        """AI 接口配置字典"""
        return self._data.get("ai", {})

    @property
    def groups(self) -> dict[str, Any]:
        """全部群配置（含 default）"""
        return self._data.get("groups", {})

    @property
    def learning(self) -> dict[str, Any]:
        """学习模块配置"""
        return self._data.get("learning", {})

    @property
    def consciousness(self) -> dict[str, Any]:
        """意识层配置（含 event_extraction / memory_decay / reflection / proactive）"""
        return self._data.get("consciousness", {})

    def get_consciousness(self, key: str, default=None):
        """读取意识层子配置，缺省时返回 default"""
        return self.consciousness.get(key, default)

    # ────────────────── 角色卡字段 ──────────────────

    @property
    def character_card(self) -> dict[str, Any]:
        """原始角色卡字典"""
        return self._character_card

    @property
    def char_name(self) -> str:
        """角色名"""
        return self._character_card.get("name", self.character.get("name", "AI助手"))

    @property
    def char_description(self) -> str:
        """角色描述"""
        return self._character_card.get("description", self.character.get("description", ""))

    @property
    def char_personality(self) -> str:
        """性格特征"""
        return self._character_card.get("personality", self.character.get("personality", ""))

    @property
    def char_scenario(self) -> str:
        """场景设定"""
        return self._character_card.get("scenario", "")

    @property
    def char_first_message(self) -> str:
        """开场白"""
        return self._character_card.get("first_mes", "")

    @property
    def char_post_history(self) -> str:
        """后置指令 / Jailbreak"""
        return self._character_card.get("post_history_instructions", "")

    @property
    def char_creator_notes(self) -> str:
        """创作者注释"""
        return self._character_card.get("creatorcomment", "")

    @property
    def char_tags(self) -> list[str]:
        """标签列表"""
        return self._character_card.get("tags", [])

    @property
    def char_physical_appearance(self) -> str:
        """物理人设（外形/身体数据），可选字段"""
        return self._character_card.get("physical_appearance", "")

    # ────────────────── 组合 prompt ──────────────────

    @property
    def character_prompt(self) -> str:
        """
        根据角色卡组合出完整的 system prompt。
        优先使用 SillyTavern 角色卡，回退到旧版 YAML character 字段。
        """
        # 如果有角色卡，使用角色卡格式
        if self._character_card:
            parts = []

            # 后置指令（Jailbreak）- 放最前面，AI对前面内容注意力更强
            post = self.char_post_history
            if post:
                parts.append(f"{post}")

            # 核心身份
            name = self.char_name
            desc = self.char_description
            if name and desc:
                parts.append(f"\n【身份】\n你是{name}。{desc}")
            elif name:
                parts.append(f"\n【身份】\n你是{name}。")
            elif desc:
                parts.append(f"\n【身份】\n{desc}")

            # 物理人设（外形/身体数据）— 可选
            appearance = self.char_physical_appearance
            if appearance:
                parts.append(f"\n{appearance}")

            # 性格
            personality = self.char_personality
            if personality:
                parts.append(f"\n【性格】\n{personality}")

            # 场景
            scenario = self.char_scenario
            if scenario:
                parts.append(f"\n【场景】\n{scenario}")

            # 创作者注释
            notes = self.char_creator_notes
            if notes:
                parts.append(f"\n【备注】\n{notes}")

            return "\n".join(parts)

        # 回退到旧版 YAML 格式
        ch = self.character
        if not ch:
            return "你是一个友好的AI助手，请用中文回复。"

        # 若有自定义全量 prompt，直接返回
        if "system_prompt" in ch and ch["system_prompt"]:
            return ch["system_prompt"]

        # 从旧字段拼接
        parts: list[str] = []
        name = ch.get("name", "")
        personality = ch.get("personality", "")
        style = ch.get("speaking_style", "")
        likes = ch.get("likes", [])
        greeting = ch.get("greeting", "")

        if name:
            parts.append(f"你的名字是「{name}」。")
        if personality:
            parts.append(f"你的性格特点：{personality}。")
        if style:
            parts.append(f"你的说话风格：{style}。")
        if likes:
            likes_str = "、".join(likes) if isinstance(likes, list) else str(likes)
            parts.append(f"你喜欢的事物：{likes_str}。")
        if greeting:
            parts.append(f"你的打招呼方式：「{greeting}」。")

        parts.append("请始终保持在角色中，用中文回复。")
        return "\n".join(parts)

    @property
    def mes_example(self) -> list[dict]:
        """
        解析 mes_example 为 chat API 可用的消息列表。

        格式转换：
        {{user}} -> user role
        {{char}} -> assistant role

        返回: [{"role": "user"/"assistant", "content": "..."}, ...]
        """
        raw = self._character_card.get("mes_example", "")
        if not raw:
            return []

        messages = []
        lines = raw.strip().split("\n")
        char_name = self.char_name

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # 检测 <START> 标记，跳过
            if "<START>" in line.upper() or "<<START>" in line.upper():
                continue

            # 解析 {{user}}: ...
            if line.startswith("{{user}}"):
                content = line[len("{{user}}"):].lstrip(": ").strip()
                if content:
                    messages.append({"role": "user", "content": content})
            # 解析 {{char}}: ...
            elif line.startswith("{{char}}"):
                content = line[len("{{char}}"):].lstrip(": ").strip()
                # 替换 {{char}} 为实际角色名（部分模型会保留{{char}}）
                content = content.replace("{{char}}", char_name)
                if content:
                    messages.append({"role": "assistant", "content": content})

        return messages

    @property
    def first_message(self) -> str:
        """角色开场白（用于私聊初始化或群欢迎）"""
        return self.char_first_message

    # ────────────────── 群级配置 ──────────────────

    def get_group_config(self, group_id: int) -> dict[str, Any]:
        """
        获取群级配置。
        优先使用 group_id 对应的专属配置，然后与 default 合并。
        """
        default_cfg = self.groups.get("default", {})
        group_cfg = self.groups.get(str(group_id), self.groups.get(group_id, {}))
        return _deep_merge(default_cfg, group_cfg)

    # ────────────────── 通用取值 ──────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """通用取值，支持点号分隔路径，如 'ai.model'"""
        parts = key.split(".")
        node: Any = self._data
        for p in parts:
            if isinstance(node, dict) and p in node:
                node = node[p]
            else:
                return default
        return node

    def __repr__(self) -> str:
        return f"<BotConfig path={_CONFIG_PATH} card={self.char_name}>"
