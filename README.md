# AIClientCenter

An AI client center that supports token and model rotation

## 前言

如果你需要一个专业的AI客户端聚合与负载均衡工具，可以参考以下项目：

> https://github.com/songquanpeng/one-api

> https://github.com/enricoros/big-AGI

如果你想降低成本，诸如使用硅基流动14元token号以及每天白嫖2000次魔搭API，请往下看。

## 起因

由于我的 [IntelligenceIntegrationSystem (IIS)](https://github.com/SleepySoft/IntelligenceIntegrationSystem) 
项目需要消耗大量的Token，为了降低成本，我尝试了各种办法。比如咸鱼的14元token，比如魔搭每日2000额度。 
但事实证明，白嫖这碗饭并不是这么容易吃的：

> + 硅基流动的服务在更换Token后可能会有一段时间一直返回503错误，几个小时后会恢复。
> 
> + 魔搭明面上的限制是每日2000条，单个模型500次。实际上400B以上可用的模型只有3个，而且服务不稳定，且容易触发敏感词。

于是就诞生了这个项目，本项目主要实现我的以下需求：

1. 多Client管理，根据可用性及价格动态获取可用的使用价格最低Client，可以多线程多Client并行处理。
2. 支持Token及模型轮换，同时支持查询特定服务提供商的Token余额。
3. 自动用量统计，支持限额及余额两种模式，并判断客户端的健康度。
4. 自动管理Client的错误状态，尽可能及时发现出错的客户端，仅返回可用的客户端
5. 自动测试Client的可用性，及时发现已恢复的服务。

## 快速运行及预览

安装 [requirements.txt](requirements.txt) 后，推荐先运行独立 Launcher 校验配置，
再访问本机管理页面。`recycled/` 中是已废弃文件，不是运行入口。

### 独立配置校验与手动对话页

推荐使用 [cli/launcher.py](cli/launcher.py) 代替负载演示脚本。
它默认加载本模块的 `config/config.py`，启动时只验证配置、预算策略和本机
Harness CLI 路径，不会自动发送模型请求。首次找不到该文件时，会使用
`config/example.py` 并显示复制命令：

    Copy-Item AIClientCenter/config/example.py AIClientCenter/config/config.py

    # 仅校验配置（不启动服务、不消耗模型额度）
    python -m AIClientCenter --validate-only

    # 启动仅本机可访问的管理与手动对话页
    python -m AIClientCenter --port 8000

启动后访问 http://127.0.0.1:8000/playground，选择已注册的 client 后可手动
发送 system/user prompt。调用仍会经过 AIClientManager 的并发和预算策略，因而
可能消耗 API 或 Harness 套餐额度。非本机监听必须显式加 --allow-remote，并自行
提供访问控制；也可传入 --disable-manual-calls 禁用真实调用。

## 目录导航

| 路径 | 用途 |
| --- | --- |
| `core/` | 调度器、Client 基类、预算策略、用量统计、SQLite 状态日志和通用轮换器。 |
| `providers/` | OpenAI 兼容协议、OpenAI Client、智谱、Gemini、OpenClaw 等服务商适配。 |
| `harness/` | 已登录的本机 Agent CLI 适配器（Codex、Kimi）。 |
| `web/` | Flask dashboard 与受控手动调用页面。 |
| `cli/` | 独立启动和配置校验入口。 |
| `services/` | 余额查询与可轮换 Token 服务。 |
| `config/` | 独立启动入口使用的配置：本机 `config.py`（已忽略）和可提交的 `example.py`。 |
| `tools/` | 最小调用示例和可选 PyQt5 余额查询 UI。 |
| `tests/` | 离线 pytest；`manual_agent_harness.py` 是会消耗套餐的人工冒烟脚本。 |
| `recycled/` | 已废弃历史文件，不维护、不参与当前导入图。 |

## 关键模块

[core/manager.py](core/manager.py)

> 核心代码：BaseAIClient 接口、Client 的运行时状态、预算准入和调度管理。

[web/dashboard.py](web/dashboard.py)

> 网页管理工具后端，内联前端网页。该功能可选，完全可以不使用该模块，但建议使用。

[providers/openai_clients.py](providers/openai_clients.py)

> AI Client的实现，依赖于 OpenAICompatibleAPI ，并默认混入了 ClientMetricsMixin 。

[core/metrics.py](core/metrics.py)

> 余额及用量统计的兼容 mixin。额度信息通过 BudgetPolicy 单独参与调度，
> 不再等同于 Client 的运行时健康度。

[core/budget.py](core/budget.py)

> 本地预算策略：unknown、observed、soft_limit、hard_limit。适用于 API、
> Agent CLI Harness 以及无法查询余额的服务。

[services/token_rotator.py](services/token_rotator.py)

> 批量Token管理及自动轮转功能。

[core/rotator.py](core/rotator.py)

> 机械的轮换功能，仅通过计数进行轮换。用于像魔搭这种限制单个模型使用量的平台。

[services/balance_query.py](services/balance_query.py)

> 特定服务提供商的余额查询。

[providers/openai_compatible.py](providers/openai_compatible.py)

> OpenAI风格的API接口，通常并不会直接使用，而是将其传入 BaseAIClient 并加入 AIClientManager 统一管理。

[providers/zhipu.py](providers/zhipu.py)

> 智谱的客户端，使用官方API。智谱注册和实名认证后有一定免费额度。

[providers/gemini.py](providers/gemini.py)

> Gemini客户端。由于本人账号限制，无法白嫖。

[harness/cli.py](harness/cli.py)

> 基于本机命令行 AI Agent（Codex CLI / Kimi CLI）的客户端实现。
> 当 API 服务不可用或余额查询失效时，可走已登录的 Agent CLI 进行分析：
> `stateless` 模式每次全新调用（模拟 AI Client），`session` 模式沿会话续接以利用缓存省 Token。
> 各 Agent CLI 的非交互调用方式调研见 [doc/AgentCLIResearch.md](doc/AgentCLIResearch.md)，
> 配置示例见 [config/example.py](config/example.py) 末尾，冒烟测试见
> [tests/manual_agent_harness.py](tests/manual_agent_harness.py)。

## 文件状态与历史代码

完整文件索引、生产入口、手动/付费工具与历史原型的保留理由见
[doc/FileGuide.md](doc/FileGuide.md)。特别注意：

- `python -m AIClientCenter` 是当前独立入口；
- `tests/manual_agent_harness.py` 会真实消耗 Agent CLI 套餐；
- `tools/balance_query_ui.py` 是可选 PyQt5 工具；
- `recycled/` 中是已废弃历史文件，不进入当前运行时导入图。


---------

## 其它

各个平台的免费额度政策经常变，因此很可能出现不稳定或一段时间拒绝服务的情况。并且在使用时注意限制同一个平台的并发访问数量（AIClientManager.set_group_limit()）。
