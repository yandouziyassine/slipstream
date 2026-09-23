from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class BookUpdate:
    symbol: str
    is_snapshot: bool
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class OrderSpec:
    order_id: str
    side: Side
    qty: float
    duration_s: int
    num_slices: int


@dataclass(frozen=True)
class Fill:
    order_id: str
    ts_ns: int
    qty: float
    price: float
