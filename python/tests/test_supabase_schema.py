from __future__ import annotations

from typing import Any

import psycopg
import pytest
from pg_cluster import COLLECTOR_ROLE, SCHEMA_FILE, PgDatabase
from psycopg import errors, sql

TABLES = (
    "runs",
    "results",
    "engine_stats",
    "recordings",
    "fill_summaries",
    "recording_deletions",
    "archive_days",
    "recording_uploads",
)
SHA = "a" * 64

# Parents first. Every table gets one row; a column that is safe to rewrite is named for the
# UPDATE attempts.
INSERTS: dict[str, tuple[str, tuple[Any, ...]]] = {
    "runs": (
        "insert into slipstream.runs (id, started_at, ended_at, status, side, qty, duration_s, "
        "fees, venue_rules, git_commit, error, supersedes_id) "
        "values (1, now(), now(), 'completed', 'buy', 0.01, 600, '{}', '{}', 'abc', null, null)",
        (),
    ),
    "results": (
        "insert into slipstream.results values "
        "(1, 1, 'twap', 'COMPLETED', 0.01, 100, 1e5, 1e5, 1, 1, 1, 1, null, '[]', 3, null)",
        (),
    ),
    "engine_stats": ("insert into slipstream.engine_stats values (1, 1, 10, 20, 30)", ()),
    "recordings": (
        "insert into slipstream.recordings values (1, 1, '2026-09-27/10-0p01.jsonl.gz', %s, now())",
        (SHA,),
    ),
    "fill_summaries": (
        "insert into slipstream.fill_summaries "
        "values (1, 'twap', 'kraken', 3, 0.01, 1000, 0.1, 1, 2)",
        (),
    ),
    "recording_deletions": ("insert into slipstream.recording_deletions values (1, 1, now())", ()),
    "archive_days": (
        "insert into slipstream.archive_days values "
        "(1, '2026-09-27', %s, 'manifests/a.jsonl', %s, 'fills/a.csv.gz', %s, 3, now())",
        ("c" * 40, SHA, SHA),
    ),
    "recording_uploads": (
        "insert into slipstream.recording_uploads values (1, 1, 1, 'recordings/a.gz', %s, 12)",
        (SHA,),
    ),
}
UPDATES = {
    "runs": "set qty = qty + 1",
    "results": "set filled_qty = filled_qty + 1",
    "engine_stats": "set events = events + 1",
    "recordings": "set rel_path = 'x'",
    "fill_summaries": "set qty = qty + 1",
    "recording_deletions": "set run_id = run_id",
    "archive_days": "set fills_rows = fills_rows + 1",
    "recording_uploads": "set bytes = bytes + 1",
}


def _table(name: str) -> sql.Identifier:
    return sql.Identifier("slipstream", name)


def _seed(conn: psycopg.Connection[Any]) -> None:
    for table in TABLES:
        statement, params = INSERTS[table]
        conn.execute(statement, params)


def _count(conn: psycopg.Connection[Any], table: str) -> int:
    row = conn.execute(sql.SQL("select count(*) from {}").format(_table(table))).fetchone()
    assert row is not None
    return int(row[0])


def test_the_schema_applies_and_creates_the_eight_tables(pg_cluster: PgDatabase) -> None:
    conn = pg_cluster.connect()

    rows = conn.execute(
        "select tablename from pg_tables where schemaname = 'slipstream'"
    ).fetchall()

    assert {row[0] for row in rows} == set(TABLES)


def test_running_the_schema_twice_fails_and_changes_nothing(pg_cluster: PgDatabase) -> None:
    conn = pg_cluster.connect()
    before = conn.execute(
        "select count(*) from pg_class where relnamespace = 'slipstream'::regnamespace"
    ).fetchone()

    with pytest.raises(errors.DuplicateSchema):
        pg_cluster.connect().execute(SCHEMA_FILE.read_text(encoding="utf-8"))

    after = conn.execute(
        "select count(*) from pg_class where relnamespace = 'slipstream'::regnamespace"
    ).fetchone()
    assert before == after


def test_the_collector_can_insert_and_select_on_every_table(pg_cluster: PgDatabase) -> None:
    conn = pg_cluster.connect(COLLECTOR_ROLE)

    _seed(conn)

    for table in TABLES:
        assert _count(conn, table) == 1


def test_the_collector_can_skip_rows_that_already_exist(pg_cluster: PgDatabase) -> None:
    conn = pg_cluster.connect(COLLECTOR_ROLE)
    _seed(conn)

    for table in TABLES:
        statement, params = INSERTS[table]
        cursor = conn.execute(statement + " on conflict do nothing", params)
        assert cursor.rowcount == 0, table


