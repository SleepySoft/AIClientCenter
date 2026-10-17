import pytest

from AIClientCenter.core.manager import (
    CLIENT_PRIORITY_CONSUMABLES,
    CLIENT_PRIORITY_FREEBIE,
)
from AIClientCenter.providers.nvidia import (
    DEFAULT_MODELS,
    build_nvidia_clients,
    nvidia_group_limits,
)


@pytest.fixture(autouse=True)
def clean_nvidia_environment(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("NVIDIA_MODELS", raising=False)
    monkeypatch.delenv("NVIDIA_ACCOUNT_RPM", raising=False)
    monkeypatch.delenv("NVIDIA_QUOTA_SAFETY_RATIO", raising=False)


def test_nvidia_builder_skips_missing_key():
    assert build_nvidia_clients() == {}


def test_nvidia_builder_isolates_accounts_and_shares_each_account_limiter(tmp_path):
    clients = build_nvidia_clients(
        {"account-a": "nvapi-test-a", "account-b": "nvapi-test-b"},
        storage_path=str(tmp_path / "limits.sqlite3"),
    )

    assert len(clients) == len(DEFAULT_MODELS) * 2
    account_a = [client for client in clients.values() if "[account-a]" in client.name]
    account_b = [client for client in clients.values() if "[account-b]" in client.name]
    assert len({id(client.rate_limiter) for client in account_a}) == 1
    assert len({id(client.rate_limiter) for client in account_b}) == 1
    assert account_a[0].rate_limiter is not account_b[0].rate_limiter
    assert account_a[0].group_id != account_b[0].group_id
    assert account_a[0].priority == CLIENT_PRIORITY_FREEBIE
    assert nvidia_group_limits(clients) == {
        account_a[0].group_id: 1,
        account_b[0].group_id: 1,
    }
    snapshot = account_a[0].get_rate_limit_snapshot()
    assert snapshot["effective_limits"]["rpm"] == 24
    assert "nvapi-test" not in snapshot["storage_namespace"]
    assert DEFAULT_MODELS == ("z-ai/glm-5.3",)
    assert "moonshotai/kimi-k3" not in DEFAULT_MODELS
    assert all(client.health_policy.failure_threshold == 1 for client in clients.values())
    assert all(client.health_policy.initial_backoff_sec == 300 for client in clients.values())
    request = account_a[0].api._prepare_request_data(
        messages=[{"role": "user", "content": "test"}],
        temperature=0,
        max_tokens=8192,
    )
    assert request["temperature"] == 1.0
    assert request["top_p"] == 0.95
    assert request["reasoning_effort"] == "low"
    assert request["chat_template_kwargs"] == {"clear_thinking": True}
    assert request["max_tokens"] == 4096


def test_nvidia_builder_assigns_primary_and_reserve_priorities(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "NVIDIA_MODELS",
        "nvidia/nemotron-3-ultra-550b-a55b,z-ai/glm-5.3-flash",
    )

    clients = build_nvidia_clients(
        {"account": "nvapi-test"},
        storage_path=str(tmp_path / "limits.sqlite3"),
    )
    by_model = {client.get_current_model(): client for client in clients.values()}

    assert by_model["nvidia/nemotron-3-ultra-550b-a55b"].priority == (
        CLIENT_PRIORITY_CONSUMABLES
    )
    assert by_model["z-ai/glm-5.3-flash"].priority == CLIENT_PRIORITY_CONSUMABLES
    nemotron_request = by_model[
        "nvidia/nemotron-3-ultra-550b-a55b"
    ].api._prepare_request_data(
        messages=[{"role": "user", "content": "test"}],
        temperature=0,
        max_tokens=8192,
    )
    assert nemotron_request["temperature"] == 1.0
    assert nemotron_request["top_p"] == 0.95
    assert nemotron_request["chat_template_kwargs"] == {"enable_thinking": False}
    assert nemotron_request["max_tokens"] == 4096


def test_nvidia_builder_rejects_unregistered_or_deprecated_model(monkeypatch, tmp_path):
    monkeypatch.setenv("NVIDIA_MODELS", "deepseek-ai/deepseek-v3.2")

    with pytest.raises(ValueError, match="已弃用"):
        build_nvidia_clients(
            {"account": "nvapi-test"},
            storage_path=str(tmp_path / "limits.sqlite3"),
        )
