import dataclasses
import re

import pytest

from slipstream import models
from slipstream.venue_rules import VenueRules, fetch_venue_rules
from slipstream.venues import registry
from slipstream.venues.registry import ADAPTERS, VENUES, adapter, check_adapters

_NAME = re.compile(r"^[a-z][a-z0-9]{0,15}$")


def test_venues_are_the_registered_adapter_names_in_order() -> None:
    assert VENUES == ("kraken", "coinbase")
    assert tuple(a.name for a in ADAPTERS) == VENUES


def test_models_copy_of_the_venue_names_matches_the_registry() -> None:
    assert models.VENUES == VENUES


def test_adapter_looks_up_by_name() -> None:
    for name in VENUES:
        assert adapter(name).name == name


def test_adapter_rejects_an_unregistered_name() -> None:
    with pytest.raises(ValueError, match="unknown venue 'binance'"):
        adapter("binance")


def test_registered_names_are_engine_safe() -> None:
    assert all(_NAME.match(name) for name in VENUES)


@pytest.mark.parametrize("name", ["", "Kraken", "1kraken", "kra-ken", "k" * 17, "kraken "])
def test_check_adapters_rejects_names_the_engine_would_refuse(name: str) -> None:
    bad = dataclasses.replace(ADAPTERS[0], name=name)
    with pytest.raises(ValueError, match="venue name"):
        check_adapters((bad,))


def test_check_adapters_rejects_duplicates() -> None:
    with pytest.raises(ValueError, match="duplicate venue"):
        check_adapters((ADAPTERS[0], ADAPTERS[0]))


def test_check_adapters_rejects_a_plaintext_url() -> None:
    bad = dataclasses.replace(ADAPTERS[0], ws_url="ws://example.com")
    with pytest.raises(ValueError, match="wss://"):
        check_adapters((bad,))


def test_fetch_venue_rules_dispatches_through_the_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_rules(symbol: str, fetch: object) -> VenueRules:
        seen.append(symbol)
        return VenueRules(1.0, 0.5, 2.0)

    fake = dataclasses.replace(ADAPTERS[0], name="fakevenue", fetch_rules=fake_rules)
    monkeypatch.setitem(registry._BY_NAME, "fakevenue", fake)
    assert fetch_venue_rules(("fakevenue",), "BTC/USD", fetch=lambda _url: b"") == {
        "fakevenue": VenueRules(1.0, 0.5, 2.0)
    }
    assert seen == ["BTC/USD"]
