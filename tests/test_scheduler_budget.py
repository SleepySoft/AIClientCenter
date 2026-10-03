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


class PassiveHealthClient(SchedulingClient):
    active_health_checks = False

    def __init__(self):
        super().__init__("passive", 1)
        self.probe_count = 0

    def _test_and_update_status(self):
        self.probe_count += 1
        return True


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
    assert client.calculate_health() == 100.0


def test_legacy_balance_threshold_becomes_an_explicit_hard_budget_gate():
    client = MetricsClient(balance_config={"hard_threshold": 1.5})
    client.update_balance(1.5)

    assert client.get_budget_decision().allowed is False


def test_passive_harness_style_client_is_not_periodically_probed():
    manager = AIClientManager(first_check_delay_sec=9999)
    client = PassiveHealthClient()
    manager.register_client(client)

    manager._check_client_health()

    assert client.probe_count == 0
    manager.stop_monitoring()


def test_client_stats_expose_budget_separately_from_runtime_health():
    manager = AIClientManager(first_check_delay_sec=9999)
    client = MetricsClient(quota_config={"limits": {"request_count": 0}})
    manager.register_client(client)

    details = manager.get_client_stats()["clients"][0]

    assert details["state"]["health_score"] == 100.0
    assert details["budget"]["mode"] == "hard_limit"
    assert details["budget"]["admitted"] is False
    manager.stop_monitoring()


def test_explicit_client_name_returns_that_client_without_falling_back():
    manager = AIClientManager(first_check_delay_sec=9999)
    default = SchedulingClient("default", 0)
    named = SchedulingClient("named", 50)
    manager.register_client(default)
    manager.register_client(named)

    assert manager.get_available_client("user", target_client_name="named") is named
    manager.stop_monitoring()
