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

标准配置只包含工厂调用和组并发：

```yaml
clients:
  - factory: codex
    name: Codex CLI Stateless
    group: codex_cli
    enable: true
    mode: stateless
    sandbox: read-only
    usage_guard:
      primary_max_percent: 65
      secondary_max_percent: 90
      namespace: default
      refresh_seconds: 30
      reset_grace_seconds: 30

group_limits:
  codex_cli: 1
```

`usage_guard` 通过 Codex app-server 的 `account/rateLimits/read` 读取账号窗口快照，将
`primary`（当前通常是 5 小时）和 `secondary`（当前通常是每周）百分比保存到
`_data/codex_usage.sqlite3`。`primary_max_percent` 控制 5 小时窗口，
`secondary_max_percent` 控制周窗口；上例分别在用到 65% 和 90% 时暂停新任务，即保留
35% 的短窗口额度和 10% 的周额度。也可只设置 `reserve_percent`，为两个窗口指定相同的
保留比例（默认 20%）。任一有效窗口达到各自上限都会停止分配。管理器直接采用服务端
`resetsAt`，并在重置点后保留
`reset_grace_seconds` 缓冲，随后自动恢复。官方接口不可用时才只读同步
`~/.codex/sessions`；两种同步都不会发起模型请求。多个 Client 共用同一登录账号时应使用
相同的 `namespace`。如确需关闭，可设置 `usage_guard.enabled: false`。

配置工厂引擎只消费 `factory`、`name`、`group`、`enable`，其余字段直接作为关键字参数传给
`providers/` 中对应的预置构造函数。这里的 `providers/` 同时包含两部分：底层协议/
厂商适配器，以及供配置引擎调用、名称以 `_preset` 结尾的组装函数。用户实现自定义
工厂时，可直接参考这些 `create_*_preset()` 的参数和返回方式。内置短名称包括 `codex`、`kimi`、
`gemini_free`、`groq_free`、`nvidia_free`、`openai_compatible`、`openclaw`、
`zhipu` 和 `self_rotating_openai`。

自定义 Client 不需要修改工厂引擎。令自己的模块提供同一签名的工厂：

```python
def create_client(*, name, group, endpoint, **kwargs):
    return MyClient(name=name, group_id=group, endpoint=endpoint, **kwargs)
```

然后直接在 YAML 引用可导入路径：

```yaml
- factory: my_package.clients:create_client
  name: My Client
  group: custom
  endpoint: https://example.test
```

`my_package.clients:create_client` 按 Python 的正常模块规则导入，适合已经安装或位于
应用导入路径中的包。部署目录中的单文件工厂可写成
`./custom_clients.py:create_client`；该路径固定相对于 YAML 所在目录解析，与从哪个
工作目录启动 IIS 无关。工厂需要引用同目录辅助模块时可使用
`from .local_settings import ...`。不要使用含义不明确的“相对于当前工作目录”路径。

宿主程序也可以通过 `ClientFactoryEngine({"custom": create_client})` 注册短名称；
两种扩展最终都走相同的配置读取、实例校验和 Client 列表拼装流程。

一个 Client 同时轮转多个模型和多个 Token 的内置预置示例：

```yaml
clients:
  - factory: self_rotating_openai
    name: ModelScope Rotating Client
    group: modelscope
    api_base_url: https://api-inference.modelscope.cn/v1
    models:
      - deepseek-ai/DeepSeek-R1-0528
      - deepseek-ai/DeepSeek-V3.2-Exp
      - Qwen/Qwen3-Coder-480B-A35B-Instruct
    token_envs:
      - MODELSCOPE_API_KEY_1
      - MODELSCOPE_API_KEY_2
      - MODELSCOPE_API_KEY_3
    model_rotate_per_times: 5
    token_rotate_per_times: 15

group_limits:
  modelscope: 1
```

这里保留了旧示例的行为：每个模型连续使用 5 次，三个模型合计 15 次后切换
Token。密钥通过环境变量加载，不需要写入 YAML。

