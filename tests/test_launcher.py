from pathlib import Path

from AIClientCenter.cli import launcher
from AIClientCenter.cli.launcher import (
    DEFAULT_CONFIG, EXAMPLE_CONFIG, PACKAGE_ROOT, PROJECT_ROOT, main,
    resolve_config_path,
)
from AIClientCenter.factory_engine import (
    AIClientConfigReloader, ClientFactoryEngine, build_manager, load_config,
    validate_config,
)
from AIClientCenter.core.budget import BudgetMode, BudgetPolicy
from AIClientCenter.core.manager import BaseAIClient, ClientStatus


class LauncherClient(BaseAIClient):
    def __init__(self, name="launcher-client", **kwargs):
        super().__init__(name=name, api_token="test", **kwargs)
        self._update_client_status(ClientStatus.AVAILABLE)

    def get_model_list(self): return {}
    def get_current_model(self): return "launcher-model"
    def get_api_base_url(self): return "test://"
    def _chat_completion_sync(self, *args, **kwargs):
        raise AssertionError("validation must not invoke a model")


def _factory(*, name, group, **kwargs):
    return LauncherClient(name=name, group_id=group, **kwargs)


def test_default_config_is_loaded_from_module_config_directory():
    assert PACKAGE_ROOT == Path(__file__).resolve().parents[1]
    assert PROJECT_ROOT == PACKAGE_ROOT.parent
    assert DEFAULT_CONFIG == PACKAGE_ROOT / "config" / "config.yaml"
    assert EXAMPLE_CONFIG == PACKAGE_ROOT / "config" / "example.yaml"


