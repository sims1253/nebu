-- migrate:up

-- Gateway bookkeeping (WP-04): every outbound attempt, the content-addressed response cache,
-- and the daily request budgets. The rate-limit buckets themselves are created by pyrate-limiter
-- on first use, one table per source, so their shape stays that library's business.

create table gateway_attempt (
    id uuid primary key default uuidv7(),
    source text not null,
    request_hash char(64) not null,
    attempt integer not null check (attempt > 0),
    status integer check (status between 100 and 599),
    duration_ms integer not null check (duration_ms >= 0),
    error text,
    recorded_at timestamptz not null
);

create index gateway_attempt_by_source on gateway_attempt (source, recorded_at desc);
create index gateway_attempt_by_request on gateway_attempt (request_hash);

-- The body lives in the artifact store under `body_hash`; this table only indexes it.
create table gateway_cache (
    request_hash char(64) primary key,
    source text not null,
    status integer not null check (status between 100 and 599),
    headers jsonb not null,
    body_hash char(64) not null,
    stored_at timestamptz not null
);

create table gateway_budget (
    source text not null,
    day date not null,
    used integer not null check (used >= 0),
    primary key (source, day)
);

-- migrate:down

drop table gateway_budget;
drop table gateway_cache;
drop table gateway_attempt;