def test_the_collector_cannot_turn_a_conflict_into_an_update(pg_cluster: PgDatabase) -> None:
    conn = pg_cluster.connect(COLLECTOR_ROLE)
    _seed(conn)

    with pytest.raises(errors.InsufficientPrivilege):
        conn.execute(
            "insert into slipstream.engine_stats values (1, 1, 10, 20, 30) "
            "on conflict (id) do update set events = 0"
        )


@pytest.mark.parametrize("table", TABLES)
def test_the_collector_cannot_update_delete_or_truncate(pg_cluster: PgDatabase, table: str) -> None:
    _seed(pg_cluster.connect())
    conn = pg_cluster.connect(COLLECTOR_ROLE)

    with pytest.raises(errors.InsufficientPrivilege):
        conn.execute(sql.SQL("update {} {}").format(_table(table), sql.SQL(UPDATES[table])))
    with pytest.raises(errors.InsufficientPrivilege):
        conn.execute(sql.SQL("delete from {}").format(_table(table)))
    with pytest.raises(errors.InsufficientPrivilege):
        conn.execute(sql.SQL("truncate {}").format(_table(table)))
    assert _count(conn, table) == 1


@pytest.mark.parametrize("table", TABLES)
def test_even_the_owner_cannot_update_delete_or_truncate(
    pg_cluster: PgDatabase, table: str
) -> None:
    conn = pg_cluster.connect()
    _seed(conn)

    with pytest.raises(errors.RaiseException, match="append-only"):
        conn.execute(sql.SQL("update {} {}").format(_table(table), sql.SQL(UPDATES[table])))
    with pytest.raises(errors.RaiseException, match="append-only"):
        conn.execute(sql.SQL("delete from {}").format(_table(table)))
    # CASCADE, because Postgres refuses a plain TRUNCATE of a referenced table before any trigger.
    with pytest.raises(errors.RaiseException, match="append-only"):
        conn.execute(sql.SQL("truncate {} cascade").format(_table(table)))
    assert _count(conn, table) == 1


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_the_data_api_roles_can_read_nothing(pg_cluster: PgDatabase, role: str) -> None:
    _seed(pg_cluster.connect())
    conn = pg_cluster.connect(role)

    for table in TABLES:
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(sql.SQL("select * from {}").format(_table(table)))
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(
                sql.SQL("insert into {} select * from {}").format(_table(table), _table(table))
            )


@pytest.mark.parametrize("role", ["anon", "authenticated", "public"])
def test_the_data_api_roles_have_no_usage_on_the_schema(pg_cluster: PgDatabase, role: str) -> None:
    conn = pg_cluster.connect()

    row = conn.execute("select has_schema_privilege(%s, 'slipstream', 'USAGE')", (role,)).fetchone()

    assert row == (False,)


def test_the_collector_has_usage_on_the_schema(pg_cluster: PgDatabase) -> None:
    row = (
        pg_cluster.connect()
        .execute("select has_schema_privilege(%s, 'slipstream', 'USAGE')", (COLLECTOR_ROLE,))
        .fetchone()
    )

    assert row == (True,)


def test_row_level_security_is_on_for_every_table(pg_cluster: PgDatabase) -> None:
    rows = (
        pg_cluster.connect()
        .execute(
            "select relname, relrowsecurity from pg_class "
            "where relnamespace = 'slipstream'::regnamespace and relkind = 'r'"
        )
        .fetchall()
    )

    assert dict(rows) == dict.fromkeys(TABLES, True)


def test_the_collector_role_is_locked_down(pg_cluster: PgDatabase) -> None:
    row = (
        pg_cluster.connect()
        .execute(
            "select rolcanlogin, rolinherit, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, "
            "rolreplication, rolpassword is null from pg_authid where rolname = %s",
            (COLLECTOR_ROLE,),
        )
        .fetchone()
    )

    assert row == (True, False, False, False, False, False, False, True)


def test_the_collector_role_has_a_statement_timeout(pg_cluster: PgDatabase) -> None:
    row = (
        pg_cluster.connect()
        .execute("select rolconfig from pg_roles where rolname = %s", (COLLECTOR_ROLE,))
        .fetchone()
    )

    assert row is not None
    assert "statement_timeout=60s" in row[0]


def test_the_collector_role_is_not_a_member_of_any_role(pg_cluster: PgDatabase) -> None:
    row = (
        pg_cluster.connect()
        .execute(
            "select count(*) from pg_auth_members where member = %s::regrole", (COLLECTOR_ROLE,)
        )
        .fetchone()
    )

    assert row == (0,)


