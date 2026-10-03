from AIClientCenter.BudgetPolicy import BudgetMode, BudgetPolicy


def test_unknown_and_observed_budget_never_block_unknown_quota():
    usage = {"total_tokens": 999999}

    for mode in (BudgetMode.UNKNOWN, BudgetMode.OBSERVED):
        decision = BudgetPolicy(mode, {"total_tokens": 1}).evaluate(usage)
        assert decision.allowed is True
        assert decision.ranking_multiplier == 1.0


def test_hard_limit_blocks_only_when_observed_usage_reaches_limit():
    policy = BudgetPolicy(BudgetMode.HARD_LIMIT, {"request_count": 3})

    assert policy.evaluate({"request_count": 2}).allowed is True
    decision = policy.evaluate({"request_count": 3})
    assert decision.allowed is False
    assert decision.reason == "budget_limit_reached:request_count=3.0/3.0"


def test_soft_limit_deprioritizes_without_blocking():
    policy = BudgetPolicy(
        BudgetMode.SOFT_LIMIT,
        {"total_tokens": 100},
        soft_limit_multiplier=0.25,
    )

    decision = policy.evaluate({"total_tokens": 100})

    assert decision.allowed is True
    assert decision.ranking_multiplier == 0.25
    assert decision.reason.startswith("budget_limit_reached:")


def test_invalid_or_missing_observations_do_not_turn_into_a_hard_block():
    policy = BudgetPolicy(BudgetMode.HARD_LIMIT, {"total_tokens": "invalid"})

    assert policy.evaluate({"total_tokens": 100}).allowed is True
    assert policy.evaluate({}).allowed is True
