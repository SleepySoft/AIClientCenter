import time

from flask import Flask

from AIClientCenter.core.manager import AIClientManager, BaseAIClient, ClientStatus
from AIClientCenter.core.observation import CallObservationSQLiteLogger
from AIClientCenter.web.dashboard import AIDashboardService


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


def test_observation_page_reports_disabled_without_observer():
    app, manager, _ = make_app(False)
    test_client = app.test_client()

    page = test_client.get("/dashboard/observations")
    payload = test_client.get("/dashboard/api/observations").get_json()

    assert page.status_code == 200
    assert "AI 调用观测" in page.get_data(as_text=True)
    assert payload["enabled"] is False
    manager.stop_monitoring()


def test_observation_api_returns_overview_series_breakdown_and_recent(tmp_path):
    observer = CallObservationSQLiteLogger(str(tmp_path / "calls.sqlite3"))
    manager = AIClientManager(first_check_delay_sec=9999, call_observer=observer)
    client = DashboardClient()
    client.set_observation_context(provider="example", account_alias="account-a")
    manager.register_client(client)
    client.chat([{"role": "user", "content": "hello"}])
    older = time.time() - 7200
    observer.record({
        "request_id": "older-call", "started_at": older, "ts": older + 0.1,
        "provider": "example", "model": "dashboard-model",
        "client_name": "dashboard-client", "account_alias": "account-a",
        "success": True, "elapsed_ms": 100,
    })
    app = Flask(__name__)
    AIDashboardService(manager).mount_to_app(app, "/dashboard")

    response = app.test_client().get(
        "/dashboard/api/observations?hours=24&dimension=provider&limit=10"
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["enabled"] is True
    assert payload["overall"]["request_count"] == 2
    assert payload["overall"]["reported_token_calls"] == 1
    assert payload["breakdown"][0]["provider"] == "example"
    assert len(payload["series"]) == 2
    assert [item["time_bucket"] for item in payload["series"]] == sorted(
        item["time_bucket"] for item in payload["series"]
    )
    assert payload["recent"][0]["account_alias"] == "account-a"
    assert "messages" not in payload["recent"][0]
    manager.stop_monitoring()


def test_observation_api_rejects_unbounded_or_unknown_slices(tmp_path):
    observer = CallObservationSQLiteLogger(str(tmp_path / "calls.sqlite3"))
    manager = AIClientManager(first_check_delay_sec=9999, call_observer=observer)
    app = Flask(__name__)
    AIDashboardService(manager).mount_to_app(app, "/dashboard")
    test_client = app.test_client()

    assert test_client.get(
        "/dashboard/api/observations?dimension=sql"
    ).status_code == 400
    assert test_client.get(
        "/dashboard/api/observations?hours=99999"
    ).status_code == 400
    manager.stop_monitoring()