安装 [requirements.txt](requirements.txt) 后，推荐先运行独立 Launcher 校验配置，
再访问本机管理页面。`recycled/` 中是已废弃文件，不是运行入口。

### 独立配置校验与手动对话页

推荐使用 [cli/launcher.py](cli/launcher.py) 代替负载演示脚本。
它默认加载本模块的 `config/config.yaml`，启动时只验证配置、预算策略和本机
Harness CLI 路径，不会自动发送模型请求。首次找不到该文件时，会使用
`config/example.yaml` 并显示复制命令：

    Copy-Item AIClientCenter/config/example.yaml AIClientCenter/config/config.yaml

    # 仅校验配置（不启动服务、不消耗模型额度）
    python -m AIClientCenter --validate-only

    # 启动仅本机可访问的管理与手动对话页
    python -m AIClientCenter --port 8000

启动后访问 http://127.0.0.1:8000/observations 查看调用次数、成功率、Token、逻辑
流量、耗时、质量反馈覆盖率，以及按服务商、模型、账号或 Client 的切片排行。页面只查
本地 SQLite，不会触发模型请求，也不会自动高频刷新。独立入口默认把记录写入
`AIClientCenter/_data/ai_client_observations.db`；可用 `--observation-db` 修改路径，或用
`--disable-observation` 关闭。

访问 http://127.0.0.1:8000/playground，选择已注册的 client 后可手动
发送 system/user prompt。调用仍会经过 AIClientManager 的并发和预算策略，因而
可能消耗 API 或 Harness 套餐额度。非本机监听必须显式加 --allow-remote，并自行
提供访问控制；也可传入 --disable-manual-calls 禁用真实调用。

IIS 内嵌的登录保护面板 `/monitor/ai-client-dashboard/` 还提供 `Reload Config`：
它会重新读取 `_config/ai_clients.yaml` 并重建全部 Client。配置读取、工厂构造或校验
失败时继续使用旧池；成功时在同一个 `AIClientManager` 内原子切换，新任务使用新池，
已经开始的调用继续使用旧 Client，完成后才释放其连接。空 Client 配置会被拒绝，连续
点击产生的并发重载返回 409，不会清空或阻塞当前池。对应受保护接口为
`POST /monitor/ai-client-dashboard/api/reload-config`。

## 目录导航

| 路径 | 用途 |
| --- | --- |
| `core/` | 调度器、Client 基类、预算策略、用量统计、SQLite 状态日志和通用轮换器。 |
| `providers/` | 底层服务商适配，以及配置引擎可直接调用的 `create_*_preset()` 组装函数。 |
| `harness/` | 已登录的本机 Agent CLI 适配器（Codex、Kimi）。 |
| `web/` | Flask dashboard 与受控手动调用页面。 |
| `cli/` | 独立启动和配置校验入口。 |
| `services/` | 余额查询与可轮换 Token 服务。 |
| `config/` | 独立启动入口使用的 YAML 配置：本机 `config.yaml`（已忽略）和 `example.yaml`。 |
| `tools/` | 最小调用示例和可选 PyQt5 余额查询 UI。 |
| `tests/` | 离线 pytest；`manual_agent_harness.py` 是会消耗套餐的人工冒烟脚本。 |
| `recycled/` | 已废弃历史文件，不维护、不参与当前导入图。 |

## 关键模块

[core/manager.py](core/manager.py)

> 核心代码：BaseAIClient 接口、Client 的运行时状态、预算准入和调度管理。

Client 健康恢复采用按需熔断策略：启动和空闲期间不发送测试 Prompt；首次真实请求
自然完成有效性验证。连续瞬时失败达到阈值后进入带抖动的指数退避，只有存在真实任务
且退避到期时才放行一个请求试运行。401/403 等永久错误保持隔离，直到配置更新或人工
执行健康检查。`HealthPolicy` 可配置失败阈值、初始/最大退避及抖动比例。

