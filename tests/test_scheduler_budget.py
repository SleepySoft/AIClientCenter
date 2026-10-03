from AIClientCenter.AIClientManager import AIClientManager, BaseAIClient, ClientStatus
from AIClientCenter.BudgetPolicy import BudgetMode, BudgetPolicy
from AIClientCenter.LimitMixins import ClientMetricsMixin


class SchedulingClient(BaseAIClient):
    def __init__(self, name, priority, budget_policy=None, **kwargs):
        super().__init__(name=name, api_token="test", priority=priority,
                         budget_policy=budget_policy, **kwargs)
        self._update_client_status(ClientStatus.AVAILABLE)

    def get_model_list(self):
        return {}

    def get_current_model(self):
        return "test"

    def get_api_base_url(self):
        return "test://"

    def _chat_completion_sync(self, *args, **kwargs):
        raise AssertionError("scheduler tests must not invoke a client")


class MetricsClient(ClientMetricsMixin, SchedulingClient):
    def __init__(self, quota_config=None, balance_config=None):
        super().__init__(name="metrics", priority=1, quota_config=quota_config,
                         balance_config=balance_config)


def test_hard_budget_limit_blocks_client_and_allows_fallback():
    manager = AIClientManager(first_check_delay_sec=9999)
    blocked = SchedulingClient(
        "blocked", 0, BudgetPolicy(BudgetMode.HARD_LIMIT, {"request_count": 0}),
    )
    fallback = SchedulingClient("fallback", 50)
    manager.register_client(blocked)
    manager.register_client(fallback)

    selected = manager.get_available_client("user")

    assert selected is fallback
    manager.stop_monitoring()


def test_soft_budget_limit_is_deprioritized_but_remains_selectable():
    manager = AIClientManager(first_check_delay_sec=9999)
    soft_limited = SchedulingClient(
        "soft", 0, BudgetPolicy(BudgetMode.SOFT_LIMIT, {"request_count": 0}),
    )
    normal = SchedulingClient("normal", 50)
    manager.register_client(soft_limited)
    manager.register_client(normal)

    assert manager.get_available_client("first") is normal
    manager.release_client("first")
    manager.clients.remove(normal)
    assert manager.get_available_client("second") is soft_limited
    manager.stop_monitoring()


def test_legacy_metrics_quota_becomes_an_explicit_hard_budget_gate():
    client = MetricsClient(quota_config={"limits": {"request_count": 1}})
    client.record_usage({"request_count": 1})

    assert client.get_budget_decision().allowed is False


def test_legacy_balance_threshold_becomes_an_explicit_hard_budget_gate():
    client = MetricsClient(balance_config={"hard_threshold": 1.5})
    client.update_balance(1.5)

    assert client.get_budget_decision().allowed is False
