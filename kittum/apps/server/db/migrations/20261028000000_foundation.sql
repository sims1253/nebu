-- migrate:up

-- Foundation tables shared by every module: projects, actors, the audit ledger, command
-- receipts, and the job table. Module tables come in their own migrations.

create table project (
    id uuid primary key default uuidv7(),
    name text not null,
    policy_version text not null,
    created_at timestamptz not null default now()
);

create table actor (
    id uuid primary key default uuidv7(),
    identity_ref text not null unique,
    kind text not null check (kind in ('human', 'model', 'system')),
    organization text
);

create table audit_event (
    project_id uuid not null references project (id),
    seq bigint not null check (seq > 0),
    event_id uuid not null unique,
    type text not null,
    schema_version integer not null,
    actor_id uuid not null references actor (id),
    recorded_at timestamptz not null,
    payload jsonb not null,
    payload_hash char(64) not null,
    prev_hash char(64) not null,
    event_hash char(64) not null unique,
    primary key (project_id, seq)
);

create function audit_event_is_append_only() returns trigger
language plpgsql as $$
begin
    raise exception 'audit_event is append-only (% on seq %)', tg_op, old.seq;
end;
$$;

create trigger audit_event_append_only
    before update or delete on audit_event
    for each row execute function audit_event_is_append_only();

create trigger audit_event_no_truncate
    before truncate on audit_event
    for each statement execute function audit_event_is_append_only();

create table checkpoint (
    id uuid primary key default uuidv7(),
    project_id uuid not null references project (id),
    seq bigint not null,
    head_hash char(64) not null,
    signature bytea not null,
    key_id text not null,
    tsa_token_hash char(64),
    created_at timestamptz not null default now(),
    foreign key (project_id, seq) references audit_event (project_id, seq)
);

create table command_receipt (
    command_id uuid primary key,
    project_id uuid not null references project (id),
    type text not null,
    accepted_at timestamptz not null default now(),
    first_seq bigint,
    last_seq bigint
);

create table job (
    id uuid primary key default uuidv7(),
    project_id uuid not null references project (id),
    kind text not null,
    stage_key text not null unique,
    payload jsonb not null,
    status text not null default 'queued'
        check (status in ('queued', 'running', 'succeeded', 'failed', 'dead')),
    attempt integer not null default 0,
    run_after timestamptz not null default now(),
    lease_owner text,
    lease_until timestamptz,
    output_hash char(64),
    last_error text,
    created_at timestamptz not null default now()
);

create index job_claimable on job (kind, run_after) where status = 'queued';

-- migrate:down

drop table job;
drop table command_receipt;
drop table checkpoint;
drop trigger audit_event_no_truncate on audit_event;
drop trigger audit_event_append_only on audit_event;
drop table audit_event;
drop function audit_event_is_append_only;
drop table actor;
drop table project;