[web/dashboard.py](web/dashboard.py)

> 网页管理工具后端，内联前端网页。该功能可选，完全可以不使用该模块，但建议使用。

[providers/openai_clients.py](providers/openai_clients.py)

> AI Client的实现，依赖于 OpenAICompatibleAPI ，并默认混入了 ClientMetricsMixin 。

[core/metrics.py](core/metrics.py)

> 余额及用量统计的兼容 mixin。额度信息通过 BudgetPolicy 单独参与调度，
> 不再等同于 Client 的运行时健康度。

[core/observation.py](core/observation.py)

> 通用调用观测器。每次真实调用只记录服务商、模型、账号别名、结果、耗时、逻辑字节数、
> 估算/实报 Token 等元数据，不保存 Prompt 和回复正文。数据追加到 SQLite，可按时间、
> 服务商、模型、账号或 Client 切片汇总。上层可选用 `feedback(request_id, valid=...,
> score=..., labels=...)` 回填通用质量信息；AIClientCenter 不依赖任何业务 Schema。

[core/budget.py](core/budget.py)

> 本地预算策略：unknown、observed、soft_limit、hard_limit。适用于 API、
> Agent CLI Harness 以及无法查询余额的服务。

[core/credentials.py](core/credentials.py)

> 多 API Key 配置归一化。工厂参数或自定义 Python 配置可用 `{"account_a": "key..."}` 的形式
> 声明多个独立额度；客户端名只显示别名，SQLite 限额账本使用不可逆指纹隔离额度，
> 不记录明文 Key。空映射会兼容回退到服务商原有的单 Key 环境变量。

[factory_engine.py](factory_engine.py)

> 唯一配置装配入口：读取 YAML、解析内置短名称或 `module:callable` 外部工厂、调用
> `providers/` 下的预置并拼装 Client 列表。扩展接口和工厂返回契约在文件顶部明确
> 说明；组并发始终由 YAML 的 `group_limits` 提供。

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

[providers/groq.py](providers/groq.py)

> Groq 免费层大模型配置。默认只启用 `openai/gpt-oss-120b`，排除账号中可见的
> 20B/27B/7B 模型；`build_groq_clients(api_keys={...})` 可直接接收多 Key 配置，
> 未传入时回退读取 `GROQ_API_KEY`。每个 Key 独立按官方免费层 RPM、RPD、TPM、TPD
> 的 80% 主动控流并单并发。Groq 免费层 TPM 较低，长 Prompt 会在本地拒绝而不
> 消耗远端请求；可用 `GROQ_QUOTA_SAFETY_RATIO` 和
> `GROQ_COMPLETION_RESERVE_TOKENS` 调整保守程度。限额以
> [Groq Rate Limits](https://console.groq.com/docs/rate-limits) 和账号 Limits 页面为准。

[providers/nvidia.py](providers/nvidia.py)

> NVIDIA Build 免费原型端点配置。每个账号可直接配置一个 Key；账号内多个候选模型
> 共享同一份保守 RPM 账本和单并发组，不同账号相互独立。默认启用 Nemotron 3 Ultra、
> GLM-5.3 和 Kimi K3；由于 NVIDIA 免费限流会随模型和平台负载动态变化，默认只按
> 账号页面常见的 40 RPM 上限使用 60%，远端 429 仍交给按需熔断处理。

[harness/cli.py](harness/cli.py)

> 基于本机命令行 AI Agent（Codex CLI / Kimi CLI）的客户端实现。
> 当 API 服务不可用或余额查询失效时，可走已登录的 Agent CLI 进行分析：
> `stateless` 模式每次全新调用（模拟 AI Client），`session` 模式沿会话续接以利用缓存省 Token。
> 各 Agent CLI 的非交互调用方式调研见 [doc/AgentCLIResearch.md](doc/AgentCLIResearch.md)，
> 配置示例见 [config/example.yaml](config/example.yaml) 末尾，冒烟测试见
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
