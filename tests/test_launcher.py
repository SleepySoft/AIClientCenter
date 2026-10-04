from pathlib import Path

from AIClientCenter.cli.launcher import build_manager, load_config, main, validate_config
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
