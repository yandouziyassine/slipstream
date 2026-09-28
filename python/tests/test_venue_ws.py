import json

import pytest
from test_kraken import DOC_SNAPSHOT

from slipstream.kraken import KrakenMessageError
from slipstream.kraken_checksum import BookChecksumError
from slipstream.models import BookUpdate
from slipstream.venue_ws import parser


def test_kraken_parser_checks_book_checksums() -> None:
    parse = parser("kraken", "BTC/USD", 10)
    assert isinstance(parse(DOC_SNAPSHOT), BookUpdate)
    with pytest.raises(BookChecksumError, match="kraken BTC/USD"):
        parse(DOC_SNAPSHOT.replace('"qty":0.00100000', '"qty":0.00100001'))


def test_kraken_parser_requires_a_checksum() -> None:
    parse = parser("kraken", "BTC/USD", 10)
    with pytest.raises(KrakenMessageError, match="missing checksum"):
        parse(DOC_SNAPSHOT.replace(',"checksum":3310070434', ""))


def test_kraken_parser_is_fresh_per_connection() -> None:
    update = json.dumps(
        {
            "channel": "book",
            "type": "update",
            "data": [{"symbol": "BTC/USD", "bids": [], "asks": [], "checksum": 3310070434}],
        }
    )
    first = parser("kraken", "BTC/USD", 10)
    first(DOC_SNAPSHOT)
    assert isinstance(first(update), BookUpdate)
    with pytest.raises(KrakenMessageError, match="before the snapshot"):
        parser("kraken", "BTC/USD", 10)(update)
