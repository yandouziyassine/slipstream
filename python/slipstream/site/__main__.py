from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from slipstream.db import ResultsDB
from slipstream.logging_setup import configure_logging
from slipstream.site.publish import publish as publish_site
from slipstream.site.render import write_site


def _default_data_dir() -> Path:
    raw = os.environ.get("SLIPSTREAM_DATA_DIR")
    return Path(raw) if raw else Path.home() / "slipstream-data"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="slipstream.site")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("build", help="write site/ from the results database")
    commands.add_parser("publish", help="publish site/ to slipstream-live, if enabled")
    return parser


def _build(data_dir: Path) -> int:
    log = configure_logging()
    db_path = data_dir / "slipstream.db"
    if not db_path.exists():
        log.error(f"no database at {db_path}")
        return 1
    db = ResultsDB(db_path)
    try:
        db.migrate()
        write_site(db.connection, data_dir / "site", datetime.now(UTC))
    finally:
        db.close()
    log.info("site build complete", extra={"fields": {"event": "site_build"}})
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_dir = _default_data_dir()
    if args.command == "build":
        return _build(data_dir)
    log = configure_logging()
    return publish_site(data_dir / "site", data_dir, log)


if __name__ == "__main__":
    sys.exit(main())
