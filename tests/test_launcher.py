from pathlib import Path

from AIClientCenter.cli import launcher
from AIClientCenter.cli.launcher import (
    DEFAULT_CONFIG, EXAMPLE_CONFIG, PACKAGE_ROOT, PROJECT_ROOT, main,
    resolve_config_path,
)
from AIClientCenter.configuration import (
    ClientFactoryRegistry, build_manager, load_config, validate_config,
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
    clients, limits = load_config(
        path, registry=ClientFactoryRegistry({"launcher": _factory}))
    assert list(clients) == ["one"]
    assert clients["one"].priority == 7
    assert clients["one"].group_id == "default"
    assert limits == {"default": 1}


def test_custom_python_config_uses_explicit_builder(tmp_path):
    path = tmp_path / "custom.py"
    path.write_text(
        "from AIClientCenter.core.preset import ClientPreset\n"
        "from AIClientCenter.tests.test_launcher import LauncherClient\n"
        "def build_ai_client_config():\n"
        "    client = LauncherClient(group_id='custom')\n"
        "    return ClientPreset({'one': client}, {'custom': 1})\n",
        encoding="utf-8",
    )
    clients, limits = load_config(path)
    assert list(clients) == ["one"]
    assert limits == {"custom": 1}


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


def test_main_validate_only_accepts_empty_yaml(tmp_path):
    path = tmp_path / "clients.yaml"
    path.write_text("clients: []\ngroup_limits: {}\n", encoding="utf-8")
    assert main(["--config", str(path), "--validate-only"]) == 0
