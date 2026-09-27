from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from slipstream.db import ResultsDB
from slipstream.site.__main__ import main
from slipstream.venue_rules import VenueRules

FEES = {"kraken": 40.0, "coinbase": 60.0}
RULES = {
    "kraken": VenueRules(min_qty=0.00005, qty_step=1e-8, min_notional=0.5),
    "coinbase": VenueRules(min_qty=1e-8, qty_step=1e-8, min_notional=1.0),
}


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("SLIPSTREAM_DATA_DIR", str(data))
    return data


def test_build_without_a_database_fails_cleanly(data_dir: Path) -> None:
    assert main(["build"]) == 1
    assert not (data_dir / "site").exists()


def test_build_writes_the_site_from_the_database(data_dir: Path) -> None:
    db = ResultsDB(data_dir / "slipstream.db")
    db.migrate()
    run_id = db.begin_run(datetime.now(UTC), "buy", 0.01, 600, FEES, RULES, None)
    db.finish_run(run_id, "completed", datetime.now(UTC))
    db.close()

    assert main(["build"]) == 0

    assert (data_dir / "site" / "index.html").exists()
    assert (data_dir / "site" / "data" / "runs.csv").exists()


def test_publish_without_the_flag_file_is_a_no_op(data_dir: Path) -> None:
    (data_dir / "site").mkdir()
    (data_dir / "site" / "index.html").write_text("<html></html>", encoding="utf-8")

    assert main(["publish"]) == 0
    assert not (data_dir / "publish-repo").exists()
