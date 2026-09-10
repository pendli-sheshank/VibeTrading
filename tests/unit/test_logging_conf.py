from __future__ import annotations

import json
import logging
import sys

from vibetrading.logging_conf import (
    ContextFilter,
    JsonFormatter,
    bind_request_id,
    bind_tenant_id,
    request_id_var,
    tenant_id_var,
)


def _make_record(message: str = "hello") -> logging.LogRecord:
    return logging.LogRecord(
        name="test.logger", level=logging.INFO, pathname=__file__, lineno=1, msg=message, args=(), exc_info=None
    )


def test_context_filter_stamps_none_when_nothing_is_bound():
    record = _make_record()
    assert ContextFilter().filter(record) is True
    assert record.tenant_id is None
    assert record.request_id is None


def test_bind_tenant_id_stamps_records_created_inside_it():
    with bind_tenant_id(42):
        record = _make_record()
        ContextFilter().filter(record)
        assert record.tenant_id == 42
    assert tenant_id_var.get() is None  # restored after exit


def test_bind_request_id_stamps_records_created_inside_it():
    with bind_request_id("req-123"):
        record = _make_record()
        ContextFilter().filter(record)
        assert record.request_id == "req-123"
    assert request_id_var.get() is None


def test_bind_tenant_id_restores_the_previous_value_on_nested_exit():
    with bind_tenant_id(1):
        with bind_tenant_id(2):
            assert tenant_id_var.get() == 2
        assert tenant_id_var.get() == 1
    assert tenant_id_var.get() is None


def test_json_formatter_produces_valid_json_with_expected_fields():
    with bind_tenant_id(7), bind_request_id("req-abc"):
        record = _make_record("something happened")
        ContextFilter().filter(record)
        line = JsonFormatter().format(record)

    payload = json.loads(line)
    assert payload["message"] == "something happened"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger"
    assert payload["tenant_id"] == 7
    assert payload["request_id"] == "req-abc"
    assert "timestamp" in payload
    assert payload["mode"] in {"PAPER", "LIVE"}


def test_json_formatter_includes_exc_info_when_present():
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            name="test.logger",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )
    ContextFilter().filter(record)
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exc_info"]
