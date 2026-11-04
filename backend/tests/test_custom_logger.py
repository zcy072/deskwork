import logging

from app.utils.custom_logger import TraceIdFilter, session_id_var, trace_id_var


def test_log_context_is_attached_and_reset():
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "message", (), None)
    trace_token = trace_id_var.set("G-test")
    session_token = session_id_var.set("session-1")
    try:
        assert TraceIdFilter().filter(record)
        assert record.trace_id == "G-test"
        assert record.session_id == "session-1"
    finally:
        trace_id_var.reset(trace_token)
        session_id_var.reset(session_token)

    next_record = logging.LogRecord("test", logging.INFO, __file__, 1, "message", (), None)
    TraceIdFilter().filter(next_record)
    assert next_record.trace_id == "-"
    assert next_record.session_id == "-"
