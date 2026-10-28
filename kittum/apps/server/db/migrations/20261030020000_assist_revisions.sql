-- migrate:up

-- The protocol assistant's revisions (kittum.assist). Every draft a model writes, every
-- suggestion it makes, every change a lead accepts, edits, or restores is a new row; none is ever
-- changed or deleted, so stepping back is reading an older row. What a model wrote is read from
-- this history by value (kittum.assist), never stored beside a revision. Every document compiles: an
-- answer that does not is never stored as a revision (its attempts are in the ledger event).

create table assist_revision (
    id uuid primary key default uuidv7(),
    project_id uuid not null references project (id),
    seq integer not null check (seq > 0),
    parent_id uuid references assist_revision (id),
    kind text not null check (kind in ('draft', 'suggestion', 'accepted', 'edit', 'restore')),
    document json not null,  -- json, not jsonb: the document exactly as compiled, keys in order
    content_hash char(64) not null,
    actor_id uuid not null references actor (id),
    model jsonb,
    suggestion jsonb,
    accepted jsonb,
    note text,
    created_at timestamptz not null default now(),
    unique (project_id, seq),
    check ((kind in ('draft', 'suggestion')) = (model is not null)),
    check ((kind = 'suggestion') = (suggestion is not null)),
    check ((kind = 'accepted') = (accepted is not null))
);

create index assist_revision_by_hash on assist_revision (project_id, content_hash);

create function assist_revision_is_append_only() returns trigger
language plpgsql as $$
begin
    raise exception 'assist_revision % is append-only', old.id;
end;
$$;

create trigger assist_revision_append_only
    before update or delete on assist_revision
    for each row execute function assist_revision_is_append_only();

-- migrate:down

drop trigger assist_revision_append_only on assist_revision;
drop function assist_revision_is_append_only;
drop table assist_revision;
