import json
import logging

from slipstream.logging_setup import JsonFormatter


def test_formats_record_as_json_with_fields() -> None:
    record = logging.LogRecord("slipstream", logging.INFO, __file__, 1, "fill", None, None)
    record.fields = {"event": "fill", "price": 101.0}  # type: ignore[attr-defined]
    payload = json.loads(JsonFormatter().format(record))
    assert payload["msg"] == "fill"
    assert payload["level"] == "INFO"
    assert payload["event"] == "fill"
    assert payload["price"] == 101.0
    assert "ts" in payload
