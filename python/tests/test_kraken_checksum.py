from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from slipstream.kraken_checksum import (
    BookChecksumError,
    KrakenBook,
    book_checksum,
    level_text,
)

# The worked example from https://docs.kraken.com/api/docs/guides/spot-ws-book-v2
DOC_BIDS = [
    ("45283.5", "0.10000000"),
    ("45283.4", "1.54582015"),
    ("45282.1", "0.10000000"),
    ("45281.0", "0.10000000"),
    ("45280.3", "1.54592586"),
    ("45279.0", "0.07990000"),
    ("45277.6", "0.03310103"),
    ("45277.5", "0.30000000"),
    ("45277.3", "1.54602737"),
    ("45276.6", "0.15445238"),
]
DOC_ASKS = [
    ("45285.2", "0.00100000"),
    ("45286.4", "1.54571953"),
    ("45286.6", "1.54571109"),
    ("45289.6", "1.54560911"),
    ("45290.2", "0.15890660"),
    ("45291.8", "1.54553491"),
    ("45294.7", "0.04454749"),
    ("45296.1", "0.35380000"),
    ("45297.5", "0.09945542"),
    ("45299.5", "0.18772827"),
]
DOC_ASKS_TEXT = (
    "4528521000004528641545719534528661545711094528961545609114529021589066045291815455349"
    "1452947445474945296135380000452975994554245299518772827"
)
DOC_BIDS_TEXT = (
    "4528351000000045283415458201545282110000000452810100000004528031545925864527907990000"
    "45277633101034527753000000045277315460273745276615445238"
)
DOC_CHECKSUM = 3310070434


def levels(rows: list[tuple[str, str]]) -> list[tuple[Decimal, Decimal]]:
    return [(Decimal(price), Decimal(qty)) for price, qty in rows]


def test_formats_the_documented_example_exactly() -> None:
    asks = "".join(level_text(p) + level_text(q) for p, q in levels(DOC_ASKS))
    bids = "".join(level_text(p) + level_text(q) for p, q in levels(DOC_BIDS))
    assert asks == DOC_ASKS_TEXT
    assert bids == DOC_BIDS_TEXT


def test_documented_example_checksum() -> None:
    assert book_checksum(levels(DOC_ASKS), levels(DOC_BIDS)) == DOC_CHECKSUM


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("0.00100000", "100000"),
        ("45285.2", "452852"),
        ("45281.0", "452810"),
        ("0.10000000", "10000000"),
        ("100", "100"),
        ("1E-8", "1"),
        ("0.5", "5"),
    ],
    ids=[
        "leading-zeros",
        "price",
        "trailing-zero",
        "trailing-zeros",
        "integer",
        "exponent",
        "half",
    ],
)
def test_level_text_drops_the_point_and_leading_zeros_only(value: str, text: str) -> None:
    assert level_text(Decimal(value)) == text


@pytest.mark.parametrize(
    "value",
    ["1E-19", "1E+19", "1" * 33, "NaN", "Infinity", "-1"],
    ids=["tiny-scale", "huge-exponent", "too-many-digits", "nan", "infinity", "negative"],
)
def test_level_text_rejects_values_kraken_never_sends(value: str) -> None:
    with pytest.raises(BookChecksumError):
        level_text(Decimal(value))


def test_rejects_unsupported_depth() -> None:
    with pytest.raises(ValueError, match="depth"):
        KrakenBook(7)


def test_snapshot_matches_the_documented_checksum() -> None:
    book = KrakenBook(10)
    book.apply(True, levels(DOC_BIDS), levels(DOC_ASKS))
    assert book.checksum() == DOC_CHECKSUM


def test_checksum_covers_only_the_top_ten_levels() -> None:
    book = KrakenBook(25)
    deeper_bids = [*levels(DOC_BIDS), (Decimal("45000.0"), Decimal("1.00000000"))]
    deeper_asks = [*levels(DOC_ASKS), (Decimal("46000.0"), Decimal("1.00000000"))]
    book.apply(True, deeper_bids, deeper_asks)
    assert book.checksum() == DOC_CHECKSUM


