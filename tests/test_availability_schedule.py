from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from AIClientCenter.core.availability import ClientAvailabilitySchedule
from AIClientCenter.core.manager import AIClientManager, BaseAIClient, ClientStatus
from AIClientCenter.factory_engine import load_config, validate_config


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _schedule(mode="available", period="22:00-07:00", *, timezone=True):
    config = {mode: period}
    if timezone:
        config["timezone"] = "Asia/Shanghai"
    return ClientAvailabilitySchedule.from_config(config)


def test_available_window_supports_cross_midnight_and_next_change():
    schedule = _schedule()

    before = schedule.evaluate(datetime(2026, 10, 19, 21, 0, tzinfo=SHANGHAI))
    during_evening = schedule.evaluate(datetime(2026, 10, 19, 23, 0, tzinfo=SHANGHAI))
    during_next_day = schedule.evaluate(datetime(2026, 10, 20, 6, 59, tzinfo=SHANGHAI))
    after = schedule.evaluate(datetime(2026, 10, 20, 7, 0, tzinfo=SHANGHAI))

    assert before.allowed is False
    assert datetime.fromtimestamp(before.next_change_at, SHANGHAI).hour == 22
    assert during_evening.allowed is True
    assert during_next_day.allowed is True
    assert after.allowed is False


def test_unavailable_window_only_blocks_matching_period():
    schedule = _schedule("unavailable", "08:00-18:00")

    assert schedule.evaluate(datetime(2026, 10, 19, 12, 0, tzinfo=SHANGHAI)).allowed is False
    assert schedule.evaluate(datetime(2026, 10, 19, 18, 0, tzinfo=SHANGHAI)).allowed is True


def test_multiple_daily_periods_are_supported():
    schedule = _schedule(period=["00:00-02:00", "22:00-23:59"])

    assert schedule.evaluate(datetime(2026, 10, 19, 1, 0, tzinfo=SHANGHAI)).allowed is True
    assert schedule.evaluate(datetime(2026, 10, 19, 12, 0, tzinfo=SHANGHAI)).allowed is False
    assert schedule.evaluate(datetime(2026, 10, 19, 22, 30, tzinfo=SHANGHAI)).allowed is True


@pytest.mark.parametrize("config", [
    {"timezone": "Invalid/Zone", "available": "22:00-07:00"},
    {"timezone": "Asia/Shanghai"},
    {"available": "22:00-07:00", "unavailable": "07:00-22:00"},
    {"available": "8:00-18:00"},
    {"available": "08:00-08:00"},
    {"available": []},
])
def test_invalid_schedule_config_is_rejected(config):
    with pytest.raises(ValueError):
        ClientAvailabilitySchedule.from_config(config)


class ScheduleClient(BaseAIClient):
    def __init__(self, name="scheduled", **kwargs):
        super().__init__(name=name, api_token="test", **kwargs)
        self._update_client_status(ClientStatus.AVAILABLE)

    def get_model_list(self): return {}
    def get_current_model(self): return "test-model"
    def get_api_base_url(self): return "test://"
    def _chat_completion_sync(self, *args, **kwargs):
        raise AssertionError("schedule tests do not call the model")


def _strict_factory(*, name, group):
    return ScheduleClient(name=name, group_id=group)


def test_factory_attaches_simple_schedule_without_passing_it_to_provider(tmp_path):
    path = tmp_path / "clients.yaml"
    path.write_text(
        "clients:\n"
        "  - factory: strict\n"
        "    name: night\n"
        "    group: codex\n"
        "    availability:\n"
        "      available: '22:00-07:00'\n"
        "group_limits:\n"
        "  codex: 1\n",
        encoding="utf-8",
    )

    clients, limits = load_config(
        path, factories={"strict": _strict_factory}, include_builtins=False)
    errors, warnings = validate_config(clients, limits)

    schedule = clients["night"].availability_schedule
    assert schedule.mode == "available"
    assert schedule.timezone_explicit is False
    assert schedule.timezone_name
    assert errors == []
    assert any(schedule.timezone_name in warning for warning in warnings)
    assert any("系统本地时区" in warning for warning in warnings)


def test_explicit_timezone_does_not_emit_local_timezone_warning(tmp_path):
    path = tmp_path / "clients.yaml"
    path.write_text(
        "clients:\n"
        "  - factory: strict\n"
        "    name: night\n"
        "    group: codex\n"
        "    availability: {timezone: Asia/Shanghai, available: '22:00-07:00'}\n"
        "group_limits: {codex: 1}\n",
        encoding="utf-8",
    )
    clients, limits = load_config(
        path, factories={"strict": _strict_factory}, include_builtins=False)
    _, warnings = validate_config(clients, limits)
    assert not any("系统本地时区" in warning for warning in warnings)


def test_manager_excludes_schedule_paused_client_without_changing_health():
    client = ScheduleClient(group_id="codex")
    client.availability_schedule = SimpleNamespace(
        evaluate=lambda now=None: SimpleNamespace(allowed=False),
        seconds_until_change=lambda now=None: 60.0,
        describe=lambda now=None: {
            "enabled": True,
            "mode": "available",
            "timezone": "Asia/Shanghai",
            "allowed": False,
            "reason": "outside_available_schedule",
            "next_change_at": 1_800_000_000.0,
        },
    )
    manager = AIClientManager(first_check_delay_sec=9999)
    manager.register_client(client)
    manager.set_group_limit("codex", 1)

    assert manager.get_available_client("iis") is None
    assert manager.get_scheduling_capacity() == 0
    assert manager._next_recovery_delay() == pytest.approx(60.0)
    stats = manager.get_client_stats()
    assert stats["summary"]["available"] == 0
    assert stats["clients"][0]["availability"]["reason"] == "outside_available_schedule"
    assert client.get_status("status") == ClientStatus.AVAILABLE
