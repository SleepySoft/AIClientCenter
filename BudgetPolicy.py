"""可选的本地预算准入策略。

预算信息并不是 Client 可执行性的组成部分：许多 API 无法查询余额，
命令行 Harness 通常也只会提供不完整的套餐用量信息。这个模块只回答
"本地调度器是否愿意继续给该 Client 分配任务"，不修改 ClientStatus。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class BudgetMode(str, Enum):
    """预算信息的可信程度及其对调度的影响。"""

    UNKNOWN = "unknown"          # 没有可信额度信息，始终允许
    OBSERVED = "observed"        # 仅记录/展示实际用量，始终允许
    SOFT_LIMIT = "soft_limit"    # 达限后降权，但不阻断
    HARD_LIMIT = "hard_limit"    # 达限后拒绝新分配


@dataclass(frozen=True)
class BudgetDecision:
    """一次预算准入判断的结果。"""

    allowed: bool
    ranking_multiplier: float = 1.0
    reason: Optional[str] = None


class BudgetPolicy:
    """基于本地计数器的轻量预算策略。

    ``limits`` 使用与旧 ``quota_config['limits']`` 相同的 metric -> 上限结构。
    传入的 usage 为当前已观察到的累计值。未知或缺失的 metric 不会被视作耗尽，
    从而避免把没有余额接口的 Client/Harness 错误地排除在调度之外。
    """

    def __init__(self, mode: BudgetMode = BudgetMode.UNKNOWN,
                 limits: Optional[Dict[str, float]] = None,
                 soft_limit_multiplier: float = 0.1):
        self.mode = BudgetMode(mode)
        self.limits = dict(limits or {})
        self.soft_limit_multiplier = max(0.0, min(1.0, float(soft_limit_multiplier)))

    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]] = None) -> "BudgetPolicy":
        config = config or {}
        return cls(
            mode=config.get("mode", BudgetMode.UNKNOWN),
            limits=config.get("limits"),
            soft_limit_multiplier=config.get("soft_limit_multiplier", 0.1),
        )

    def evaluate(self, usage: Optional[Dict[str, Any]] = None) -> BudgetDecision:
        """根据已观察的累计用量决定准入与排序权重。"""
        if self.mode in (BudgetMode.UNKNOWN, BudgetMode.OBSERVED):
            return BudgetDecision(True)

        exhausted = self._first_exhausted_metric(usage or {})
        if exhausted is None:
            return BudgetDecision(True)

        key, current, limit = exhausted
        reason = "budget_limit_reached:%s=%s/%s" % (key, current, limit)
        if self.mode == BudgetMode.HARD_LIMIT:
            return BudgetDecision(False, 0.0, reason)
        return BudgetDecision(True, self.soft_limit_multiplier, reason)

    def _first_exhausted_metric(self, usage: Dict[str, Any]):
        for key, raw_limit in self.limits.items():
            try:
                limit = float(raw_limit)
                current = float(usage.get(key, 0))
            except (TypeError, ValueError):
                # 无法可靠解析的额度配置不能阻断正常调用。
                continue
            if limit >= 0 and current >= limit:
                return key, current, limit
        return None
