-- Slipstream cloud archive: the Supabase (Postgres) schema.
-- Design: docs/superpowers/specs/2026-09-28-cloud-archive-design.md, sections 5.1 and 5.2.
--
-- Paste this whole file once into the Supabase SQL editor and run it. It contains no secrets
-- and sets no password: the collector's password is added afterwards with the ALTER ROLE line
-- printed by `python -m slipstream.cloud scram-verifier`.
--
-- It is one transaction. Running it a second time fails at `create schema` and changes nothing.
--
-- What it creates:
--   * schema `slipstream`, which is not exposed through the Data API
--   * eight append-only tables (UPDATE, DELETE and TRUNCATE raise 'append-only', for every role,
--     including the owner and the dashboard's table editor)
--   * role `slipstream_collector`: login, SELECT and INSERT on those tables only, row level
--     security on, no BYPASSRLS, statement_timeout 60s
--   * no access at all for anon and authenticated

begin;

create schema slipstream;
revoke all on schema slipstream from public, anon, authenticated;

create function slipstream.append_only() returns trigger
language plpgsql
set search_path = ''
as $$
begin
    raise exception 'append-only';
end;
$$;
revoke all on function slipstream.append_only() from public, anon, authenticated;

-- Remote ids are the local ids: explicit bigint keys, no sequences.
create table slipstream.runs (
    id bigint primary key,
    started_at timestamptz not null,
    ended_at timestamptz,
    status text not null check (status in ('completed', 'failed', 'abandoned')),
    side text not null check (side in ('buy', 'sell')),
    qty double precision not null,
    duration_s integer not null,
    fees jsonb not null,
    venue_rules jsonb not null,
    git_commit text,
    error text,
    supersedes_id bigint references slipstream.runs (id)
);

create table slipstream.results (
    id bigint primary key,
    run_id bigint not null references slipstream.runs (id),
    algo text not null,
    state text not null,
    filled_qty double precision not null,
    filled_pct double precision not null,
    avg_price double precision not null,
    arrival_mid double precision not null,
    slippage_bps double precision not null,
    fee_bps double precision not null,
    all_in_bps double precision not null,
    immediate_cost_bps double precision not null,
    routing_gain_bps double precision,
    venue_costs jsonb not null,
    fills_count integer not null check (fills_count >= 0),
    halt_reason text
);

create table slipstream.engine_stats (
    id bigint primary key,
    run_id bigint not null references slipstream.runs (id),
    latency_p50_ns bigint not null,
    latency_p99_ns bigint not null,
    events bigint not null
);

-- rel_path is relative to $DATA/recordings; a local absolute path must never get here.
create table slipstream.recordings (
    id bigint primary key,
    run_id bigint not null references slipstream.runs (id),
    rel_path text not null check (
        rel_path <> ''
        and rel_path !~ '^/'
        and rel_path !~ '[\\:]'
        and rel_path !~ '(^|/)\.\.(/|$)'
    ),
    sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz not null
);

-- Derived at sync time from the local fills: the raw fills live in the Hugging Face dataset.
create table slipstream.fill_summaries (
    run_id bigint not null references slipstream.runs (id),
    algo text not null,
    venue text not null,
    fills_count integer not null check (fills_count >= 0),
    qty double precision not null,
    notional double precision not null,
    fees double precision not null,
    first_ts_ns bigint not null,
    last_ts_ns bigint not null,
    primary key (run_id, algo, venue)
);

create table slipstream.recording_deletions (
    id bigint primary key,
    run_id bigint not null references slipstream.runs (id),
    deleted_at timestamptz not null
);

create table slipstream.archive_days (
    id bigint primary key,
    day date not null unique,
    commit_oid text not null,
    manifest_path text not null,
    manifest_sha256 text not null check (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    fills_path text not null,
    fills_sha256 text not null check (fills_sha256 ~ '^[0-9a-f]{64}$'),
    fills_rows bigint not null check (fills_rows >= 0),
    uploaded_at timestamptz not null
);

create table slipstream.recording_uploads (
    id bigint primary key,
    run_id bigint not null unique references slipstream.runs (id),
    archive_day_id bigint not null references slipstream.archive_days (id),
    path_in_repo text not null,
    sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
    bytes bigint not null check (bytes >= 0)
);

-- No password: the role cannot log in until the ALTER ROLE line from `scram-verifier` is run.
create role slipstream_collector with login noinherit nocreatedb nocreaterole nobypassrls;
alter role slipstream_collector set statement_timeout = '60s';
grant usage on schema slipstream to slipstream_collector;

-- The same treatment for each of the eight tables.
do $$
declare
    t text;
begin
    foreach t in array array[
        'runs', 'results', 'engine_stats', 'recordings',
        'fill_summaries', 'recording_deletions', 'archive_days', 'recording_uploads'
    ] loop
        execute format('revoke all on slipstream.%I from public, anon, authenticated', t);
        execute format('grant select, insert on slipstream.%I to slipstream_collector', t);
        execute format('alter table slipstream.%I enable row level security', t);
        execute format(
            'create policy collector_read on slipstream.%I for select to slipstream_collector using (true)',
            t
        );
        execute format(
            'create policy collector_insert on slipstream.%I for insert to slipstream_collector with check (true)',
            t
        );
        execute format(
            'create trigger %I before update or delete on slipstream.%I '
            'for each row execute function slipstream.append_only()',
            t || '_append_only', t
        );
        execute format(
            'create trigger %I before truncate on slipstream.%I '
            'for each statement execute function slipstream.append_only()',
            t || '_no_truncate', t
        );
    end loop;
end;
$$;

commit;
