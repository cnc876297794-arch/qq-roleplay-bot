# 安全策略

## 报告漏洞

如果你发现本项目存在安全漏洞，**请不要在公开 Issue 中披露**。

请通过 GitHub Security Advisories 私下联系维护者：
👉 https://github.com/cnc876297794-arch/qq-roleplay-bot/security/advisories/new

请在报告中包含：
- 漏洞类型与影响范围
- 复现步骤
- 可能的修复方向

我们会在 7 个工作日内响应。

## 已知安全注意事项

### 1. API Key 管理

- **请勿将 DeepSeek API key 直接写入 `config/bot.yaml`**
- 推荐做法：在 `.env` 中设置 `DEEPSEEK_API_KEY=sk-...`
- 本项目代码会在启动时优先读取环境变量，yaml 中的值仅作回退
- 定期在 DeepSeek 控制台轮换你的 key

### 2. 配置文件隔离

`.gitignore` 已默认排除以下敏感路径，请勿手动加入版本控制：

- `.env` / `.env.local`
- `config/bot.yaml` / `config/admin.yaml`
- `characters/`（可能含 IP 衍生内容）
- `data/`（含用户聊天记录、印象笔记、关系数据）
- `napcat_data/`（含登录态、token）

### 3. 第三方协议风险

本项目通过 NapCat 接入 QQ 协议，**属于非官方接入方式**。
- 使用小号部署，**不要部署在你的主账号**
- QQ 官方有权对使用非官方协议的主号/小号做封禁
- 部署者需自行承担合规风险

### 4. 群聊内容外发

默认情况下，机器人会把群聊消息发送到 DeepSeek API：
- 请勿在生产环境使用含敏感信息（身份证号、密码、个人隐私）的群
- 思考模式（`thinking.enabled: true`）下，消息内容会进入 reasoning_content 并停留更久
- 如需关闭外发，请在 `config/bot.yaml` 设置 `ai.enabled: false`（如果你实现了这个开关）

### 5. 数据库安全

SQLite 数据库文件 `data/roleplay.db` 含历史消息、用户画像、关系数据：
- 备份时加密或妥善保管
- 分享截图/调试日志前先脱敏
- 建议每周备份一次

## 版本支持

| 版本 | 支持状态 |
|---|---|
| main 分支 | ✅ 活跃开发 |
| 最近 3 个 tag | ✅ 接受安全修复 |
| 更早版本 | ❌ 不再支持，请升级 |

## 致谢

感谢所有负责任地报告漏洞的研究者。
