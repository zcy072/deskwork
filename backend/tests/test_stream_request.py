import pytest
from pydantic import ValidationError

from app.schemas import StreamEventsRequest


def _req(**kw):
    return StreamEventsRequest(session_id="s1", prompt_id="p1", **kw)


def test_block_ms_default_and_upper_bound():
    assert _req().block_ms == 100
    assert _req(block_ms=4000).block_ms == 4000


@pytest.mark.parametrize("value", [-1, 4001, 20000])
def test_block_ms_out_of_range_rejected(value):
    with pytest.raises(ValidationError):
        _req(block_ms=value)
