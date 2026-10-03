from flask import Flask

from AIClientCenter.AIClientManager import AIClientManager, BaseAIClient, ClientStatus
from AIClientCenter.AIClientManagerBackend import AIDashboardService


class DashboardClient(BaseAIClient):
    def __init__(self):
        super().__init__(name="dashboard-client", api_token="test")
        self._update_client_status(ClientStatus.AVAILABLE)
        self.calls = []

    def get_model_list(self):
        return {"dashboard-model": {}}

    def get_current_model(self):
        return "dashboard-model"

    def get_api_base_url(self):
        return "test://"

    def _chat_completion_sync(self, messages, model=None, temperature=0.7,
                              max_tokens=4096, is_health_check=False):
        self.calls.append({
            "messages": messages,
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
        })
        return {
            "success": True,
            "data": {
                "choices": [{"message": {"content": "manual-ok"}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 3},
            },
            "error": None,
        }


def make_app(enabled):
    manager = AIClientManager(first_check_delay_sec=9999)
    client = DashboardClient()
    manager.register_client(client)
    app = Flask(__name__)
    AIDashboardService(manager, enable_manual_calls=enabled).mount_to_app(app, "/dashboard")
    return app, manager, client


def test_manual_call_page_and_api_are_disabled_by_default():
    app, manager, _ = make_app(False)

    response = app.test_client().get("/dashboard/api/manual-call/clients")

    assert response.status_code == 403
    assert "disabled" in response.get_json()["error"]
    manager.stop_monitoring()


def test_manual_call_uses_named_client_and_releases_it_after_response():
    app, manager, client = make_app(True)
    test_client = app.test_client()

    page = test_client.get("/dashboard/playground")
    listed = test_client.get("/dashboard/api/manual-call/clients")
    response = test_client.post("/dashboard/api/manual-call", json={
        "client_name": "dashboard-client",
        "system_prompt": "Reply concisely.",
        "prompt": "Say manual-ok",
        "model": "override-model",
        "temperature": 0.2,
        "max_tokens": 77,
    })

    assert page.status_code == 200
    assert "手动调用 Client" in page.get_data(as_text=True)
    assert listed.status_code == 200
    assert listed.get_json()["clients"][0]["name"] == "dashboard-client"
    assert response.status_code == 200
    assert response.get_json()["response"]["choices"][0]["message"]["content"] == "manual-ok"
    assert client.calls[0]["model"] == "override-model"
    assert client.calls[0]["max_tokens"] == 77
    assert client._is_acquired() is False
    assert manager.user_client_map == {}
    manager.stop_monitoring()


def test_manual_call_validates_the_request_before_allocating_client():
    app, manager, client = make_app(True)

    response = app.test_client().post("/dashboard/api/manual-call", json={
        "client_name": "dashboard-client",
        "prompt": "x",
        "max_tokens": 99999,
    })

    assert response.status_code == 400
    assert client.calls == []
    assert manager.user_client_map == {}
    manager.stop_monitoring()
