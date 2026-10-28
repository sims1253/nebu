-- migrate:up

-- Who works on a project and in which role. A person can hold several roles; screening checks
-- the rules between them (an auditor never screens the same stage) when a stage opens.
create table project_member (
    project_id uuid not null references project (id),
    actor_id uuid not null references actor (id),
    role text not null check (role in ('lead', 'reviewer', 'auditor', 'adjudicator')),
    display_name text not null,
    event_id uuid not null,
    created_at timestamptz not null default now(),
    primary key (project_id, actor_id, role)
);

-- The full text attached to a record (a corpus report), by its screening item reference. One per
-- record; attaching another PDF later is a new document and needs a new full-text stage. A
-- sentinel's full text (a report with a known outcome, seeded into full-text queues) has an
-- item reference of its own, a title, and the outcome it should get.
create table full_text (
    project_id uuid not null references project (id),
    item_ref text not null,
    document_id uuid not null references document (id),
    layer_id uuid not null references layer (id),
    licence text,
    sentinel_title text,
    sentinel_outcome text check (sentinel_outcome in ('include', 'exclude')),
    check ((sentinel_title is null) = (sentinel_outcome is null)),
    event_id uuid not null,
    created_at timestamptz not null default now(),
    primary key (project_id, item_ref)
);

-- migrate:down
drop table full_text;
drop table project_member;
