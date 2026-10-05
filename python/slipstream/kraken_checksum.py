from __future__ import annotations

import heapq
import zlib
from collections.abc import Iterable
from decimal import Decimal

from slipstream.models import MarketDataError

BOOK_DEPTHS = frozenset({10, 25, 100, 500, 1000})
CHECKSUM_LEVELS = 10
MAX_DIGITS = 32
MAX_SCALE = 18

Level = tuple[Decimal, Decimal]


class BookChecksumError(MarketDataError):
    pass


def level_text(value: Decimal) -> str:
    """A price or qty as Kraken checksums it: the wire decimal without '.' or leading zeros."""
    if not value.is_finite() or value < 0:
        raise BookChecksumError(f"cannot checksum {value!s:.32}")
    _, digits, exponent = value.as_tuple()
    if len(digits) > MAX_DIGITS or not -MAX_SCALE <= int(exponent) <= MAX_SCALE:
        raise BookChecksumError(f"cannot checksum {value!s:.32}")
    return format(value, "f").replace(".", "").lstrip("0")


def book_checksum(asks: Iterable[Level], bids: Iterable[Level]) -> int:
    """CRC32 of asks best-first then bids best-first, as Kraken's v2 book channel computes it."""
    text = "".join(level_text(price) + level_text(qty) for price, qty in (*asks, *bids))
    return zlib.crc32(text.encode("ascii"))


class KrakenBook:
    """One Kraken v2 book subscription, mirrored at its subscribed depth to check its checksums."""

    def __init__(self, depth: int) -> None:
        if depth not in BOOK_DEPTHS:
            raise ValueError(f"depth must be one of {sorted(BOOK_DEPTHS)}")
        self._depth = depth
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}

    def apply(self, is_snapshot: bool, bids: Iterable[Level], asks: Iterable[Level]) -> None:
        if is_snapshot:
            self._bids.clear()
            self._asks.clear()
        _apply_side(self._bids, bids)
        _apply_side(self._asks, asks)
        # Kraken sends no delete for a level pushed past the subscribed depth.
        if len(self._bids) > self._depth:
            self._bids = dict(heapq.nlargest(self._depth, self._bids.items()))
        if len(self._asks) > self._depth:
            self._asks = dict(heapq.nsmallest(self._depth, self._asks.items()))

    def checksum(self) -> int:
        return book_checksum(
            heapq.nsmallest(CHECKSUM_LEVELS, self._asks.items()),
            heapq.nlargest(CHECKSUM_LEVELS, self._bids.items()),
        )


def _apply_side(side: dict[Decimal, Decimal], levels: Iterable[Level]) -> None:
    for price, qty in levels:
        if qty == 0:
            side.pop(price, None)
        else:
            side[price] = qty
