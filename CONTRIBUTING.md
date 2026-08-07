# 贡献指南

感谢你愿意为 `qq-roleplay-bot` 添砖加瓦！本文档说明开发流程与规范。

## 开发流程

1. Fork 本仓库
2. 创建特性分支：`git checkout -b feat/your-feature`
3. 提交前确保：
   - 代码风格与现有保持一致（参考既有模块命名）
   - 不在代码中提交任何 **API key、QQ 号、个人配置**
   - `.env`、`config/bot.yaml`、`config/admin.yaml`、`characters/`、`data/` 均已在 `.gitignore` 中
4. 推送到你的 fork 并发起 Pull Request
5. 在 PR 描述中说明动机、变更点、测试方式

## 提交信息约定

推荐使用 [Conventional Commits](https://www.conventionalcommits.org/)：

```
feat(roleplay): 新增群级冷却配置项
fix(ai_client): 修复思考模式下 max_tokens 优先级问题
docs(readme): 补充快速开始截图
refactor(consciousness): 拆分事件提取 pipeline
```

## 代码风格

- Python 3.10+ 语法（可使用 `match`、`X | Y`）
- 缩进 4 空格
- 类型注解：函数签名必须，函数体内可省略
- 公开函数/类必须有 docstring（中文或英文均可）
- 日志统一用 `nonebot.log.logger`，避免自定义 logger 被 NoneBot 框架漏掉

## 目录约定

```
plugins/roleplay/    # 核心 roleplay 插件
  ai_client.py       # DeepSeek 客户端
  config.py          # BotConfig 单例（YAML + 角色卡）
  database.py        # SQLite 持久层
  memory_engine.py   # 记忆系统
  personality_engine.py  # 性格分析
  topic_tracker.py   # 话题追踪
  reply_decider.py   # 回复决策
  message_processor.py   # 消息处理
  character_runtime.py   # 角色运行时
  consciousness.py   # 自我意识系统
  config_watcher.py  # 配置热重载
plugins/group_admin/  # 群管插件（与 roleplay 解耦）
qqbot/bot.py         # NoneBot 入口
config/              # *.yaml（不入仓，模板见 *.example）
.env                 # 不入仓，模板见 .env.example
```

## 隐私红线

- **绝不提交任何人的真实 QQ 号、群号、聊天记录**到公共 PR
- 调试时若需要贴日志，请用 `<QQ:123456>`、`<群:123456>` 脱敏
- 角色卡涉及版权/IP 时，请确认你有权开源

## 联系方式

- Issues：提 bug / 建议
- Discussions：通用问题与想法

期待你的 PR！
