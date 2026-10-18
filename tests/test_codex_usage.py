import json

from AIClientCenter.harness.cli import CodexCLIClient
from AIClientCenter.harness.codex_usage import CodexUsageManager


def _limits(
        primary=40, secondary=30,
        primary_reset=2_000_000_000, secondary_reset=2_100_000_000,
):
    return {
        "primary": {
            "used_percent": primary,
            "window_minutes": 300,
            "resets_at": primary_reset,
        },
        "secondary": {
            "used_percent": secondary,
            "window_minutes": 10080,
            "resets_at": secondary_reset,
        },
        "plan_type": "plus",
    }


def test_guard_blocks_primary_or_secondary_until_reset(tmp_path):
    manager = CodexUsageManager(
        storage_path=tmp_path / "usage.db",
        primary_max_percent=65,
        secondary_max_percent=70,
        app_server_enabled=False,
        bootstrap_from_codex_sessions=False,
        clock=lambda: 1000,
    )
    manager.update_from_event(_limits(primary=65))
    decision = manager.evaluate()
    assert not decision.allowed
    assert decision.reason.startswith("codex_primary_limit:")

    manager.update_from_event(_limits(primary=20, secondary=71))
    decision = manager.evaluate()
    assert not decision.allowed
    assert decision.reason.startswith("codex_secondary_limit:")


def test_expired_window_allows_probe_and_snapshot_persists(tmp_path):
    path = tmp_path / "usage.db"
    manager = CodexUsageManager(
        storage_path=path,
        primary_max_percent=65,
        secondary_max_percent=70,
        app_server_enabled=False,
        bootstrap_from_codex_sessions=False,
        clock=lambda: 3000,
    )
    manager.update_from_event(_limits(primary=99, secondary=20, primary_reset=2000))
    assert manager.evaluate().allowed

    restored = CodexUsageManager(
        storage_path=path,
        primary_max_percent=65,
        secondary_max_percent=70,
        app_server_enabled=False,
        bootstrap_from_codex_sessions=False,
        clock=lambda: 1000,
    )
    assert restored.get_snapshot().primary.used_percent == 99
    assert not restored.evaluate().allowed


def test_missing_or_malformed_snapshot_does_not_deadlock_client(tmp_path):
    manager = CodexUsageManager(
        storage_path=tmp_path / "usage.db",
        app_server_enabled=False,
        bootstrap_from_codex_sessions=False,
    )
    assert manager.evaluate().allowed
    assert manager.update_from_event({"primary": {"used_percent": "bad"}}) is None
    assert manager.evaluate().allowed
    manager.update_from_event({"primary": {"used_percent": 100}})
    assert manager.evaluate().allowed


def test_codex_parser_updates_authoritative_window_snapshot(tmp_path):
    client = CodexCLIClient(
        name="codex-test",
        usage_guard={
            "storage_path": str(tmp_path / "usage.db"),
            "primary_max_percent": 65,
            "secondary_max_percent": 70,
            "app_server_enabled": False,
            "bootstrap_from_codex_sessions": False,
        },
    )
    event = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"last_token_usage": {"input_tokens": 11, "output_tokens": 5}},
            "rate_limits": _limits(primary=66),
        },
    }
    client._parse_output(json.dumps(event), "", None)

    snapshot = client.get_codex_usage_snapshot()
    assert snapshot.primary.window_minutes == 300
    assert snapshot.primary.used_percent == 66
    assert not client.get_budget_decision().allowed
    metrics = {item["key"]: item for item in client.get_standardized_metrics()}
    assert metrics["codex_primary_percent"]["current"] == 66


def test_bootstrap_reads_latest_codex_session_snapshot(tmp_path):
    session_dir = tmp_path / "codex" / "sessions" / "2026" / "10" / "18"
    session_dir.mkdir(parents=True)
    event = {
        "timestamp": "2026-10-18T08:00:00Z",
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {}, "rate_limits": _limits(primary=64)},
    }
    (session_dir / "rollout.jsonl").write_text(
        json.dumps(event) + "\n", encoding="utf-8")

    manager = CodexUsageManager(
        storage_path=tmp_path / "usage.db",
        codex_home=tmp_path / "codex",
        primary_max_percent=65,
        app_server_enabled=False,
    )
    assert manager.get_snapshot().primary.used_percent == 64


def test_running_manager_refreshes_usage_written_by_other_codex_session(tmp_path):
    now = [1000.0]
    codex_home = tmp_path / "codex"
    manager = CodexUsageManager(
        storage_path=tmp_path / "usage.db",
        codex_home=codex_home,
        primary_max_percent=65,
        app_server_enabled=False,
        session_refresh_seconds=30,
        clock=lambda: now[0],
    )
    assert manager.evaluate().allowed

    session_dir = codex_home / "sessions" / "2026" / "10" / "18"
    session_dir.mkdir(parents=True)
    event = {
        "timestamp": "2033-05-18T03:33:21Z",
        "type": "event_msg",
        "payload": {"type": "token_count", "rate_limits": _limits(primary=80)},
    }
    (session_dir / "rollout.jsonl").write_text(
        json.dumps(event) + "\n", encoding="utf-8")

    # Refresh is throttled until the configured polling interval has elapsed.
    assert manager.evaluate().allowed
    now[0] = 1031.0
    assert not manager.evaluate().allowed


def test_default_reserves_twenty_percent(tmp_path):
    manager = CodexUsageManager(
        storage_path=tmp_path / "usage.db",
        app_server_enabled=False,
        bootstrap_from_codex_sessions=False,
        clock=lambda: 1000,
    )
    assert manager.primary_max_percent == 80
    assert manager.secondary_max_percent == 80
    manager.update_from_event(_limits(primary=79))
    assert manager.evaluate().allowed
    manager.update_from_event(_limits(primary=80))
    assert not manager.evaluate().allowed


def test_reset_boundary_uses_provider_timestamp_plus_grace(tmp_path):
    now = [1999.0]
    manager = CodexUsageManager(
        storage_path=tmp_path / "usage.db",
        primary_max_percent=80,
        reset_grace_seconds=30,
        app_server_enabled=False,
        bootstrap_from_codex_sessions=False,
        clock=lambda: now[0],
    )
    manager.update_from_event(_limits(primary=90, primary_reset=2000))
    assert not manager.evaluate().allowed
    now[0] = 2029.0
    assert not manager.evaluate().allowed
    now[0] = 2030.0
    assert manager.evaluate().allowed


def test_app_server_camel_case_bucket_is_supported():
    snapshot = CodexUsageManager._parse_app_server_bucket({
        "planType": "plus",
        "rateLimitReachedType": None,
        "primary": {
            "usedPercent": 42,
            "windowDurationMins": 300,
            "resetsAt": 2000,
        },
        "secondary": {
            "usedPercent": 17,
            "windowDurationMins": 10080,
            "resetsAt": 9000,
        },
    }, observed_at=1000)
    assert snapshot.primary.used_percent == 42
    assert snapshot.primary.window_minutes == 300
    assert snapshot.secondary.window_minutes == 10080
    assert snapshot.plan_type == "plus"
