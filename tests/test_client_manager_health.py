from AIClientCenter.core.manager import BaseAIClient, ClientStatus
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
