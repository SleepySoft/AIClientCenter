from AIClientCenter.harness.cli import CodexCLIClient
from AIClientCenter.providers.openai_compatible import OpenAICompatibleAPI


def test_codex_jsonl_parser_supports_current_event_envelope():
    client = CodexCLIClient(name="codex-test")
    stdout = "\n".join([
        '{"type":"session_meta","payload":{"session_id":"session-1"}}',
        ('{"type":"event_msg","payload":{"type":"item_completed","item":'
         '{"type":"AgentMessage","content":[{"text":"intermediate"}]}}}'),
        ('{"type":"event_msg","payload":{"type":"token_count","info":'
         '{"last_token_usage":{"input_tokens":11,"cached_input_tokens":7,"output_tokens":5}}}}'),
        '{"type":"event_msg","payload":{"type":"task_complete","last_agent_message":"final"}}',
    ])

    text, usage, session_id = client._parse_output(stdout, "", None)

    assert text == "final"
    assert session_id == "session-1"
    assert usage == {
        "prompt_tokens": 11,
        "completion_tokens": 5,
        "total_tokens": 16,
        "cached_input_tokens": 7,
    }


def test_codex_jsonl_bad_request_is_not_misclassified_by_request_id_text():
    client = CodexCLIClient(name="codex-test")
    stdout = (
        '{"type":"event_msg","payload":{"type":"task_complete","error":'
        '{"message":"{\\"error\\": {\\"code\\": \\"InvalidParameter\\", '
        '\\"type\\": \\"BadRequest\\", \\"message\\": \\"bad input\\"}}"}}}'
    )

    error = client._classify_error(1, stdout, "request_id=401-not-an-auth-error")

    assert error["type"] == "BAD_REQUEST"
    assert error["code"] == "HTTP_400"


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code
        self.text = "error body"

    def json(self):
        return {}


def test_sync_http_error_classification_separates_auth_from_bad_request():
    api = OpenAICompatibleAPI("https://example.invalid/v1", token="test")
    api._attempt_sync_post = lambda *args, **kwargs: _Response(401)
    assert api._post_sync_unified("chat/completions", {}, False)["error"]["type"] == "PERMANENT"

    api._attempt_sync_post = lambda *args, **kwargs: _Response(400)
    assert api._post_sync_unified("chat/completions", {}, False)["error"]["type"] == "BAD_REQUEST"