def test_levels_are_ordered_regardless_of_message_order() -> None:
    book = KrakenBook(10)
    book.apply(True, list(reversed(levels(DOC_BIDS))), list(reversed(levels(DOC_ASKS))))
    assert book.checksum() == DOC_CHECKSUM


def test_update_changes_deletes_and_inserts_levels() -> None:
    book = KrakenBook(10)
    book.apply(True, levels(DOC_BIDS), levels(DOC_ASKS))
    book.apply(False, [(Decimal("45283.4"), Decimal("0.00000000"))], [])
    book.apply(False, [(Decimal("45276.5"), Decimal("0.20000000"))], [])
    book.apply(False, [], [(Decimal("45285.2"), Decimal("0.00200000"))])
    expected_bids = [*(row for row in DOC_BIDS if row[0] != "45283.4"), ("45276.5", "0.20000000")]
    expected_asks = [("45285.2", "0.00200000"), *DOC_ASKS[1:]]
    assert book.checksum() == book_checksum(levels(expected_asks), levels(expected_bids))


def test_levels_pushed_past_the_depth_are_dropped() -> None:
    book = KrakenBook(10)
    book.apply(True, levels(DOC_BIDS), levels(DOC_ASKS))
    book.apply(False, [(Decimal("45284.0"), Decimal("1.00000000"))], [])
    # Kraken sends no delete for the level that falls out of scope; it must not come back.
    book.apply(False, [(Decimal("45284.0"), Decimal("0.00000000"))], [])
    expected_bids = DOC_BIDS[:9]
    assert book.checksum() == book_checksum(levels(DOC_ASKS), levels(expected_bids))


def test_snapshot_replaces_the_whole_book() -> None:
    book = KrakenBook(10)
    book.apply(True, [(Decimal("1.0"), Decimal("1.0"))], [(Decimal("2.0"), Decimal("1.0"))])
    book.apply(True, levels(DOC_BIDS), levels(DOC_ASKS))
    assert book.checksum() == DOC_CHECKSUM


def test_flipped_digit_changes_the_checksum() -> None:
    flipped = [("45285.2", "0.00100001"), *DOC_ASKS[1:]]
    assert book_checksum(levels(flipped), levels(DOC_BIDS)) != DOC_CHECKSUM


_PRICE_CENTS = st.integers(min_value=1, max_value=10**9)
_QTY_UNITS = st.integers(min_value=1, max_value=10**12)
_SIDE = st.dictionaries(_PRICE_CENTS, _QTY_UNITS, min_size=1, max_size=15)


def _side(raw: dict[int, int]) -> list[tuple[Decimal, Decimal]]:
    return [(Decimal(p).scaleb(-1), Decimal(q).scaleb(-8)) for p, q in raw.items()]


@given(bids=_SIDE, asks=_SIDE, data=st.data())
@settings(deadline=None)
def test_any_single_level_mutation_changes_the_checksum(
    bids: dict[int, int], asks: dict[int, int], data: st.DataObject
) -> None:
    book = KrakenBook(10)
    book.apply(True, _side(bids), _side(asks))
    before = book.checksum()
    again = KrakenBook(10)
    again.apply(True, list(reversed(_side(bids))), list(reversed(_side(asks))))
    assert again.checksum() == before

    top_bids = sorted(bids, reverse=True)[:10]
    top_asks = sorted(asks)[:10]
    is_bid = data.draw(st.booleans())
    side, top = (bids, top_bids) if is_bid else (asks, top_asks)
    price = data.draw(st.sampled_from(top))
    new_qty = data.draw(_QTY_UNITS.filter(lambda q: q != side[price]))
    mutated = KrakenBook(10)
    mutated.apply(True, _side(bids), _side(asks))
    change = [(Decimal(price).scaleb(-1), Decimal(new_qty).scaleb(-8))]
    mutated.apply(False, change if is_bid else [], [] if is_bid else change)
    # The checksum text always changes; a CRC32 collision is a 2**-32 chance per example.
    assert mutated.checksum() != before
