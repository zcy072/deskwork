from types import SimpleNamespace

import pytest

from app.services.budget import BudgetExceeded, BudgetGuard
from app.services.usage import UsageTracker


def _usage(calls: int, tokens_each: int = 100) -> UsageTracker:
    t = UsageTracker()
    for i in range(calls):
        t.add(
            SimpleNamespace(
                id=f"m{i}",
                usage_metadata={"input_tokens": tokens_each, "output_tokens": 0},
            )
        )
    return t


def _call(name="bash_tool", **args):
    return {"name": name, "args": args}


def test_within_budget_passes():
    BudgetGuard(10, 10_000, 3).check(_usage(5), [_call(command="ls")])


def test_llm_call_limit():
    guard = BudgetGuard(3, 0, 0)
    guard.check(_usage(3))
    with pytest.raises(BudgetExceeded, match="模型调用上限"):
        guard.check(_usage(4))


def test_token_limit():
    with pytest.raises(BudgetExceeded, match="token 上限"):
        BudgetGuard(0, 500, 0).check(_usage(6, 100))


def test_repeated_identical_calls_trip_the_guard():
    guard = BudgetGuard(0, 0, 3)
    guard.check(_usage(1), [_call(command="ls")])
    guard.check(_usage(2), [_call(command="ls")])
    with pytest.raises(BudgetExceeded, match="循环"):
        guard.check(_usage(3), [_call(command="ls")])


def test_different_args_or_interleaving_reset_the_counter():
    guard = BudgetGuard(0, 0, 3)
    for cmd in ["a", "a", "b", "a", "a", "b"]:
        guard.check(_usage(1), [_call(command=cmd)])


def test_zero_disables_each_check():
    guard = BudgetGuard(0, 0, 0)
    for _ in range(50):
        guard.check(_usage(500), [_call(command="ls")])
