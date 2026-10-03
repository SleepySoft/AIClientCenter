import os
import json
import time
import threading
from typing import Dict, Any, List, Optional, Union
from collections import Counter

try:
    from .BudgetPolicy import BudgetMode, BudgetPolicy
except ImportError:
    from BudgetPolicy import BudgetMode, BudgetPolicy

# Constants for Metric Types
METRIC_TYPE_USAGE = "USAGE_LIMIT"  # Logic: Healthy when current < target
METRIC_TYPE_BALANCE = "BALANCE_THRESHOLD"  # Logic: Healthy when current > target


class ClientMetricsMixin:
    """
    A unified Mixin for managing AI Client statistics, quotas, and financial balances.

    Architecture Note:
    ------------------
    We separate 'Periodic Stats' (Quota) from 'Balance' (Wallet) because they have
    different lifecycles:
    1. Periodic Stats: Reset automatically (e.g., every 30 days).
    2. Balance: Persistent; only changes via manual external updates (top-up/deduction).

    Both are persisted to the same state file for atomicity.
    """

    def __init__(self,
                 quota_config: Optional[Dict[str, Any]] = None,
                 balance_config: Optional[Dict[str, float]] = None,
                 state_file_path: Optional[str] = None,
                 budget_policy: Optional[BudgetPolicy] = None,
                 *args, **kwargs):
        """
        Initialize the metrics subsystem.

        Args:
            quota_config: Usage limits (e.g., {'period_days': 30, 'limits': {'total_tokens': 1000}}).
            balance_config: Financial rules (e.g., {'hard_threshold': 1.0}).
            state_file_path: JSON path for persisting both usage stats and balance.
            *args, **kwargs: Passed to super() to maintain MRO chain.
        """
        super().__init__(*args, budget_policy=budget_policy, **kwargs)

        self.quota_config = quota_config or {}
        self.balance_config = balance_config or {}
        self.state_file_path = state_file_path

        self._metrics_lock = threading.Lock()

        # Lifetime stats (In-memory only, resets on process restart)
        self._lifetime_stats = Counter()

        # Periodic stats (Persisted, resets on time intervals)
        self._periodic_stats = Counter()
        self._last_reset_time = 0.0

        # Financial Balance (Persisted, NEVER resets automatically)
        self._balance = 0.0

        if self.state_file_path:
            self._load_state()

        # 兼容旧配置：原 quota/balance 均是硬性准入条件。新的 policy
        # 把该判断从 health score 中独立出来，未知额度则保持默认 unknown。
        if budget_policy is None and (self.quota_config or self.balance_config):
            self.budget_policy = BudgetPolicy(
                BudgetMode.HARD_LIMIT,
                limits=self.quota_config.get('limits', {}),
                minimums={"balance": self.balance_config['hard_threshold']}
                if 'hard_threshold' in self.balance_config else {},
                period_days=self.quota_config.get('period_days', 30),
            )

    def set_usage_constraints(self,
                              max_tokens: Optional[int] = None,
                              period_days: int = 30,
                              min_balance: Optional[float] = None,
                              target_metric: str = 'total_tokens'):
        """
        Convenience method to quickly configure usage limits and balance thresholds.

        Args:
            max_tokens: Max allowed usage for the target metric. None to disable quota.
            period_days: Number of days before the usage counter resets (default: 30).
            min_balance: Minimum funds required. None to disable balance check.
            target_metric: The metric key to track (default: 'total_tokens').
        """
        with self._metrics_lock:
            # 1. Configure Quota
            if max_tokens is not None:
                self.quota_config = {
                    'period_days': period_days,
                    'limits': {target_metric: max_tokens}
                }
                if self._last_reset_time == 0:
                    self._last_reset_time = time.time()
            else:
                self.quota_config = {}

            # 2. Configure Balance Thresholds
            if min_balance is not None:
                self.balance_config = {'hard_threshold': min_balance}
            else:
                self.balance_config = {}

            self.budget_policy = BudgetPolicy(
                BudgetMode.HARD_LIMIT,
                limits=self.quota_config.get('limits', {}),
                minimums={"balance": self.balance_config['hard_threshold']}
                if 'hard_threshold' in self.balance_config else {},
                period_days=self.quota_config.get('period_days', period_days),
            )

            # 3. Persist configuration changes
            if self.state_file_path:
                self._save_state_unsafe()

    def record_usage(self, usage_data: Dict[str, Any]):
        """
        Records usage statistics (deltas) into lifetime and periodic counters.

        Key Conventions:
        ----------------
        Although this method accepts any numeric keys, the following standard keys
        are recommended to ensure compatibility with the quota system:

        1. 'total_tokens' (int):
           Sum of input + output tokens.
           *REQUIRED* if you use the default quota configuration.

        2. 'prompt_tokens' (int):
           Input tokens. Useful for analytics.

        3. 'completion_tokens' (int):
           Output tokens. Useful for analytics.

        4. 'request_count' (int):
           Usually set to 1 per call. Useful for limiting API calls per month.

        5. 'cost_usd' (float):
           Estimated cost. Useful for tracking spending logic internally.

        Example:
            # Good Practice
            client.record_usage({
                'prompt_tokens': 50,
                'completion_tokens': 150,
                'total_tokens': 200,  # Explicitly provided for quota check
                'request_count': 1
            })

        Args:
            usage_data: Dict containing numeric values (e.g., {'total_tokens': 150}).
        """
        numeric_data = {k: v for k, v in usage_data.items() if isinstance(v, (int, float))}
        increment = Counter(numeric_data)

        with self._metrics_lock:
            # 1. Update lifetime stats (Memory only)
            self._lifetime_stats.update(increment)
            self._lifetime_stats['last_update'] = time.time()

            # 2. 对任何有本地 token/request 预算的策略维护周期计数。
            # soft_limit 不能只配置 policy 而不配置旧 quota_config，否则永远不会降权。
            if self._has_periodic_budget_unsafe():
                self._check_and_reset_period_unsafe()
                self._periodic_stats.update(increment)
                self._save_state_unsafe()

    def update_balance(self, amount: float, mode: str = 'set'):
        """
        Updates the financial balance.

        Args:
            amount (float): The value to set or add.
            mode (str): 'set' to overwrite, 'add' to increment (top-up), 'sub' to deduct.
        """
        with self._metrics_lock:
            if mode == 'set':
                self._balance = amount
            elif mode == 'add':
                self._balance += amount
            elif mode == 'sub':
                self._balance -= amount
            else:
                raise ValueError(f"Invalid balance update mode: {mode}")

            # Persist immediately on financial change
            if self.state_file_path:
                self._save_state_unsafe()

    def get_balance(self) -> float:
        with self._metrics_lock:
            return self._balance

    def get_usage_stats(self) -> Dict[str, Any]:
        with self._metrics_lock:
            return dict(self._lifetime_stats)

    def get_budget_usage(self) -> Dict[str, Any]:
        """返回可用于本地预算准入的周期用量和余额快照。"""
        with self._metrics_lock:
            if self._has_periodic_budget_unsafe():
                self._check_and_reset_period_unsafe()
            usage = dict(self._periodic_stats)
            usage['balance'] = self._balance
            return usage

    def get_standardized_metrics(self) -> List[Dict[str, Any]]:
        """
        Returns a list of standardized metrics for health calculation.
        Schema: {'type': str, 'key': str, 'current': float, 'target': float}
        """
        metrics = []
        with self._metrics_lock:
            # 1. Quota Metrics
            if self.quota_config:
                self._check_and_reset_period_unsafe()
                limits = self.quota_config.get('limits', {})
                for key, limit in limits.items():
                    metrics.append({
                        "type": METRIC_TYPE_USAGE,
                        "key": key,
                        "current": self._periodic_stats.get(key, 0),
                        "target": limit
                    })

            # 2. Balance Metrics (Now uses the persistent _current_balance)
            if self.balance_config:
                metrics.append({
                    "type": METRIC_TYPE_BALANCE,
                    "key": "balance",
                    "current": self._balance,
                    "target": self.balance_config.get('hard_threshold', 0.0)
                })
        return metrics

    def calculate_health(self) -> float:
        """
        兼容旧界面的运行态健康分数。

        配额和余额已由 BudgetPolicy 单独表达，不能再作为 Client 运行健康度：
        未知额度的 Harness 与可观测但不精确的 API 都应保持可执行。
        调度器请使用 ``get_budget_decision()``，管理页请读取 ``budget`` 字段。
        """
        return 100.0

    def increase_quota(self, additional_amount: int, metric_key: Optional[str] = None):
        """
        Increases the quota limit for the current period by a specific amount (Incremental Update).

        Behavior:
        1. **Preserves History**: This method does NOT clear the 'used' statistics. It simply raises the ceiling (the Limit).
        2. **Instant Revival**: If a client is currently marked as 'dead' (Health=0) because `Usage >= Limit`,
           calling this method will immediately satisfy the condition `Usage < New_Limit`.
           Consequently, `calculate_health()` will return a positive score, and the Manager will
           automatically start routing traffic to this client again.
        3. **Persistence**: The new limit is immediately saved to the JSON state file (if configured).

        Use Case:
        - A user purchases a "Traffic Booster Pack" or "Add-on Package".
        - An administrator temporarily increases the limit for emergency testing.

        Args:
            additional_amount (int): The amount to add to the current limit (e.g., +1000 requests).
            metric_key (Optional[str]): The specific metric key to increase (e.g., 'total_tokens', 'request_count').
                                        If None, it defaults to the first metric defined in the config.
        """
        with self._metrics_lock:
            if not self.quota_config or 'limits' not in self.quota_config:
                target = metric_key or 'total_tokens'
                self.quota_config = {'limits': {target: 0}, 'period_days': 30}

            limits = self.quota_config['limits']
            target_key = metric_key or next(iter(limits.keys()))

            limits[target_key] = limits.get(target_key, 0) + additional_amount

            if self.state_file_path:
                self._save_state_unsafe()

    # --- Internal Helpers ---

    def _check_and_reset_period_unsafe(self):
        """Internal: Checks if period elapsed and resets USAGE stats only."""
        period_days = self.quota_config.get(
            'period_days', getattr(self.budget_policy, 'period_days', 30))
        if period_days <= 0: return

        if time.time() - self._last_reset_time >= period_days * 86400:
            self._periodic_stats.clear()  # Reset usage
            self._last_reset_time = time.time()
            # Note: Balance is NOT reset here.

    def _has_periodic_budget_unsafe(self) -> bool:
        """Whether observed request/token usage must be retained for a local budget policy."""
        if self.quota_config:
            return True
        policy = self.budget_policy
        return policy.mode in (BudgetMode.SOFT_LIMIT, BudgetMode.HARD_LIMIT) and bool(policy.limits)

    def _save_state_unsafe(self):
        """Internal: Persists both periodic stats and balance to disk."""
        if not self.state_file_path: return
        try:
            data = {
                'last_reset_time': self._last_reset_time,
                'periodic_usage': dict(self._periodic_stats),
                'balance': self._balance  # Persist balance alongside stats
            }
            # Use a temp file + rename for atomic write safety in production
            temp_path = self.state_file_path + ".tmp"
            with open(temp_path, 'w') as f:
                json.dump(data, f, indent=2)
            os.replace(temp_path, self.state_file_path)
        except Exception as e:
            print(f"Metrics save error: {e}")

    def _load_state(self):
        """Internal: Loads state from disk."""
        if not self.state_file_path or not os.path.exists(self.state_file_path): return
        try:
            with self._metrics_lock:
                with open(self.state_file_path, 'r') as f:
                    data = json.load(f)
                    self._last_reset_time = data.get('last_reset_time', 0)
                    self._periodic_stats = Counter(data.get('periodic_usage', {}))
                    # Safely load balance, default to 0.0
                    self._balance = float(data.get('balance', 0.0))
        except Exception:
            pass  # Start fresh on error
