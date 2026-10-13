import sqlite3

import pytest

from AIClientCenter.core.manager import AIClientManager, ClientStatus
from AIClientCenter.core.rate_limit import EstimatedRateLimiter, estimate_message_tokens
from AIClientCenter.providers.openai_clients import RateLimitedOpenAIClient


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeAPI:
    def __init__(self):
        self.calls = 0

    def get_api_token(self):
        return "test"

    def get_using_model(self):
        return "gemini-test"

    def get_api_base_url(self):
        return "test://"

    def get_model_list(self):
        return {"data": []}

    def create_chat_completion_sync(self, **_kwargs):
        self.calls += 1
        return {
            "success": True,
            "data": {
                "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8},
            },
            "error": None,
        }


def test_message_estimate_handles_cjk_and_grows_with_content():
    short = estimate_message_tokens([{"role": "user", "content": "你好 world"}])
    long = estimate_message_tokens([{"role": "user", "content": "你好 world" * 10}])

    assert short >= 7
    assert long > short


def test_limiter_applies_80_percent_rpm_and_rolling_expiry():
    clock = FakeClock()
    limiter = EstimatedRateLimiter(rpm=5, utilization_ratio=0.8, clock=clock)

    assert all(limiter.try_reserve(estimated_tokens=1) for _ in range(4))
    assert limiter.try_reserve(estimated_tokens=1) is None
    assert limiter.snapshot()["effective_limits"]["rpm"] == 4

    clock.now += 60
    assert limiter.try_reserve(estimated_tokens=1) is not None


def test_limiter_reconciles_estimate_with_actual_prompt_tokens():
    limiter = EstimatedRateLimiter(tpm=100, utilization_ratio=0.8)
    reservation = limiter.try_reserve(estimated_tokens=20)

    limiter.reconcile(reservation, 55)

    assert limiter.snapshot()["used"]["tpm"] == 55
    assert limiter.try_reserve(estimated_tokens=26) is None


def test_limiter_applies_daily_token_limit():
    limiter = EstimatedRateLimiter(tpd=100, utilization_ratio=0.8)

    assert limiter.try_reserve(estimated_tokens=50) is not None
    assert limiter.try_reserve(estimated_tokens=31) is None
    assert limiter.snapshot()["effective_limits"]["tpd"] == 80


def test_shared_limiter_blocks_second_client_without_hurting_health():
    limiter = EstimatedRateLimiter(rpm=1, utilization_ratio=1)
    first_api = FakeAPI()
    second_api = FakeAPI()
    first = RateLimitedOpenAIClient(
        name="first", openai_api=first_api, rate_limiter=limiter, default_available=True,
    )
    second = RateLimitedOpenAIClient(
        name="second", openai_api=second_api, rate_limiter=limiter, default_available=True,
    )

    assert "error" not in first.chat([{"role": "user", "content": "hello"}])
    blocked = second.chat([{"role": "user", "content": "hello"}])

    assert blocked["error"] == "local_rate_limit"
    assert second_api.calls == 0
    assert second.get_status("status") == ClientStatus.AVAILABLE
    assert second.get_status("error_count") == 0


def test_manager_stats_expose_estimated_rate_limit_snapshot():
    limiter = EstimatedRateLimiter(rpm=100, tpm=1000, rpd=1000)
    client = RateLimitedOpenAIClient(
        name="visible", openai_api=FakeAPI(), rate_limiter=limiter,
        default_available=True,
    )
    manager = AIClientManager(first_check_delay_sec=9999)
    manager.register_client(client)

    details = manager.get_client_stats()["clients"][0]

    assert details["rate_limit"]["estimated"] is True
    assert details["rate_limit"]["effective_limits"] == {
        "rpm": 80, "tpm": 800, "rpd": 800, "tpd": None,
    }
    manager.stop_monitoring()


def test_manager_skips_rate_limited_client_that_cannot_fit_workload():
    too_small = RateLimitedOpenAIClient(
        name="too-small",
        openai_api=FakeAPI(),
        rate_limiter=EstimatedRateLimiter(tpm=100, utilization_ratio=1),
        completion_reserve_tokens=20,
        priority=1,
        default_available=True,
    )
    fits = RateLimitedOpenAIClient(
        name="fits",
        openai_api=FakeAPI(),
        rate_limiter=EstimatedRateLimiter(tpm=1000, utilization_ratio=1),
        completion_reserve_tokens=20,
        priority=2,
        default_available=True,
    )
    manager = AIClientManager(first_check_delay_sec=9999)
    manager.register_client(too_small)
    manager.register_client(fits)

    selected = manager.get_available_client(
        "large-request", estimated_input_tokens=90, max_tokens=50,
    )

    assert selected is fits
    assert too_small.get_status("acquire_count") == 0
    manager.release_client("large-request")
    manager.stop_monitoring()


def test_sqlite_ledger_survives_limiter_restart(tmp_path):
    clock = FakeClock()
    database = tmp_path / "rate_limits.sqlite3"
    first = EstimatedRateLimiter(
        rpd=2,
        utilization_ratio=1,
        clock=clock,
        storage_path=database,
        storage_namespace="gemini:test-model",
    )

    assert first.try_reserve(estimated_tokens=10) is not None
    assert first.try_reserve(estimated_tokens=10) is not None

    restarted = EstimatedRateLimiter(
        rpd=2,
        utilization_ratio=1,
        clock=clock,
        storage_path=database,
        storage_namespace="gemini:test-model",
    )

    assert restarted.try_reserve(estimated_tokens=10) is None
    assert restarted.snapshot()["used"]["rpd"] == 2
    assert restarted.snapshot()["persistent"] is True


def test_sqlite_ledger_reconciles_tokens_and_removes_expired_rows(tmp_path):
    clock = FakeClock()
    database = tmp_path / "rate_limits.sqlite3"
    limiter = EstimatedRateLimiter(
        tpm=100,
        rpd=2,
        utilization_ratio=1,
        clock=clock,
        storage_path=database,
        storage_namespace="gemini:test-model",
    )
    reservation = limiter.try_reserve(estimated_tokens=20)

    limiter.reconcile(reservation, 55)

    assert limiter.snapshot()["used"] == {
        "rpm": 1, "tpm": 55, "rpd": 1, "tpd": 55,
    }
    assert limiter.try_reserve(estimated_tokens=46) is None

    clock.now += 86400
    assert limiter.try_reserve(estimated_tokens=20) is not None
    with sqlite3.connect(database) as connection:
        count = connection.execute("SELECT COUNT(*) FROM rate_limit_usage").fetchone()[0]
    assert count == 1


def test_sqlite_ledger_requires_namespace(tmp_path):
    with pytest.raises(ValueError, match="storage_namespace"):
        EstimatedRateLimiter(rpd=10, storage_path=tmp_path / "rate_limits.sqlite3")


def test_rate_limited_client_reserves_completion_and_reconciles_total_tokens():
    limiter = EstimatedRateLimiter(tpm=100, tpd=100, utilization_ratio=1)
    client = RateLimitedOpenAIClient(
        name="total-token-provider",
        openai_api=FakeAPI(),
        rate_limiter=limiter,
        token_usage_field="total_tokens",
        completion_reserve_tokens=20,
        default_available=True,
    )

    assert "error" not in client.chat(
        [{"role": "user", "content": "hello"}], max_tokens=50)
    snapshot = limiter.snapshot()
    assert snapshot["used"]["tpm"] == 8
    assert snapshot["used"]["tpd"] == 8
