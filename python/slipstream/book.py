from __future__ import annotations

from collections.abc import Mapping

from slipstream.models import BookUpdate, Venue


class LocalBook:
    """One venue's book as the engine holds it: snapshots replace, deltas upsert, top N kept."""

    def __init__(self, depth: int) -> None:
        if depth < 1:
            raise ValueError("depth must be at least 1")
        self._depth = depth
        self._bids: dict[float, float] = {}
        self._asks: dict[float, float] = {}

    def apply(self, update: BookUpdate) -> None:
        if update.is_snapshot:
            self._bids.clear()
            self._asks.clear()
        for side, levels in ((self._bids, update.bids), (self._asks, update.asks)):
            for price, qty in levels:
                if qty == 0:
                    side.pop(price, None)
                else:
                    side[price] = qty
        self._bids = dict(self.bids())
        self._asks = dict(self.asks())

    def bids(self) -> tuple[tuple[float, float], ...]:
        return tuple(sorted(self._bids.items(), reverse=True)[: self._depth])

    def asks(self) -> tuple[tuple[float, float], ...]:
        return tuple(sorted(self._asks.items())[: self._depth])


def consolidated_book(
    symbol: str, books: Mapping[Venue, LocalBook], fee_bps: Mapping[Venue, float]
) -> BookUpdate:
    """All venues' levels at fee-adjusted prices, ranked the way the engine's router ranks them."""
    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    for venue, book in books.items():
        fee = fee_bps.get(venue, 0.0) / 1e4
        bids.extend((price * (1 - fee), qty) for price, qty in book.bids())
        asks.extend((price * (1 + fee), qty) for price, qty in book.asks())
    return BookUpdate(symbol, True, tuple(sorted(bids, reverse=True)), tuple(sorted(asks)))
