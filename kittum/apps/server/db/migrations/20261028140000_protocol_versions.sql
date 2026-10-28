-- migrate:up

-- Protocol versions (WP-03). A version is saved as a draft and frozen when published. The
-- document and its hash never change; publishing only fills in the approval.

create table protocol_version (
    id uuid primary key default uuidv7(),
    project_id uuid not null references project (id),
    parent_id uuid references protocol_version (id),
    version_label text not null,
    content_hash char(64) not null,
    document jsonb not null,
    status text not null default 'draft' check (status in ('draft', 'published')),
    created_at timestamptz not null default now(),
    published_at timestamptz,
    approved_by uuid references actor (id),
    approved_event_id uuid references audit_event (event_id),
    publish_reason text,
    unique (project_id, content_hash),
    check (
        (status = 'draft' and published_at is null and approved_by is null
            and approved_event_id is null)
        or (status = 'published' and published_at is not null and approved_by is not null
            and approved_event_id is not null)
    )
);

create index protocol_version_by_project on protocol_version (project_id, created_at);

create function protocol_version_is_frozen() returns trigger
language plpgsql as $$
begin
    if tg_op = 'DELETE' then
        if old.status = 'published' then
            raise exception 'protocol_version % is published and frozen', old.id;
        end if;
        return old;
    end if;
    if old.status = 'published' then
        raise exception 'protocol_version % is published and frozen', old.id;
    end if;
    if new.project_id <> old.project_id
        or new.parent_id is distinct from old.parent_id
        or new.version_label <> old.version_label
        or new.content_hash <> old.content_hash
        or new.document <> old.document
        or new.created_at <> old.created_at then
        raise exception 'protocol_version % content is immutable', old.id;
    end if;
    return new;
end;
$$;

create trigger protocol_version_frozen
    before update or delete on protocol_version
    for each row execute function protocol_version_is_frozen();

-- migrate:down

drop trigger protocol_version_frozen on protocol_version;
drop function protocol_version_is_frozen;
drop table protocol_version;
