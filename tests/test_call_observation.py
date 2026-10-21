from AIClientCenter.core.manager import AIClientManager, BaseAIClient
from AIClientCenter.core.observation import CallObservationSQLiteLogger


class ObservedClient(BaseAIClient):
    def __init__(self, *, with_usage: bool = True, fail: bool = False):
        super().__init__(
            name="Observed Client",
            api_token="secret",
            group_id="observed-group",
            observation_context={
                "provider": "test-provider",
                "account_alias": "account-a",
            },
        )
        self.with_usage = with_usage
        self.fail = fail

    def get_model_list(self):
        return {"data": []}

    def get_current_model(self):
        return "test/model"

    def get_api_base_url(self):
        return "https://example.invalid/v1"

    def _chat_completion_sync(self, *args, **kwargs):
        if self.fail:
            return {
                "success": False,
                "data": None,
                "error": {
                    "type": "TRANSIENT_SERVER",
                    "code": "HTTP_503",
                    "message": "temporarily unavailable",
                },
            }
        data = {
            "choices": [{
                "message": {"role": "assistant", "content": "测试回复"},
                "finish_reason": "stop",
            }],
        }
        if self.with_usage:
            data["usage"] = {
                "prompt_tokens": 12,
                "completion_tokens": 4,
                "total_tokens": 16,
                "prompt_tokens_details": {"cached_tokens": 8},
                "completion_tokens_details": {"reasoning_tokens": 2},
            }
        return {"success": True, "data": data, "error": None}


def test_observer_records_generic_call_metrics_and_optional_feedback(tmp_path):
    observer = CallObservationSQLiteLogger(str(tmp_path / "observations.sqlite3"))
    manager = AIClientManager(call_observer=observer)
    client = ObservedClient()
    side_events = []
    client.add_event_sink(side_events.append)
    manager.register_client(client)

    response = client.chat([{"role": "user", "content": "测试输入"}])
    request_id = response["_ai_client_meta"]["request_id"]
    rows = observer.query_calls()

    assert len(rows) == 1
    row = rows[0]
    assert row["request_id"] == request_id
    assert row["provider"] == "test-provider"
    assert row["account_alias"] == "account-a"
    assert row["model"] == "test/model"
    assert row["success"] is True
    assert row["logical_input_bytes"] > 0
    assert row["logical_output_bytes"] > 0
    assert row["reported_input_tokens"] == 12
    assert row["reported_output_tokens"] == 4
    assert row["reported_total_tokens"] == 16
    assert row["cached_input_tokens"] == 8
    assert row["uncached_input_tokens"] == 4
    assert row["reasoning_output_tokens"] == 2
    assert row["token_source"] == "reported"
    assert any(event["type"] == "chat_end" for event in side_events)

    assert observer.feedback(
        request_id,
        valid=True,
        score=0.9,
        labels={"format": "json", "reply_valid": True},
    ) is True
    updated = observer.query_calls()[0]
    assert updated["feedback_valid"] is True
    assert updated["feedback_score"] == 0.9
    assert updated["feedback"] == {"format": "json", "reply_valid": True}


def test_observer_uses_estimates_when_provider_has_no_usage(tmp_path):
    observer = CallObservationSQLiteLogger(str(tmp_path / "observations.sqlite3"))
    client = ObservedClient(with_usage=False)
    observer.attach_client(client)

    client.chat([{"role": "user", "content": "hello world"}])
    row = observer.query_calls()[0]

    assert row["token_source"] == "estimated"
    assert row["estimated_input_tokens"] > 0
    assert row["estimated_output_tokens"] > 0
    assert row["reported_total_tokens"] is None


def test_observer_records_failed_calls_without_affecting_client_result(tmp_path):
    observer = CallObservationSQLiteLogger(str(tmp_path / "observations.sqlite3"))
    client = ObservedClient(fail=True)
    observer.attach_client(client)

    response = client.chat([{"role": "user", "content": "hello"}])
    row = observer.query_calls()[0]

    assert response["error"] == "unified_api_error"
    assert response["_ai_client_meta"]["request_id"] == row["request_id"]
    assert row["success"] is False
    assert row["error_type"] == "TRANSIENT_SERVER"
    assert row["error_code"] == "HTTP_503"
    assert row["logical_output_bytes"] > 0


def test_observer_summarizes_by_safe_dimensions(tmp_path):
    observer = CallObservationSQLiteLogger(str(tmp_path / "observations.sqlite3"))
    client = ObservedClient()
    observer.attach_client(client)
    response = client.chat([{"role": "user", "content": "hello"}])
    observer.feedback(response["_ai_client_meta"]["request_id"], valid=True)

    summary = observer.summarize(
        group_by=("provider", "model"),
        time_bucket="hour",
    )

    assert summary[0]["provider"] == "test-provider"
    assert summary[0]["model"] == "test/model"
    assert summary[0]["time_bucket"].endswith(":00:00Z")
    assert summary[0]["request_count"] == 1
    assert summary[0]["success_rate"] == 1.0
    assert summary[0]["input_tokens"] == 12
    assert summary[0]["output_tokens"] == 4
    assert summary[0]["cached_input_tokens"] == 8
    assert summary[0]["uncached_input_tokens"] == 4
    assert summary[0]["feedback_valid_rate"] == 1.0