def test_missing_default_config_falls_back_to_yaml_example(tmp_path, monkeypatch):
    example = tmp_path / "example.yaml"
    example.write_text("clients: []\ngroup_limits: {}\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "DEFAULT_CONFIG", tmp_path / "config.yaml")
    monkeypatch.setattr(launcher, "EXAMPLE_CONFIG", example)
    selected, notice = resolve_config_path(None)
    assert selected == example
    assert "Copy-Item" in notice


def test_yaml_passes_non_reserved_fields_directly_to_factory(tmp_path):
    path = tmp_path / "clients.yaml"
    path.write_text(
        "clients:\n"
        "  - factory: launcher\n"
        "    name: one\n"
        "    group: default\n"
        "    priority: 7\n"
        "group_limits:\n"
        "  default: 1\n",
        encoding="utf-8",
    )
    clients, limits = load_config(path, factories={"launcher": _factory})
    assert list(clients) == ["one"]
    assert clients["one"].priority == 7
    assert clients["one"].group_id == "default"
    assert limits == {"default": 1}


def test_disabled_yaml_entry_is_kept_but_not_built(tmp_path):
    path = tmp_path / "clients.yaml"
    path.write_text(
        "clients:\n"
        "  - factory: unavailable_factory\n"
        "    name: disabled\n"
        "    group: disabled_group\n"
        "    enable: false\n"
        "  - factory: launcher\n"
        "    name: enabled\n"
        "    group: default\n"
        "group_limits:\n"
        "  default: 1\n",
        encoding="utf-8",
    )

    clients, limits = load_config(path, factories={"launcher": _factory})

    assert list(clients) == ["enabled"]
    assert limits == {"default": 1}


def test_enable_must_be_boolean(tmp_path):
    path = tmp_path / "clients.yaml"
    path.write_text(
        "clients:\n"
        "  - factory: launcher\n"
        "    name: invalid\n"
        "    group: default\n"
        "    enable: 'false'\n"
        "group_limits:\n"
        "  default: 1\n",
        encoding="utf-8",
    )

    try:
        load_config(path, factories={"launcher": _factory})
    except ValueError as exc:
        assert "clients[0].enable 必须是布尔值" in str(exc)
    else:
        raise AssertionError("string enable value must be rejected")


def test_custom_factory_can_use_import_path(tmp_path):
    path = tmp_path / "custom.yaml"
    path.write_text(
        "clients:\n"
        "  - factory: AIClientCenter.tests.test_launcher:_factory\n"
        "    name: imported\n"
        "    group: custom\n"
        "group_limits:\n"
        "  custom: 1\n",
        encoding="utf-8",
    )
    clients, limits = load_config(path)
    assert list(clients) == ["imported"]
    assert limits == {"custom": 1}


def test_host_can_register_factory_without_editing_engine(tmp_path):
    path = tmp_path / "custom.yaml"
    path.write_text(
        "clients:\n"
        "  - factory: local\n"
        "    name: registered\n"
        "    group: custom\n"
        "group_limits:\n"
        "  custom: 1\n",
        encoding="utf-8",
    )
    engine = ClientFactoryEngine(include_builtins=False)
    engine.register_factory("local", _factory)
    clients, limits = engine.load(path)
    assert list(clients) == ["registered"]
    assert limits == {"custom": 1}


def test_python_file_factory_is_relative_to_yaml_not_working_directory(
        tmp_path, monkeypatch):
    config_dir = tmp_path / "deployment"
    config_dir.mkdir()
    (config_dir / "local_settings.py").write_text(
        "PRIORITY = 7\n", encoding="utf-8")
    factory_file = config_dir / "custom_clients.py"
    factory_file.write_text(
        "from AIClientCenter.tests.test_launcher import LauncherClient\n"
        "from .local_settings import PRIORITY\n"
        "def create_client(*, name, group, **kwargs):\n"
        "    return LauncherClient(name=name, group_id=group, "
        "priority=PRIORITY, **kwargs)\n",
        encoding="utf-8",
    )
    config_file = config_dir / "clients.yaml"
    config_file.write_text(
        "clients:\n"
        "  - factory: ./custom_clients.py:create_client\n"
        "    name: relative\n"
        "    group: custom\n"
        "group_limits:\n"
        "  custom: 1\n",
        encoding="utf-8",
    )
    unrelated_dir = tmp_path / "elsewhere"
    unrelated_dir.mkdir()
    monkeypatch.chdir(unrelated_dir)

    clients, limits = load_config(config_file)

    assert list(clients) == ["relative"]
    assert clients["relative"].priority == 7
    assert limits == {"custom": 1}


def test_self_rotating_openai_preset_keeps_model_and_token_rotation(
        tmp_path, monkeypatch):
    monkeypatch.setenv("ROTATION_TOKEN_A", "token-a")
    monkeypatch.setenv("ROTATION_TOKEN_B", "token-b")
    path = tmp_path / "rotating.yaml"
    path.write_text(
        "clients:\n"
        "  - factory: self_rotating_openai\n"
        "    name: rotating\n"
        "    group: rotating\n"
        "    api_base_url: https://example.invalid/v1\n"
        "    models: [model-a, model-b]\n"
        "    token_envs: [ROTATION_TOKEN_A, ROTATION_TOKEN_B]\n"
        "    model_rotate_per_times: 2\n"
        "    token_rotate_per_times: 3\n"
        "group_limits:\n"
        "  rotating: 1\n",
        encoding="utf-8",
    )

    clients, limits = load_config(path)
    client = clients["rotating"]

    assert client.model_rotator.get_stats() == {
        "total_items": 2, "current_index": 0, "current_uses": 0,
        "rotate_threshold": 2,
    }
    assert client.token_rotator.get_stats() == {
        "total_items": 2, "current_index": 0, "current_uses": 0,
        "rotate_threshold": 3,
    }
    assert limits == {"rotating": 1}


def test_validate_requires_group_limit_and_reports_hard_budget():
    client = LauncherClient(
        group_id="missing",
        budget_policy=BudgetPolicy(BudgetMode.HARD_LIMIT, {"request_count": 0}),
    )
    errors, warnings = validate_config({"one": client}, {})
    assert any("missing" in error for error in errors)
    assert any("预算" in warning for warning in warnings)


def test_build_manager_registers_clients_and_group_limits():
    client = LauncherClient(group_id="default")
    manager = build_manager({"one": client}, {"default": 1})
    assert manager.get_client_by_name("launcher-client") is client
    assert manager.group_limits == {"default": 1}
    manager.stop_monitoring()


def test_config_reloader_atomically_rebuilds_pool_and_preserves_inflight(tmp_path):
    old_client = LauncherClient(name="old", group_id="default")
    manager = build_manager({"old": old_client}, {"default": 1})
    assert manager.get_available_client("inflight") is old_client

    path = tmp_path / "clients.yaml"
    path.write_text(
        "clients:\n"
        "  - factory: launcher\n"
        "    name: new\n"
        "    group: default\n"
        "group_limits:\n"
        "  default: 1\n",
        encoding="utf-8",
    )
    result = AIClientConfigReloader(
        manager, path, factories={"launcher": _factory},
        include_builtins=False).reload()

    assert result["generation"] == 1
    assert result["old_inflight_count"] == 1
    assert [client.name for client in manager.clients] == ["new"]
    assert manager.get_available_client("inflight") is None
    assert old_client._is_acquired() is True
    # 旧调用仍占用原分组额度；热切换不会让并发限制瞬时失效。
    assert manager.get_available_client("next") is None

    manager.release_client("inflight")
    selected = manager.get_available_client("next")
    assert selected is not None
    assert selected.name == "new"
    manager.release_client(selected)
    manager.stop_monitoring()


def test_config_reloader_keeps_current_pool_when_new_config_is_empty(tmp_path):
    old_client = LauncherClient(name="old", group_id="default")
    manager = build_manager({"old": old_client}, {"default": 1})
    path = tmp_path / "clients.yaml"
    path.write_text("clients: []\ngroup_limits: {}\n", encoding="utf-8")

    reloader = AIClientConfigReloader(
        manager, path, include_builtins=False)
    try:
        reloader.reload()
    except ValueError as exc:
        assert "没有生成任何 Client" in str(exc)
    else:
        raise AssertionError("empty reload must be rejected")

    assert manager.clients == [old_client]
    assert manager.group_limits == {"default": 1}
    assert manager._pool_generation == 0
    manager.stop_monitoring()


def test_main_validate_only_accepts_empty_yaml(tmp_path):
    path = tmp_path / "clients.yaml"
    path.write_text("clients: []\ngroup_limits: {}\n", encoding="utf-8")
    assert main(["--config", str(path), "--validate-only"]) == 0
