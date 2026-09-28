import json
from pathlib import Path

import pytest
from test_kraken import DOC_SNAPSHOT
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


BOOK_ACK = {
    "method": "subscribe",
    "result": {"channel": "book", "depth": 10, "snapshot": True, "symbol": "BTC/USD"},
    "success": True,
}
FLIPPED_SNAPSHOT = DOC_SNAPSHOT.replace('"qty":0.00100000', '"qty":0.00100001')


def raw_line(recv_ns: int, msg: str, venue: str = "kraken") -> str:
    """A record spelled the way the recorder writes it: the venue's message text kept verbatim."""
    return f'{{"recv_ns": {recv_ns}, "venue": "{venue}", "msg": {msg}}}'


def test_fixture_book_checksums_verify() -> None:
    assert len(list(read_replay(FIXTURE))) == 7


def test_verifies_the_recorded_wire_text(tmp_path: Path) -> None:
    file = write_lines(
        tmp_path / "r.jsonl", [raw_line(1, json.dumps(BOOK_ACK)), raw_line(2, DOC_SNAPSHOT)]
    )
    assert len(list(read_replay(file))) == 2


def test_recorded_checksum_mismatch_raises_with_its_line(tmp_path: Path) -> None:
    file = write_lines(
        tmp_path / "r.jsonl", [raw_line(1, json.dumps(BOOK_ACK)), raw_line(2, FLIPPED_SNAPSHOT)]
    )
    records = read_replay(file)
    assert next(records)[0] == 1
    with pytest.raises(ReplayError, match="line 2: kraken BTC/USD book checksum mismatch"):
        next(records)


def test_books_before_any_recorded_book_subscription_are_not_verified(tmp_path: Path) -> None:
    file = write_lines(tmp_path / "r.jsonl", [raw_line(1, FLIPPED_SNAPSHOT)])
    assert len(list(read_replay(file))) == 1


def test_books_without_checksums_are_not_verified(tmp_path: Path) -> None:
    no_checksum = DOC_SNAPSHOT.replace(',"checksum":3310070434', "")
    corrupted = no_checksum.replace('"qty":0.00100000', '"qty":0.00100001')
    file = write_lines(
        tmp_path / "r.jsonl",
        [raw_line(1, corrupted), raw_line(2, json.dumps(BOOK_ACK)), raw_line(3, corrupted)],
    )
    assert len(list(read_replay(file))) == 3


def test_coinbase_records_are_not_kraken_checked(tmp_path: Path) -> None:
    file = write_lines(tmp_path / "r.jsonl", [raw_line(1, FLIPPED_SNAPSHOT, venue="coinbase")])
    assert len(list(read_replay(file))) == 1


def test_malformed_book_subscription_raises_with_its_line(tmp_path: Path) -> None:
    ack = {**BOOK_ACK, "result": {"channel": "book", "depth": 7, "symbol": "BTC/USD"}}
    file = write_lines(tmp_path / "r.jsonl", [raw_line(1, json.dumps(ack))])
    with pytest.raises(ReplayError, match="line 1: malformed book subscription"):
        list(read_replay(file))


def test_a_new_book_subscription_starts_a_new_mirror(tmp_path: Path) -> None:
    update = (
        '{"channel":"book","type":"update","data":[{"symbol":"BTC/USD","bids":[],"asks":[],'
        '"checksum":3310070434}]}'
    )
    file = write_lines(
        tmp_path / "r.jsonl",
        [
            raw_line(1, json.dumps(BOOK_ACK)),
            raw_line(2, DOC_SNAPSHOT),
            raw_line(3, json.dumps(BOOK_ACK)),
            raw_line(4, update),
        ],
    )
    with pytest.raises(ReplayError, match=r"line 4: .*before the snapshot"):
        list(read_replay(file))


def test_replayed_messages_keep_their_float_values(tmp_path: Path) -> None:
    file = write_lines(
        tmp_path / "r.jsonl", [raw_line(1, json.dumps(BOOK_ACK)), raw_line(2, DOC_SNAPSHOT)]
    )
    _, raw, _ = list(read_replay(file))[1]
    assert raw == json.dumps(json.loads(DOC_SNAPSHOT))
