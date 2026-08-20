from datetime import timedelta

from src.gateway.application.services.login_throttle_service import LoginThrottlePolicy, ThrottleRule


def test_progressive_blocking_is_bounded_and_global_rule_is_independent() -> None:
    policy = LoginThrottlePolicy(
        account=ThrottleRule(limit=5, window=timedelta(minutes=15), base_block=timedelta(seconds=2), max_block=timedelta(minutes=15)),
        ip=ThrottleRule(limit=20, window=timedelta(minutes=15), base_block=timedelta(seconds=2), max_block=timedelta(minutes=15)),
        global_=ThrottleRule(limit=200, window=timedelta(minutes=1), base_block=timedelta(seconds=1), max_block=timedelta(minutes=1)),
    )

    assert policy.account.block_seconds(4) == 0
    assert policy.account.block_seconds(5) == 2
    assert policy.account.block_seconds(6) == 4
    assert policy.account.block_seconds(100) == 900
    assert policy.global_.window_seconds == 60
