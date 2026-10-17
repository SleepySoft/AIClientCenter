import time

from AIClientCenter.core.manager import (
    AIClientManager,
    BaseAIClient,
    CircuitState,
    ClientStatus,
    HealthPolicy,
)
from AIClientCenter.core.budget import BudgetMode, BudgetPolicy


class FakeClient(BaseAIClient):
    def __init__(self, **kwargs):
        super().__init__(name="fake", api_token="test", **kwargs)

    def get_model_list(self):
        return {}

    def get_current_model(self):
        return "fake"

    def get_api_base_url(self):
        return "fake://"

    def _chat_completion_sync(self, messages, model=None, temperature=0.7,
                              max_tokens=4096, is_health_check=False):
        return {
            "success": True,
            "data": {
                "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 1},
            },
            "error": None,
        }


class SequencedClient(FakeClient):
    def __init__(self, results, **kwargs):
        super().__init__(**kwargs)
        self.results = list(results)
        self.calls = []

    def _chat_completion_sync(self, messages, model=None, temperature=0.7,
                              max_tokens=4096, is_health_check=False):
        self.calls.append({
            "messages": messages,
            "is_health_check": is_health_check,
        })
        return self.results.pop(0)


def _success_result():
    return {
        "success": True,
        "data": {
            "choices": [{"message": {"content": "business result"},
                         "finish_reason": "stop"}],
            "usage": {"total_tokens": 1},
        },
        "error": None,
    }


def _transient_result(code="HTTP_503"):
    return {
        "success": False,
        "data": None,
        "error": {"type": "TRANSIENT_SERVER", "code": code,
                  "message": "temporarily unavailable"},
    }


def _permanent_result():
    return {
        "success": False,
        "data": None,
        "error": {"type": "PERMANENT", "code": "HTTP_401",
                  "message": "invalid token"},
    }


def test_unavailable_client_can_be_recovered_by_a_health_check():
    client = FakeClient()
    client._update_client_status(ClientStatus.UNAVAILABLE)

    assert client.chat([{"role": "user", "content": "normal"}])["error"] == "client_unavailable"
    result = client.chat([{"role": "user", "content": "probe"}], is_health_check=True)

    assert "error" not in result
    assert client.get_status("status") == ClientStatus.AVAILABLE


def test_bad_request_is_fatal_for_the_task_but_does_not_disable_the_client():
    client = FakeClient()
    client._update_client_status(ClientStatus.AVAILABLE)

    result = client._handle_unified_error({
        "type": "BAD_REQUEST", "code": "HTTP_400", "message": "invalid prompt",
    })

    assert result["error_type"] == "fatal"
    assert client.get_status("status") == ClientStatus.AVAILABLE
    assert client.get_status("error_count") == 0


def test_quality_feedback_reorders_clients_without_marking_transport_error():
    manager = AIClientManager()
    first = FakeClient()
    first.name = "first"
    second = FakeClient()
    second.name = "second"
    manager.register_client(first)
    manager.register_client(second)

    manager.report_client_feedback(first, None, valid=False)

    selected = manager.get_available_client("next-task")
    assert selected is second
    assert first.get_status("quality_score") == 80.0
    assert first.get_status("error_count") == 0
    manager.release_client("next-task")


def test_force_acquire_allows_a_health_check_to_probe_an_unavailable_client():
    client = FakeClient()
    client._update_client_status(ClientStatus.UNAVAILABLE)

    assert client._acquire() is False
    assert client._acquire(force=True) is True
    client._release()


def test_base_client_exposes_budget_policy_without_requiring_metrics_mixin():
    client = FakeClient(budget_policy=BudgetPolicy(BudgetMode.HARD_LIMIT, {"request_count": 0}))

    decision = client.get_budget_decision()

    assert decision.allowed is False
    assert decision.reason.startswith("budget_limit_reached:")


def test_monitoring_starts_silently_and_first_real_request_validates_unknown_client():
    manager = AIClientManager()
    client = SequencedClient([_success_result()])
    manager.register_client(client)

    manager.start_monitoring()
    time.sleep(0.02)

    assert manager.monitor_thread is None
    assert manager._check_client_health() == 0
    assert client.calls == []
    assert client.get_status("status") == ClientStatus.UNKNOWN

    selected = manager.get_available_client("business-task")
    assert selected is client
    response = selected.chat([{"role": "user", "content": "analyze article"}])

    assert "error" not in response
    assert len(client.calls) == 1
    assert client.calls[0]["is_health_check"] is False
    assert client.get_status("status") == ClientStatus.AVAILABLE
    manager.release_client("business-task")
    manager.stop_monitoring()


def test_repeated_failures_recover_with_one_real_request_after_backoff():
    policy = HealthPolicy(
        failure_threshold=2,
        initial_backoff_sec=0.03,
        max_backoff_sec=0.03,
        jitter_ratio=0,
    )
    client = SequencedClient(
        [_transient_result(), _transient_result(), _success_result()],
        health_policy=policy,
    )
    manager = AIClientManager()
    manager.register_client(client)

    for user in ("first", "second"):
        selected = manager.get_available_client(user)
        assert selected is client
        assert "error" in selected.chat([{"role": "user", "content": user}])
        manager.release_client(user)

    assert client.get_status("circuit_state") == CircuitState.OPEN
    assert manager.get_available_client("too-early") is None

    selected = manager.wait_for_available_client("recovery-task", timeout=0.5)
    assert selected is client
    assert len(client.calls) == 2  # 到期只分配任务，还没有发送额外探测。
    assert client.get_status("circuit_state") == CircuitState.HALF_OPEN

    manager.release_client("recovery-task")
    assert client.get_status("circuit_state") == CircuitState.OPEN
    selected = manager.get_available_client("recovery-task")
    assert selected is client
    assert client.get_status("circuit_state") == CircuitState.HALF_OPEN

    response = selected.chat([{"role": "user", "content": "real recovery task"}])

    assert "error" not in response
    assert [call["is_health_check"] for call in client.calls] == [False, False, False]
    assert client.get_status("circuit_state") == CircuitState.CLOSED
    assert client.get_status("error_count") == 0
    manager.release_client("recovery-task")


def test_permanent_failure_is_quarantined_until_manual_recovery():
    client = SequencedClient([_permanent_result(), _success_result()])
    manager = AIClientManager()
    manager.register_client(client)

    selected = manager.get_available_client("task")
    assert "error" in selected.chat([{"role": "user", "content": "work"}])
    manager.release_client("task")

    assert client.get_status("status") == ClientStatus.UNAVAILABLE
    assert client.get_status("circuit_state") == CircuitState.QUARANTINED
    assert client._recovery_delay() is None
    assert manager.wait_for_available_client("later", timeout=0.03) is None
    assert len(client.calls) == 1

    assert manager.trigger_manual_check(client.name) is False
    # 手动检查请求已执行，但预置响应不包含健康检查要求的 OK，因此不会误判恢复。
    assert len(client.calls) == 2
    assert client.calls[-1]["is_health_check"] is True
