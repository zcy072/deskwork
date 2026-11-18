from types import SimpleNamespace

from app.services.history import TurnRecorder
from app.services.usage import UsageTracker


def _msg(mid, i, o, cache=0, model=None):
    return SimpleNamespace(
        id=mid,
        response_metadata={"model_name": model} if model else {},
        usage_metadata={
            "input_tokens": i,
            "output_tokens": o,
            "input_token_details": {"cache_read": cache},
        },
    )


def test_accumulates_and_dedups_by_message_id():
    t = UsageTracker()
    t.add(_msg("a", 100, 10, 40))
    t.add(_msg("a", 100, 10, 40))
    t.add(_msg("b", 200, 20))
    assert t.to_event() == {
        "llm_calls": 2,
        "input_tokens": 300,
        "output_tokens": 30,
        "total_tokens": 330,
        "cache_read_tokens": 40,
        "models": [],
        "by_model": {"unknown": {"input_tokens": 300, "output_tokens": 30}},
        "missing_usage_calls": 0,
    }


def test_records_models_actually_used_in_order():
    t = UsageTracker()
    t.add(_msg("a", 1, 1, model="primary"))
    t.add(_msg("b", 1, 1, model="deepseek-flash"))
    t.add(_msg("c", 1, 1, model="deepseek-flash"))
    assert t.to_event()["models"] == ["primary", "deepseek-flash"]


def test_by_model_splits_tokens_per_model():
    t = UsageTracker()
    t.add(_msg("a", 100, 10, model="primary"))
    t.add(_msg("b", 30, 5, model="deepseek-flash"))
    t.add(_msg("c", 20, 5, model="primary"))
    assert t.to_event()["by_model"] == {
        "primary": {"input_tokens": 120, "output_tokens": 15},
        "deepseek-flash": {"input_tokens": 30, "output_tokens": 5},
    }


def test_missing_usage_still_counts_model_calls():
    t = UsageTracker()
    t.add(SimpleNamespace(id="x", usage_metadata=None))
    t.add(SimpleNamespace(id="x", usage_metadata=None))
    assert t.to_event()["llm_calls"] == 1
    assert t.to_event()["missing_usage_calls"] == 1


def test_usage_arriving_after_response_is_counted_once():
    t = UsageTracker()
    t.add(SimpleNamespace(id="x", usage_metadata=None))
    t.add(_msg("x", 4, 2))
    t.add(_msg("x", 4, 2))
    assert t.to_event()["llm_calls"] == 1
    assert t.to_event()["total_tokens"] == 6
    assert t.to_event()["missing_usage_calls"] == 0


def test_recorder_ignores_usage_event():
    r = TurnRecorder()
    r.record("usage", {"total_tokens": 5})
    assert r.parts == []


# ---------- 会话级汇总与持久化 ----------


def test_aggregate_usage_sums_turns_and_models():
    from app.services.usage import aggregate_usage

    rows = [
        {
            "llm_calls": 2,
            "input_tokens": 100,
            "output_tokens": 10,
            "cache_read_tokens": 50,
            "by_model": {"a": {"input_tokens": 100, "output_tokens": 10}},
        },
        {
            "llm_calls": 1,
            "input_tokens": 40,
            "output_tokens": 5,
            "cache_read_tokens": 0,
            "by_model": {
                "a": {"input_tokens": 30, "output_tokens": 5},
                "b": {"input_tokens": 10, "output_tokens": 0},
            },
        },
    ]
    total = aggregate_usage(rows)
    assert total["turns"] == 2 and total["llm_calls"] == 3
    assert total["total_tokens"] == 155
    assert total["by_model"]["a"] == {"input_tokens": 130, "output_tokens": 15}
