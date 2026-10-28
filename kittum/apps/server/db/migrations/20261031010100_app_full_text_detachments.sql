-- migrate:up

-- A full text taken off its record because it is not the record's report (a reviewer's "wrong
-- document"). The record has no full text again and can get another one. The detached document
-- stays stored, and the ledger event names it. `command_id` makes a retried request a no-op.
create table full_text_detachment (
    id uuid primary key,
    project_id uuid not null references project (id),
    item_ref text not null,
    document_id uuid not null references document (id),
    reason text not null check (reason in ('wrong_document')),
    note text,
    actor_id uuid not null references actor (id),
    command_id uuid not null unique,
    event_id uuid not null,
    recorded_at timestamptz not null
);

create index full_text_detachment_item on full_text_detachment (project_id, item_ref);

-- migrate:down

drop table full_text_detachment;
