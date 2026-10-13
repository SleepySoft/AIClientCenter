import pytest

from AIClientCenter.core.manager import BaseAIClient
from AIClientCenter.core.preset import ClientPreset, compose_presets


class StubClient(BaseAIClient):
    def get_model_list(self):
        return {}

    def get_current_model(self):
        return "stub"

    def get_api_base_url(self):
        return "https://example.invalid/v1"

    def _chat_completion_sync(self, *args, **kwargs):
        raise NotImplementedError


def _client(name: str, group_id: str) -> StubClient:
    return StubClient(name=name, api_token="test", group_id=group_id)


def test_compose_presets_combines_clients_and_limits():
    first = ClientPreset({"a": _client("a", "group-a")}, {"group-a": 1})
    second = ClientPreset({"b": _client("b", "group-b")}, {"group-b": 2})

    clients, limits = compose_presets(first, second)

    assert set(clients) == {"a", "b"}
    assert limits == {"group-a": 1, "group-b": 2}


def test_compose_presets_rejects_client_name_collision():
    first = ClientPreset({"same": _client("a", "group-a")})
    second = ClientPreset({"same": _client("b", "group-b")})

    with pytest.raises(ValueError, match="名称重复"):
        compose_presets(first, second)


def test_compose_presets_rejects_conflicting_group_limits():
    with pytest.raises(ValueError, match="分组限额冲突"):
        compose_presets(
            ClientPreset(group_limits={"shared": 1}),
            ClientPreset(group_limits={"shared": 2}),
        )
