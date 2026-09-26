import pytest

from slipstream.book import LocalBook, consolidated_book
from slipstream.models import BookUpdate


def test_snapshot_then_deltas_keep_top_depth() -> None:
    book = LocalBook(depth=2)
    book.apply(BookUpdate("BTC/USD", True, ((99.0, 1.0), (98.0, 1.0)), ((101.0, 1.0),)))
    book.apply(BookUpdate("BTC/USD", False, ((99.5, 2.0), (98.0, 0.0)), ((102.0, 3.0),)))
    assert book.bids() == ((99.5, 2.0), (99.0, 1.0))
    assert book.asks() == ((101.0, 1.0), (102.0, 3.0))
    book.apply(BookUpdate("BTC/USD", False, ((97.0, 1.0),), ()))
    assert book.bids() == ((99.5, 2.0), (99.0, 1.0))


def test_snapshot_replaces_everything() -> None:
    book = LocalBook(depth=10)
    book.apply(BookUpdate("BTC/USD", True, ((99.0, 1.0),), ((101.0, 1.0),)))
    book.apply(BookUpdate("BTC/USD", True, ((50.0, 1.0),), ()))
    assert book.bids() == ((50.0, 1.0),)
    assert book.asks() == ()


def test_depth_must_be_positive() -> None:
    with pytest.raises(ValueError):
        LocalBook(depth=0)


def test_consolidated_book_merges_at_fee_adjusted_prices() -> None:
    kraken = LocalBook(depth=10)
    kraken.apply(BookUpdate("BTC/USD", True, ((99.0, 1.0),), ((100.0, 1.0), (100.3, 2.0))))
    coinbase = LocalBook(depth=10)
    coinbase.apply(BookUpdate("BTC/USD", True, ((99.2, 1.0),), ((100.2, 5.0),), "coinbase"))
    merged = consolidated_book(
        "BTC/USD", {"kraken": kraken, "coinbase": coinbase}, {"kraken": 0.0, "coinbase": 20.0}
    )
    # asks: kraken 100.0 and 100.3 unchanged; coinbase 100.2 x 1.002 = 100.4004 ranks last.
    # pytest.approx() on this pytest version does not support nesting past one level, so the
    # per-price tolerance is applied by hand instead of wrapping the whole tuple of tuples.
    assert merged.asks == (
        (pytest.approx(100.0), 1.0),
        (pytest.approx(100.3), 2.0),
        (pytest.approx(100.4004), 5.0),
    )
    # bids: coinbase 99.2 x 0.998 = 99.0016 ranks above kraken 99.0.
    assert merged.bids == ((pytest.approx(99.0016), 1.0), (pytest.approx(99.0), 1.0))
