# QQ Roleplay Bot

> 基于 NoneBot2 + NapCat + DeepSeek 的 AI 角色扮演 QQ 群聊机器人框架

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](#)
[![NoneBot2](https://img.shields.io/badge/nonebot2-2.4%2B-green.svg)](#)

一个**可定制的 AI 群聊机器人框架**，主打"持久化人格 + 群友关系 + 主动意识"三件套，
用 SillyTavern 角色卡作为人格源，通过四层 Prompt 缓存降低推理成本，
通过自我意识系统让角色在群聊中"活"起来。

> **重要声明**：本项目仅供学习与个人实验 QQ Bot 开发，**不提供任何形式的合规承诺**。
> 请遵守 QQ 用户协议与本地法律法规，第三方协议登录存在封号风险。

---

## ✨ 核心特性

| 模块 | 说明 |
|---|---|
| 🧠 **四层 Prompt 缓存** | L0 静态角色卡 / L1 Few-shot / L2 per-user 慢变 / L3 快变上下文，按需注入，最大化 KV Cache 命中率 |
| 👥 **群友画像与关系** | 自动从群聊中提取每个人的性格标签、活跃度、亲密度（stranger → close） |
| 🌀 **多角色人格来源** | 直接读 SillyTavern V2 角色卡 JSON，零成本切换不同 AI 人格 |
| 🌱 **自我意识系统** | 角色持续状态、情景记忆、印象笔记、关系推进、每日复盘、主动发起对话 |
| 💬 **私聊 / 群聊完全分离** | 两套独立数据通路，记忆互不串扰，按 `group_id` 严格隔离 |
| 🎯 **触发策略** | `@` 触发、低概率随机插话（按群覆盖）、冷却防抖、每日配额 |
| 🧮 **思考模式** | 支持 DeepSeek `thinking` 参数（reasoning_effort=max），可关闭以降低延迟 |
| 🛠️ **管理员插件** | 群管功能（禁言/踢人/撤回/关键词过滤），与角色扮演解耦 |

---

## 🏗️ 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                        QQ 用户/群聊                          │
└──────────────┬──────────────────────────────┬───────────────┘
               │ WebSocket (OneBot v11)         │ HTTP API
               ▼                                ▲
┌─────────────────────────────────────────────────────────────┐
│                          NapCat                              │
│  （QQ 协议层，安装脚本自动从 GitHub Releases 拉取）              │
└──────────────┬──────────────────────────────┬───────────────┘
               │ 正向 WS                          │ 反向 HTTP
               ▼                                ▲
┌─────────────────────────────────────────────────────────────┐
│                       NoneBot2 本体                          │
│  ┌─────────────┐  ┌─────────────┐  ┌────────────────────┐    │
│  │  echo 插件  │  │ group_admin │  │   roleplay 插件   │    │
│  └─────────────┘  └─────────────┘  │  (本文档的主角)    │    │
│                                    └──────┬─────────────┘    │
└───────────────────────────────────────────┼─────────────────┘
                                            ▼
                    ┌──────────────────────────────────┐
                    │       AI 调用层 (ai_client)        │
                    │  ──────────────────────────────  │
                    │  • DeepSeek Chat Completions API  │
                    │  • Token 预算控制 (reply/learn)   │
                    │  • 思考模式注入 (thinking)         │
                    └──────────────┬───────────────────┘
                                   ▼
                    ┌──────────────────────────────────┐
                    │       持久层 (SQLite)             │
                    │  ──────────────────────────────  │
                    │  member_profiles / member_memories│
                    │  group_profiles / roleplay.db     │
                    │  episodic_memories / relationships│
                    └──────────────────────────────────┘
```

### roleplay 插件内部数据流

```
新消息进 ──► ReplyDecider ──► 触发? ──► MessageProcessor
                              │ no              │
                              ▼                 ▼
                         入库/学习         build_prompt()
                          (L2 慢变)         │
                                            ├── L0 角色卡 (system)
                                            ├── L1 few-shot (system)
                                            ├── L2 群友档案 (user)
                                            └── L3 当前上下文 (user)
                                                    │
                                                    ▼
                                              AIClient.chat()
                                                    │
                                                    ▼
                                                发出回复
```

---

## 🚀 快速开始

### 前置条件

- **操作系统**：Linux（推荐 Ubuntu 22.04+ / Debian 12+）
- **Python**：3.10 或更高
- **一个 QQ 小号**：用于 NapCat 登录，**强烈建议使用不常用的小号**
- **DeepSeek API Key**：在 [DeepSeek 控制台](https://platform.deepseek.com) 创建

### 安装步骤

```bash
# 1. 克隆仓库
git clone https://github.com/cnc876297794-arch/qq-roleplay-bot.git
cd qq-roleplay-bot

# 2. 安装 Python 依赖
python -m venv venv
source venv/bin/activate
pip install -e .

# 3. 下载 NapCat（QQ 客户端）
./install.sh

# 4. 准备配置文件
cp .env.example .env
cp config/bot.yaml.example config/bot.yaml
cp config/admin.yaml.example config/admin.yaml
# 编辑 .env：填入 DEEPSEEK_API_KEY 和你的机器人 QQ 号
# 编辑 config/bot.yaml：按需修改 ai.* / groups.* / consciousness.*

# 5. 放入你的角色卡
mkdir -p characters
# 把 SillyTavern V2 角色卡放到 characters/<name>.json
# 然后在 config/bot.yaml 中设置 character_card 指向它

# 6. 登录 QQ（首次会打印二维码，用手机 QQ 扫码）
cd napcat
./launch.sh
# 看到 "扫码登录成功" 后，另开终端回到项目根目录

# 7. 启动 Bot
cd ..
./start_bot.sh
```

> 💡 想看具体怎么准备角色卡？看 [docs/CARDS.md](docs/CARDS.md)（待补充）。

---

## ⚙️ 配置说明

### 环境变量（推荐，敏感信息走这里）

| 变量 | 必填 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | ✅ | DeepSeek API key |
| `SUPERUSER` | ✅ | 超级用户 QQ 号（JSON 数组字符串） |
| `ONEBOT_API_ROOTS` | ✅ | 形如 `{"<bot_qq>":"http://127.0.0.1:3002/"}` |
| `HOST` / `PORT` | | NoneBot 监听地址，默认 `0.0.0.0:8080` |
| `LOG_LEVEL` | | DEBUG / INFO / WARNING / ERROR |
| `QQBOT_BOT_ACCOUNT_IDS` | | 同群其他机器人 QQ 号（逗号分隔），不处理其消息 |
| `QQBOT_DUMP_LLM_PROMPT` | | 设为 1 调试时把每次 prompt 写入 `data/last_llm_prompt.json` |

### `config/bot.yaml` 主要段

- `character_card`：SillyTavern 角色卡路径
- `character.group_behavior`：回复概率、冷却、每日配额
- `ai.*`：API 配置、token 预算、思考模式开关
- `groups.<group_id>`：按群覆盖回复概率
- `learning.*`：后台学习任务的刷新间隔
- `consciousness.*`：意识层（事件提取/记忆衰减/复盘/主动发起）

完整字段说明见 [config/bot.yaml.example](config/bot.yaml.example)。

---

## 📚 文档

- [架构详解](docs/ARCHITECTURE.md)（待补充）
- [角色卡制作](docs/CARDS.md)（待补充）
- [多群部署](docs/MULTI-GROUP.md)（待补充）
- [常见问题](docs/FAQ.md)（待补充）

---

## 🤝 贡献

欢迎 PR！请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 了解开发流程。

## 📜 许可证

[MIT](LICENSE) © 2026 qq-roleplay-bot contributors

## ⚠️ 风险提示

- 本项目通过第三方协议接入 QQ 服务（NapCat），**存在账号被封禁的风险**。
- 默认运行模式会向 DeepSeek API 发送群聊内容，**请勿在生产环境使用含敏感信息的群**。
- 思考模式（`thinking.enabled: true`）会显著增加 token 消耗与响应延迟。
- 部署者需自行承担合规与运营风险。
