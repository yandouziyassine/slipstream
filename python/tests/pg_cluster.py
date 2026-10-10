"""A throwaway PostgreSQL cluster for the cloud-archive tests.

`pg_server` (session scope) runs one cluster: `initdb` into a pytest temp dir, Unix socket only
(no TCP), stopped at the end of the session. It is the stand-in for Supabase, so it has the same
superuser name (`postgres`) and the stub roles `anon` and `authenticated`.

`pg_cluster` (function scope) gives each test a fresh database with `cloud/supabase/001_schema.sql`
applied unchanged. Roles are cluster-wide, so the schema's `create role` would fail on the second
test; instead each test's teardown drops its database (which removes every privilege the role held
there) and then the role itself. The next test therefore starts from an empty cluster again.

If the PostgreSQL server binaries are missing, the fixtures skip, or fail when
`SLIPSTREAM_REQUIRE_PG=1` (CI sets it, so Postgres tests can never be skipped silently there).
Install them with `sudo apt-get install postgresql` (the server stays stopped as a service; the
tests start their own).
"""

from __future__ import annotations

import contextlib
import itertools
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, NoReturn

import psycopg
import pytest
from psycopg import sql

SCHEMA_FILE = Path(__file__).resolve().parents[2] / "cloud" / "supabase" / "001_schema.sql"
COLLECTOR_ROLE = "slipstream_collector"
_SUPERUSER = "postgres"
_CTL_TIMEOUT_S = 60


def find_bindir() -> Path | None:
    candidates: list[Path] = []
    pg_config = shutil.which("pg_config")
    if pg_config is not None:
        result = subprocess.run(
            [pg_config, "--bindir"], capture_output=True, text=True, timeout=30, check=False
        )
        if result.returncode == 0 and result.stdout.strip():
            candidates.append(Path(result.stdout.strip()))
    versions = Path("/usr/lib/postgresql")
    if versions.is_dir():
        found = [entry / "bin" for entry in versions.iterdir() if entry.name.isdigit()]
        candidates.extend(sorted(found, key=lambda p: int(p.parent.name), reverse=True))
    for candidate in candidates:
        if (candidate / "initdb").is_file() and (candidate / "pg_ctl").is_file():
            return candidate
    return None


class PgServer:
    """One cluster, reachable over a Unix socket only."""

    def __init__(self, bindir: Path, datadir: Path, sockdir: Path) -> None:
        self._bindir = bindir
        self._datadir = datadir
        self._sockdir = sockdir
        self._original_hba: str | None = None
        self._log = str(datadir.parent / "server.log")

    def conn_kwargs(self, dbname: str, user: str = _SUPERUSER) -> dict[str, Any]:
        return {"host": str(self._sockdir), "port": 5432, "dbname": dbname, "user": user}

    def start(self) -> None:
        self._run(
            self._bindir / "initdb",
            "-D",
            str(self._datadir),
            "-U",
            _SUPERUSER,
            "-A",
            "trust",
            "-E",
            "UTF8",
            "--locale=C",
            "--no-sync",
        )
        self._original_hba = (self._datadir / "pg_hba.conf").read_text(encoding="utf-8")
        options = (
            f"-c listen_addresses='' -c unix_socket_directories={self._sockdir} "
            "-c fsync=off -c synchronous_commit=off -c full_page_writes=off -c max_connections=30"
        )
        self._ctl("start", "-w", "-t", "30", "-l", self._log, "-o", options)
        with self.admin() as admin:
            admin.execute("create role anon nologin")
            admin.execute("create role authenticated nologin")

    def stop(self) -> None:
        self._ctl("stop", "-m", "immediate", "-w")

    def admin(self) -> psycopg.Connection[Any]:
        return psycopg.connect(**self.conn_kwargs("postgres"), autocommit=True)

    def set_hba(self, lines: Sequence[str]) -> None:
        """Replace pg_hba.conf and restart, so the change is in force when this returns."""
        (self._datadir / "pg_hba.conf").write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._ctl("restart", "-w", "-m", "fast", "-l", self._log)

    def reset_hba(self) -> None:
        if self._original_hba is None:
            return
        current = (self._datadir / "pg_hba.conf").read_text(encoding="utf-8")
        if current != self._original_hba:
            (self._datadir / "pg_hba.conf").write_text(self._original_hba, encoding="utf-8")
            self._ctl("restart", "-w", "-m", "fast", "-l", self._log)

    def _ctl(self, *args: str) -> None:
        self._run(self._bindir / "pg_ctl", "-D", str(self._datadir), *args)

    @staticmethod
    def _run(executable: Path, *args: str) -> None:
        try:
            subprocess.run(
                [str(executable), *args],
                capture_output=True,
                text=True,
                timeout=_CTL_TIMEOUT_S,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            pytest.fail(f"{executable.name} failed: {exc.stderr.strip()[:500]}")


class PgDatabase:
    """A fresh database with the Supabase schema applied, for one test."""

    def __init__(self, server: PgServer, name: str) -> None:
        self._server = server
        self.name = name
        self._open: list[psycopg.Connection[Any]] = []

    def connect(
        self, role: str | None = None, *, autocommit: bool = True
    ) -> psycopg.Connection[Any]:
        """A superuser connection; with `role`, the session then runs `SET ROLE <role>`."""
        conn = psycopg.connect(**self._server.conn_kwargs(self.name), autocommit=autocommit)
        self._open.append(conn)
        if role is not None:
            conn.execute(sql.SQL("set role {}").format(sql.Identifier(role)))
            if not autocommit:
                conn.commit()
        return conn

    def connect_as(self, user: str, password: str) -> psycopg.Connection[Any]:
        """A real login over the socket, subject to pg_hba.conf."""
        conn = psycopg.connect(
            **self._server.conn_kwargs(self.name, user=user), password=password, autocommit=True
        )
        self._open.append(conn)
        return conn

    def set_hba(self, lines: Sequence[str]) -> None:
        for conn in self._open:
            conn.close()
        self._open.clear()
        self._server.set_hba(lines)

    def drop(self) -> None:
        for conn in self._open:
            conn.close()
        self._open.clear()
        self._server.reset_hba()
        with self._server.admin() as admin:
            admin.execute(
                sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(self.name))
            )
            admin.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(COLLECTOR_ROLE)))


_database_numbers = itertools.count(1)


def _unavailable(reason: str) -> NoReturn:
    if os.environ.get("SLIPSTREAM_REQUIRE_PG") == "1":
        pytest.fail(f"{reason}, and SLIPSTREAM_REQUIRE_PG=1 forbids skipping the Postgres tests")
    pytest.skip(reason)


@pytest.fixture(scope="session")
def pg_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PgServer]:
    bindir = find_bindir()
    if bindir is None:
        _unavailable("PostgreSQL server binaries not found (apt-get install postgresql)")
    # A Unix socket path is limited to about 100 bytes, which a pytest temp dir can exceed.
    sockdir = Path(tempfile.mkdtemp(prefix="slpg-"))
    server = PgServer(bindir, tmp_path_factory.mktemp("pgdata") / "data", sockdir)
    try:
        server.start()
        yield server
    finally:
        with contextlib.suppress(Exception, pytest.fail.Exception):
            server.stop()
        shutil.rmtree(sockdir, ignore_errors=True)


@pytest.fixture
def pg_cluster(pg_server: PgServer) -> Iterator[PgDatabase]:
    name = f"t_{next(_database_numbers)}"
    with pg_server.admin() as admin:
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
    database = PgDatabase(pg_server, name)
    try:
        with database.connect() as conn:
            conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
        yield database
    finally:
        database.drop()
