import pytest

from AIClientCenter.providers.openai_compatible import (
    create_cloudflare_client,
    create_gemini_client,
    create_groq_client,
    create_mistral_client,
    create_openrouter_client,
)


@pytest.mark.parametrize(
    ("factory", "env_name", "expected_url", "expected_model"),
    [
        (create_groq_client, "GROQ_API_KEY", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
        (create_openrouter_client, "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "openrouter/free"),
        (create_mistral_client, "MISTRAL_API_KEY", "https://api.mistral.ai/v1", "mistral-small-latest"),
    ],
)
def test_openai_compatible_provider_factories_use_environment(
        monkeypatch, factory, env_name, expected_url, expected_model):
    monkeypatch.setenv(env_name, "test-key")
    api = factory()

    assert api.get_api_token() == "test-key"
    assert api.get_api_base_url() == expected_url
    assert api.get_using_model() == expected_model


def test_cloudflare_factory_requires_and_embeds_account_id(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    with pytest.raises(ValueError, match="CLOUDFLARE_ACCOUNT_ID"):
        create_cloudflare_client(token="test")

    api = create_cloudflare_client(token="test", account_id="account-123")
    assert api.get_api_base_url() == (
        "https://api.cloudflare.com/client/v4/accounts/account-123/ai/v1"
    )


def test_gemini_factory_has_no_implicit_local_proxy(monkeypatch):
    monkeypatch.delenv("GEMINI_PROXY", raising=False)
    api = create_gemini_client(token="test")
    assert api.proxies == {}

    monkeypatch.setenv("GEMINI_PROXY", "http://127.0.0.1:10809")
    proxied = create_gemini_client(token="test")
    assert proxied.proxies == {
        "http": "http://127.0.0.1:10809",
        "https": "http://127.0.0.1:10809",
    }
