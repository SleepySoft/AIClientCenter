from unittest.mock import patch

from AIClientCenter.harness.cli import CodexCLIClient, KimiCLIClient
from AIClientCenter.providers.openai_compatible import OpenAICompatibleAPI


def test_codex_jsonl_parser_supports_current_event_envelope():
    client = CodexCLIClient(name="codex-test", usage_guard={"enabled": False})
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
    client = CodexCLIClient(name="codex-test", usage_guard={"enabled": False})
    stdout = (
        '{"type":"event_msg","payload":{"type":"task_complete","error":'
        '{"message":"{\\"error\\": {\\"code\\": \\"InvalidParameter\\", '
        '\\"type\\": \\"BadRequest\\", \\"message\\": \\"bad input\\"}}"}}}'
    )

    error = client._classify_error(1, stdout, "request_id=401-not-an-auth-error")

    assert error["type"] == "BAD_REQUEST"
    assert error["code"] == "HTTP_400"


def test_kimi_code_stream_json_parser_extracts_clean_text_and_session():
    client = KimiCLIClient(
        name="kimi-test", cli_command="kimi", cli_protocol="kimi-code")
    stdout = "\n".join([
        '{"role":"meta","type":"system.version","version":"2.6.0"}',
        '{"role":"assistant","content":"final answer"}',
        ('{"role":"meta","type":"session.resume_hint",'
         '"session_id":"session-123"}'),
    ])

    text, usage, session_id = client._parse_output(stdout, "", None)

    assert text == "final answer"
    assert usage == {}
    assert session_id == "session-123"


def test_kimi_code_commands_use_prompt_protocol_without_legacy_flags(tmp_path):
    agent_file = tmp_path / "json-agent.md"
    agent_file.write_text("---\ndescription: test\ntools: []\n---\ntest")
    client = KimiCLIClient(
        name="kimi-test", cli_command="kimi", cli_protocol="kimi-code",
        mode="stateless", model="kimi-model", agent_file=str(agent_file))

    command = client._build_exec_command("kimi-model", None)

    assert command == [
        "--output-format", "stream-json",
        "--agent-file", str(agent_file), "-m", "kimi-model",
    ]
    assert "--quiet" not in command
    assert "-w" not in command


def test_kimi_resume_does_not_repeat_agent_file(tmp_path):
    agent_file = tmp_path / "json-agent.md"
    agent_file.write_text("---\ndescription: test\ntools: []\n---\ntest")
    client = KimiCLIClient(
        name="kimi-test", cli_command="kimi", cli_protocol="kimi-code",
        mode="session", agent_file=str(agent_file))

    command = client._build_resume_command("session-1", None, None)

    assert command == ["--output-format", "stream-json", "--session", "session-1"]


def test_kimi_code_replaces_nul_before_passing_prompt_in_argv():
    client = KimiCLIClient(
        name="kimi-test", cli_command="kimi", cli_protocol="kimi-code")

    with patch("AIClientCenter.harness.cli.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = b'{"role":"assistant","content":"ok"}\n'
        run.return_value.stderr = b''
        client._run_cli([], "left\x00right", None)

    command = run.call_args.args[0]
    assert command[-2:] == ["--prompt", "left\ufffdright"]


def test_kimi_local_spawn_error_is_transient_not_permanent():
    client = KimiCLIClient(
        name="kimi-test", cli_command="kimi", cli_protocol="kimi-code")

    with patch(
            "AIClientCenter.harness.cli.subprocess.run",
            side_effect=ValueError("bad argv")):
        exit_code, stdout, stderr = client._run_cli([], "prompt", None)

    error = client._classify_error(exit_code, stdout, stderr)
    assert error["type"] == "TRANSIENT_SERVER"
    assert error["code"] == "LOCAL_EXECUTION_ERROR"


class _Response:
    def __init__(self, status_code, text="error body"):
        self.status_code = status_code
        self.text = text

    def json(self):
        return {}


def test_sync_http_error_classification_separates_auth_from_bad_request():
    api = OpenAICompatibleAPI("https://example.invalid/v1", token="test")
    api._attempt_sync_post = lambda *args, **kwargs: _Response(401)
    assert api._post_sync_unified("chat/completions", {}, False)["error"]["type"] == "PERMANENT"

    api._attempt_sync_post = lambda *args, **kwargs: _Response(400)
    assert api._post_sync_unified("chat/completions", {}, False)["error"]["type"] == "BAD_REQUEST"

    api._attempt_sync_post = lambda *args, **kwargs: _Response(410)
    assert api._post_sync_unified("chat/completions", {}, False)["error"]["type"] == "PERMANENT"


def test_sync_http_400_distinguishes_upstream_and_content_safety():
    api = OpenAICompatibleAPI("https://example.invalid/v1", token="test")
    api._attempt_sync_post = lambda *args, **kwargs: _Response(
        400, "Function id 'missing-function' was not found",
    )
    upstream = api._post_sync_unified("chat/completions", {}, False)["error"]
    assert upstream["type"] == "TRANSIENT_SERVER"
    assert upstream["code"] == "HTTP_400_UPSTREAM"

    api._attempt_sync_post = lambda *args, **kwargs: _Response(
        400, "Request rejected by content policy",
    )
    safety = api._post_sync_unified("chat/completions", {}, False)["error"]
    assert safety["type"] == "BAD_REQUEST"
    assert safety["code"] == "CONTENT_SAFETY"
