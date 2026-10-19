import io
import json
import logging

from app.monitoring.logging_setup import REDACTED, configure_logging, redact


def _capture(**extra):
    buf = io.StringIO()
    configure_logging("INFO", True, stream=buf)
    logging.getLogger("t").info("hello", extra=extra)
    return json.loads(buf.getvalue().strip().splitlines()[-1])


def test_json_output_has_core_fields():
    rec = _capture(symbol="BTCUSD")
    assert rec["msg"] == "hello" and rec["level"] == "INFO" and rec["symbol"] == "BTCUSD"
    assert rec["ts"].endswith("+00:00")


def test_sensitive_extras_are_redacted():
    rec = _capture(api_key="abc123", payload={"signature": "zzz", "price": 1, "nested": [{"password": "p"}]})
    assert rec["api_key"] == REDACTED
    assert rec["payload"] == {"signature": REDACTED, "price": 1, "nested": [{"password": REDACTED}]}
    assert "abc123" not in json.dumps(rec)


def test_configure_logging_is_idempotent():
    buf = io.StringIO()
    configure_logging("INFO", True, stream=buf)
    configure_logging("INFO", True, stream=buf)
    logging.getLogger("t").info("once")
    assert len(buf.getvalue().strip().splitlines()) == 1


def test_redact_leaves_plain_values():
    assert redact({"a": 1}) == {"a": 1}
