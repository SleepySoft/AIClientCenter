from pathlib import Path

from AIClientCenter.cli import launcher
from AIClientCenter.cli.launcher import (
    DEFAULT_CONFIG, EXAMPLE_CONFIG, PACKAGE_ROOT, PROJECT_ROOT, build_manager,
    load_config, main, resolve_config_path, validate_config,
)
from AIClientCenter.core.manager import BaseAIClient, ClientStatus
from AIClientCenter.core.budget import BudgetMode, BudgetPolicy


class LauncherClient(BaseAIClient):
    def __init__(self, name="launcher-client", **kwargs):
        super().__init__(name=name, api_token="test", **kwargs)
        self._update_client_status(ClientStatus.AVAILABLE)

    def get_model_list(self):
        return {}

    def get_current_model(self):
        return "launcher-model"

    def get_api_base_url(self):
        return "test://"

    def _chat_completion_sync(self, *args, **kwargs):
        raise AssertionError("launcher validation must not invoke a model")


def test_default_config_is_loaded_from_module_config_directory():
    assert PACKAGE_ROOT == Path(__file__).resolve().parents[1]
    assert PROJECT_ROOT == PACKAGE_ROOT.parent
    assert DEFAULT_CONFIG == PACKAGE_ROOT / "config" / "config.py"
    assert EXAMPLE_CONFIG == PACKAGE_ROOT / "config" / "example.py"


def test_missing_default_config_falls_back_to_example_and_reports_copy_command(tmp_path, monkeypatch):
    config_path = tmp_path / "config.py"
    example_path = tmp_path / "example.py"
    example_path.write_text("AI_CLIENTS = {}\nAI_CLIENT_LIMIT = {}\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "DEFAULT_CONFIG", config_path)
    monkeypatch.setattr(launcher, "EXAMPLE_CONFIG", example_path)

    selected, notice = resolve_config_path(None)

    assert selected == example_path
    assert "Copy-Item" in notice
    assert str(config_path) in notice


def test_explicit_config_does_not_fall_back_to_example(tmp_path, monkeypatch):
    example_path = tmp_path / "example.py"
    monkeypatch.setattr(launcher, "EXAMPLE_CONFIG", example_path)
    explicit_path = tmp_path / "other.py"

    selected, notice = resolve_config_path(explicit_path)

    assert selected == explicit_path
    assert notice is None


def test_main_uses_example_when_local_config_is_missing(tmp_path, monkeypatch, capsys):
    example_path = tmp_path / "example.py"
    example_path.write_text(
        "from AIClientCenter.tests.test_launcher import LauncherClient\n"
        "AI_CLIENTS = {'one': LauncherClient()}\n"
        "AI_CLIENT_LIMIT = {'default': 1}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(launcher, "DEFAULT_CONFIG", tmp_path / "config.py")
    monkeypatch.setattr(launcher, "EXAMPLE_CONFIG", example_path)

    assert main(["--validate-only"]) == 0
    assert "正在使用样例" in capsys.readouterr().err


def test_load_config_executes_python_config_and_reads_public_contract(tmp_path: Path):
    config_path = tmp_path / "ai_client_config.py"
    config_path.write_text(
        "from AIClientCenter.tests.test_launcher import LauncherClient\n"
        "AI_CLIENTS = {'one': LauncherClient()}\n"
        "AI_CLIENT_LIMIT = {'default': 1}\n",
        encoding="utf-8",
    )

    clients, limits = load_config(config_path)

    assert list(clients) == ["one"]
    assert limits == {"default": 1}


def test_validate_config_reports_hard_budget_and_bad_group_limit_without_network():
    client = LauncherClient(
        budget_policy=BudgetPolicy(BudgetMode.HARD_LIMIT, {"request_count": 0}),
    )

    errors, warnings = validate_config({"one": client}, {"default": 0})

    assert any("正整数" in error for error in errors)
    assert any("预算阻断" in warning for warning in warnings)


def test_build_manager_registers_clients_and_group_limits():
    client = LauncherClient()

    manager = build_manager({"one": client}, {"default": 1})

    assert manager.get_client_by_name("launcher-client") is client
    assert manager.group_limits == {"default": 1}
    manager.stop_monitoring()


def test_main_validate_only_returns_success_without_starting_http_server(tmp_path: Path):
    config_path = tmp_path / "ai_client_config.py"
    config_path.write_text(
        "from AIClientCenter.tests.test_launcher import LauncherClient\n"
        "AI_CLIENTS = {'one': LauncherClient()}\n"
        "AI_CLIENT_LIMIT = {'default': 1}\n",
        encoding="utf-8",
    )

    assert main(["--config", str(config_path), "--validate-only"]) == 0
