"""Every venue Slipstream can stream. Adding a venue: one adapter module plus one line here."""

from __future__ import annotations

import re
from collections.abc import Sequence

from slipstream.venues.base import VenueAdapter
from slipstream.venues.coinbase import COINBASE
from slipstream.venues.kraken import KRAKEN

# The engine accepts the same names: lowercase, starting with a letter, at most 16 characters.
_NAME = re.compile(r"[a-z][a-z0-9]{0,15}")


def check_adapters(adapters: Sequence[VenueAdapter]) -> dict[str, VenueAdapter]:
    by_name: dict[str, VenueAdapter] = {}
    for venue in adapters:
        if _NAME.fullmatch(venue.name) is None:
            raise ValueError(f"invalid venue name {venue.name!r}")
        if venue.name in by_name:
            raise ValueError(f"duplicate venue {venue.name!r}")
        if not venue.ws_url.startswith("wss://"):
            raise ValueError(f"{venue.name} WebSocket URL must use wss://")
        by_name[venue.name] = venue
    return by_name


ADAPTERS: tuple[VenueAdapter, ...] = (
    KRAKEN,
    COINBASE,
)
VENUES: tuple[str, ...] = tuple(venue.name for venue in ADAPTERS)
_BY_NAME = check_adapters(ADAPTERS)


def adapter(name: str) -> VenueAdapter:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise ValueError(f"unknown venue {name[:32]!r}") from None
