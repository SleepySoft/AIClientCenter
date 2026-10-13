# Call Observation

`CallObservationSQLiteLogger` 是与业务无关的调用观测器。它订阅 Client 已有事件，记录
服务商、模型、账号别名、成功状态、耗时、逻辑载荷字节数，以及估算或服务商实报的
Token。它不保存 Prompt、回复正文和 API Key，也不参与调度、预算或熔断。

```python
from AIClientCenter.core.manager import AIClientManager
from AIClientCenter.core.observation import CallObservationSQLiteLogger

observer = CallObservationSQLiteLogger(
    "./ai_client_observations.db",
    retention_days=90,
)
manager = AIClientManager(call_observer=observer)
```

当 Manager 挂载 `AIDashboardService` 后，可通过其 `/observations` 页面查看总览、趋势、
切片排行和最近调用。该页面只读取本地 SQLite；首次打开及手动刷新时查询，不主动探测
Client，也不产生模型请求。独立 Launcher 默认启用并写入
`AIClientCenter/_data/ai_client_observations.db`。

推荐预置会自动填写 `provider` 和 `account_alias`。自行构造的付费 Client 可显式设置：

```python
client.set_observation_context(provider="openai", account_alias="production")
```

成功响应会包含本地元数据，可由任意上层选择性回填反馈：

```python
request_id = response["_ai_client_meta"]["request_id"]
observer.feedback(
    request_id,
    valid=True,
    score=0.9,
    labels={"format": "json", "reply_valid": True},
)
```

`valid`、`score` 和 `labels` 都是通用、可空字段。AIClientCenter 不解释 labels，也不要求
调用方提供反馈。

按时间范围和维度切片：

```python
observer.summarize(
    since_ts=...,
    group_by=("provider", "model", "account_alias"),
    time_bucket="day",
)
```

支持的维度为 `provider`、`model`、`client_name`、`account_alias`、`group_id`；时间桶
支持 `hour` 和 `day`。逻辑字节数是应用层 JSON 的 UTF-8 大小，不包含 TLS、HTTP Header
和传输压缩开销。`request_count` 统计进入 Client 的逻辑调用；底层 HTTP 库在一次调用内
发生的网络重试暂不拆成独立次数。
