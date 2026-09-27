import json
from pathlib import Path

import pytest
from test_kraken_rest import ohlc_body

from slipstream.kraken import parse_message
from slipstream.replay import ReplayError, read_calibration, read_replay

FIXTURE = Path(__file__).parent / "fixtures" / "kraken_btcusd_replay.jsonl"


def write_lines(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_reads_records(tmp_path: Path) -> None:
    file = write_lines(
        tmp_path / "r.jsonl", [json.dumps({"recv_ns": 5, "msg": {"channel": "heartbeat"}}), ""]
    )
    assert list(read_replay(file)) == [(5, json.dumps({"channel": "heartbeat"}), "kraken")]


def test_venue_defaults_to_kraken_when_absent(tmp_path: Path) -> None:
    file = write_lines(
        tmp_path / "r.jsonl", [json.dumps({"recv_ns": 5, "msg": {"channel": "heartbeat"}})]
    )
    assert [venue for _, _, venue in read_replay(file)] == ["kraken"]


def test_venue_explicit_coinbase_is_kept(tmp_path: Path) -> None:
    file = write_lines(
        tmp_path / "r.jsonl",
        [json.dumps({"recv_ns": 5, "venue": "coinbase", "msg": {"channel": "heartbeat"}})],
    )
    assert [venue for _, _, venue in read_replay(file)] == ["coinbase"]


@pytest.mark.parametrize(
    "venue",
    ["binance", 5, True, None, ""],
    ids=["unknown-venue", "int-venue", "bool-venue", "null-venue", "empty-venue"],
)
def test_rejects_bad_venue(tmp_path: Path, venue: object) -> None:
    file = write_lines(
        tmp_path / "bad.jsonl", [json.dumps({"recv_ns": 1, "venue": venue, "msg": {}})]
    )
    with pytest.raises(ReplayError, match="line 1"):
        list(read_replay(file))


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        json.dumps({"msg": {}}),
        json.dumps({"recv_ns": -1, "msg": {}}),
        json.dumps({"recv_ns": "5", "msg": {}}),
        json.dumps({"recv_ns": True, "msg": {}}),
        "[" * 100_000,
        '{"recv_ns": ' + "1" * 5000 + ', "msg": {}}',
    ],
    ids=[
        "not-json",
        "missing-recv-ns",
        "negative-recv-ns",
        "string-recv-ns",
        "bool-recv-ns",
        "deeply-nested-brackets",
        "huge-int-literal",
    ],
)
def test_rejects_malformed_records(tmp_path: Path, line: str) -> None:
    file = write_lines(tmp_path / "bad.jsonl", [line])
    with pytest.raises(ReplayError, match="line 1"):
        list(read_replay(file))


def test_rejects_invalid_utf8(tmp_path: Path) -> None:
    file = tmp_path / "bad_utf8.jsonl"
    file.write_bytes(b"\xff\xfe\n")
    with pytest.raises(ReplayError):
        list(read_replay(file))


def test_fixture_replays_valid_kraken_messages() -> None:
    records = list(read_replay(FIXTURE))
    assert len(records) == 7
    recv_ns_values = [recv_ns for recv_ns, _, _ in records]
    assert recv_ns_values == sorted(recv_ns_values)
    for _, raw, venue in records:
        assert venue == "kraken"
        parse_message(raw)


def test_replay_skips_ohlc_header_and_calibration_reads_it(tmp_path: Path) -> None:
    ohlc = {"kind": "ohlc", "interval": 15, "data": json.loads(ohlc_body())}
    file = write_lines(
        tmp_path / "c.jsonl",
        [json.dumps(ohlc), json.dumps({"recv_ns": 5, "msg": {"channel": "heartbeat"}})],
    )
    assert [ns for ns, _, _ in read_replay(file)] == [5]
    assert len(read_calibration(file)[15]) == 2


@pytest.mark.parametrize(
    "line",
    [
        json.dumps({"kind": "ohlc", "interval": 5, "data": {}}),
        json.dumps({"kind": "ohlc", "interval": 15, "data": {"error": ["x"], "result": {}}}),
        json.dumps({"kind": "ohlc", "interval": True, "data": {}}),
    ],
    ids=["bad-interval", "kraken-error", "bool-interval"],
)
def test_bad_calibration_lines_raise(tmp_path: Path, line: str) -> None:
    with pytest.raises(ReplayError, match="line 1"):
        read_calibration(write_lines(tmp_path / "bad.jsonl", [line]))