def test_the_collector_has_no_table_privilege_beyond_select_and_insert(
    pg_cluster: PgDatabase,
) -> None:
    conn = pg_cluster.connect()

    for table in TABLES:
        granted = {
            privilege
            for privilege in (
                "SELECT",
                "INSERT",
                "UPDATE",
                "DELETE",
                "TRUNCATE",
                "REFERENCES",
                "TRIGGER",
            )
            if conn.execute(
                "select has_table_privilege(%s, %s, %s)",
                (COLLECTOR_ROLE, f"slipstream.{table}", privilege),
            ).fetchone()
            == (True,)
        }
        assert granted == {"SELECT", "INSERT"}, table


def test_the_trigger_function_is_not_callable_by_the_data_api_roles(pg_cluster: PgDatabase) -> None:
    conn = pg_cluster.connect()

    for role in ("anon", "authenticated", COLLECTOR_ROLE, "public"):
        row = conn.execute(
            "select has_function_privilege(%s, 'slipstream.append_only()', 'EXECUTE')", (role,)
        ).fetchone()
        assert row == (False,), role


def test_children_need_their_parent_run(pg_cluster: PgDatabase) -> None:
    conn = pg_cluster.connect(COLLECTOR_ROLE)

    with pytest.raises(errors.ForeignKeyViolation):
        conn.execute("insert into slipstream.engine_stats values (1, 99, 10, 20, 30)")


@pytest.mark.parametrize(
    "values",
    [
        "(1, now(), null, 'started', 'buy', 1, 1, '{}', '{}', null, null, null)",
        "(1, now(), null, 'completed', 'hold', 1, 1, '{}', '{}', null, null, null)",
    ],
)
def test_run_status_and_side_are_checked(pg_cluster: PgDatabase, values: str) -> None:
    conn = pg_cluster.connect(COLLECTOR_ROLE)

    with pytest.raises(errors.CheckViolation):
        conn.execute(sql.SQL("insert into slipstream.runs values {}").format(sql.SQL(values)))


@pytest.mark.parametrize("digest", ["", "A" * 64, "a" * 63, "a" * 65, "g" * 64])
def test_sha256_must_be_64_lowercase_hex(pg_cluster: PgDatabase, digest: str) -> None:
    conn = pg_cluster.connect(COLLECTOR_ROLE)
    conn.execute(INSERTS["runs"][0])

    with pytest.raises(errors.CheckViolation):
        conn.execute(
            "insert into slipstream.recordings values (1, 1, 'a/b.gz', %s, now())", (digest,)
        )


@pytest.mark.parametrize(
    "rel_path",
    ["", "/home/alice/rec.gz", "a/../b.gz", "../b.gz", "a/..", "C:\\Users\\alice\\r.gz", "a\\b.gz"],
)
def test_a_recording_path_must_be_relative(pg_cluster: PgDatabase, rel_path: str) -> None:
    conn = pg_cluster.connect(COLLECTOR_ROLE)
    conn.execute(INSERTS["runs"][0])

    with pytest.raises(errors.CheckViolation):
        conn.execute(
            "insert into slipstream.recordings values (1, 1, %s, %s, now())", (rel_path, SHA)
        )


@pytest.mark.parametrize("rel_path", ["2026-09-27/10-0p01.jsonl.gz", "a/b..c/d.gz", "x.gz"])
def test_ordinary_relative_recording_paths_are_accepted(
    pg_cluster: PgDatabase, rel_path: str
) -> None:
    conn = pg_cluster.connect(COLLECTOR_ROLE)
    conn.execute(INSERTS["runs"][0])

    conn.execute("insert into slipstream.recordings values (1, 1, %s, %s, now())", (rel_path, SHA))


def test_a_run_can_be_archived_only_once(pg_cluster: PgDatabase) -> None:
    conn = pg_cluster.connect(COLLECTOR_ROLE)
    _seed(conn)

    with pytest.raises(errors.UniqueViolation):
        conn.execute(
            "insert into slipstream.recording_uploads values (2, 1, 1, 'recordings/b.gz', %s, 1)",
            (SHA,),
        )


def test_the_helpers_log_in_over_the_socket_with_scram(pg_cluster: PgDatabase) -> None:
    admin = pg_cluster.connect()
    admin.execute(
        sql.SQL("alter role {} password {}").format(
            sql.Identifier(COLLECTOR_ROLE), sql.Literal("only-a-test-value-0123456789")
        )
    )
    pg_cluster.set_hba(["local all postgres trust", "local all all scram-sha-256"])

    conn = pg_cluster.connect_as(COLLECTOR_ROLE, "only-a-test-value-0123456789")
    assert conn.execute("select current_user").fetchone() == (COLLECTOR_ROLE,)
    with pytest.raises(errors.OperationalError):
        pg_cluster.connect_as(COLLECTOR_ROLE, "wrong")
