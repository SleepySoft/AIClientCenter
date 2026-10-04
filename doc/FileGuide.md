# AIClientCenter 文件索引与维护说明

> 审计日期：2026-10-03。本索引的目的不是把历史代码一律删除，而是让维护者
> 能判断每个文件是否仍应被新的部署路径使用。标为“保留”的文件均有明确用途；
> 标为“历史/手动”的文件不应作为生产入口。

## 推荐入口

| 任务 | 文件或命令 | 说明 |
| --- | --- | --- |
| 校验并启动独立后台 | `python -m AIClientCenter`（`cli/launcher.py`） | 当前推荐入口。默认只监听本机，手动调用不会在启动阶段自动执行。 |
| 在 IIS 中使用 | IntelligenceHubStartup.py（父项目） | 由 IIS 读取 _config/ai_client_config.py，并挂载受登录保护的后台。 |
| 配置 client | config/example.py | 可复制到父项目 _config/ai_client_config.py；包含 API、预算、Harness 示例。 |
| 自动化回归 | tests/ | 离线 pytest 测试；不调用真实 API 或 Agent CLI。 |
| 人工真实 Harness 冒烟 | tests/manual_agent_harness.py | 会消耗 Codex/Kimi 套餐，仅在需要验证本机 CLI 时运行。 |

## 运行时核心

| 文件 | 状态 | 人类说明 |
| --- | --- | --- |
| core/manager.py | 保留 | Client 状态、选择、分组并发、健康检查和生命周期的核心。预算准入已与运行健康分离。 |
| providers/openai_clients.py | 保留 | OpenAI 兼容 client，以及本地/外部 Token 轮换变体。 |
| providers/openai_compatible.py | 保留 | 同步/异步 OpenAI 风格 HTTP 调用、超时、重试、错误归类和常用服务工厂。 |
| core/result.py | 保留 | 底层 API 与上层 client 间的统一结果约定。 |
| core/budget.py | 保留 | unknown、observed、soft_limit、hard_limit 本地预算策略。未知额度不会被当成不可用。 |
| core/metrics.py | 保留（兼容层） | 用量、余额、周期计数的持久化 mixin；兼容旧 quota_config/balance_config。新配置优先用 BudgetPolicy。 |
| core/rotator.py | 保留 | 无服务商语义的简单轮换器，供模型/Token 轮换复用。 |
| services/token_rotator.py | 保留 | 面向 SiliconFlow 等可查余额服务的外部 Token 轮换与 RotatableClient 接口。 |
| core/state_logger.py | 保留（可选） | 记录状态、调用区间与 timeline 的 SQLite 观测后端。 |

## Provider 与 Agent 适配器

| 文件 | 状态 | 人类说明 |
| --- | --- | --- |
| harness/cli.py | 保留 | 当前实现 Codex CLI 和 Kimi CLI。通过本机非交互子进程把结果包装成 OpenAI 风格响应。 |
| providers/zhipu.py | 保留（可选） | 智谱官方 SDK 适配器；用于 API 调用，不是 GLM CLI Harness。 |
| providers/gemini.py | 保留（可选） | Gemini HTTP 适配器。 |
| providers/openclaw.py | 保留（可选） | 通过 OpenClaw Gateway WebSocket 连接外部 Agent；不是 AgentCLIClient 子类。启用时需安装 websocket-client。 |
| services/balance_query.py | 保留（可选工具） | 若干服务商余额查询工具，不参与常规调度路径。命令行批量查询必须显式传入 --keys-file。 |
| tools/balance_query_ui.py | 保留（可选工具） | 上述余额查询的 PyQt5 图形界面；未被 Launcher/IIS 自动加载。 |

## 后台、示例与手动工具

| 文件 | 状态 | 使用边界 |
| --- | --- | --- |
| web/dashboard.py | 保留 | Flask dashboard、timeline、手动 client 调用页。独立服务中手动调用默认关闭；IIS 在登录保护下开启。 |
| cli/launcher.py | 保留，首选 | 加载、离线校验并启动独立 dashboard。 |
| config/example.py | 保留 | 人工编辑的 Python 配置样例，不应放入真实 Token。 |
| tools/simplest_demo.py | 保留（最小示例） | 单 client 最小调用参考；不承担配置加载或后台职责。 |
| tests/manual_agent_harness.py | 保留（付费手动测试） | 真实 CLI 冒烟、session/缓存验证；不纳入 pytest。 |

## 文档、测试与历史原型

| 路径 | 状态 | 人类说明 |
| --- | --- | --- |
| README.md | 保留 | 面向使用者的概览和最短启动路径。 |
| doc/AgentCLIResearch.md | 保留 | Codex/Kimi 已实测协议及其他 Agent 的扩展调研。要以当前 adapter 和版本标注为准。 |
| doc/frontend.md | 历史说明 | 原文描述的是早期、非 AIClientCenter 的爬虫 UI；保留原文以免丢失人工记录，但不能作为当前后台说明。 |
| doc/FileGuide.md | 保留 | 本文件：当前文件用途、保留策略与清理边界。 |
| tests/test_*.py | 保留 | 离线回归测试：预算、调度、健康、Harness 解析、dashboard、launcher。 |
| recycled/ | 已废弃 | 其中内容不维护、不进入当前运行时导入图，也不应作为使用依据。 |
| .gitignore | 保留 | Python、测试缓存、环境文件等本地生成物忽略规则。 |
| requirements.txt | 保留 | 核心依赖清单；可选适配器的额外依赖见文件注释。 |

## 清理建议

当前不建议直接删除任何受版本控制文件：

1. recycled/ 已明确隔离且不维护；若未来需要减小仓库，可在发布说明中先声明移除版本，再整体删除该目录。
2. tools/balance_query_ui.py 的 PyQt5 依赖较重，但它是独立人工工具；只有确认不再需要桌面余额查询时才应删除。
3. doc/frontend.md 内容与当前模块不匹配，已标记为历史。新的 dashboard 说明应写入 README 和后续专用文档，而不是改写旧记录。
