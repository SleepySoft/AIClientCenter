import pytest

from AIClientCenter.providers.groq import (
    GROQ_GROUP_ID,
    GROQ_LARGE_MODEL_LIMITS,
    build_groq_clients,
)


def test_groq_builder_skips_missing_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    assert build_groq_clients() == {}


def test_groq_builder_only_enables_large_model_and_applies_free_limits(tmp_path):
    clients = build_groq_clients("test-key", storage_path=str(tmp_path / "limits.sqlite3"))

    assert set(GROQ_LARGE_MODEL_LIMITS) == {"openai/gpt-oss-120b"}
    assert len(clients) == 1
    client = next(iter(clients.values()))
    assert client.group_id == GROQ_GROUP_ID
    assert client.get_current_model() == "openai/gpt-oss-120b"
    assert client.completion_reserve_tokens == 2_048
    assert client.get_rate_limit_snapshot()["effective_limits"] == {
        "rpm": 24,
        "tpm": 6_400,
        "rpd": 800,
        "tpd": 160_000,
    }


def test_groq_builder_rejects_small_or_unregistered_models(monkeypatch, tmp_path):
    monkeypatch.setenv("GROQ_MODELS", "qwen/qwen3.8-27b")

    with pytest.raises(ValueError, match="质量门槛"):
        build_groq_clients("test-key", storage_path=str(tmp_path / "limits.sqlite3"))
