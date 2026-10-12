import pytest

from AIClientCenter.core.credentials import resolve_api_keys


def test_resolve_api_keys_prefers_direct_mapping_over_environment(monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_KEY", "environment-key")

    credentials = resolve_api_keys(
        {"Primary Project": "direct-key"},
        env_name="TEST_PROVIDER_KEY",
    )

    assert len(credentials) == 1
    assert credentials[0].alias == "primary_project"
    assert credentials[0].api_key == "direct-key"
    assert "direct-key" not in credentials[0].quota_id


def test_resolve_api_keys_falls_back_to_environment(monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_KEY", "environment-key")

    credentials = resolve_api_keys({}, env_name="TEST_PROVIDER_KEY")

    assert len(credentials) == 1
    assert credentials[0].alias == "env"
    assert credentials[0].api_key == "environment-key"


def test_resolve_api_keys_rejects_duplicate_secret():
    with pytest.raises(ValueError, match="同一个 API Key"):
        resolve_api_keys(
            {"first": "same-key", "second": "same-key"},
            env_name="TEST_PROVIDER_KEY",
        )
