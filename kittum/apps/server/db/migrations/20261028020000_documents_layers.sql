-- migrate:up

-- Documents, acquisitions, parsed layers, anchors, and anchor mappings (DESIGN.md, "Provenance
-- that survives re-parsing"). PDF bytes and layer JSON live in the artifact store; rows hold
-- their SHA-256. Layers and anchors never change once written.

create table document (
    id uuid primary key,
    artifact_hash char(64) not null unique,
    media_type text not null,
    byte_size bigint not null check (byte_size > 0),
    page_count integer check (page_count >= 0),
    created_at timestamptz not null default now()
);

create table acquisition (
    id uuid primary key,
    project_id uuid not null references project (id),
    document_id uuid not null references document (id),
    source text not null,
    url text,
    licence text,
    terms_url text,
    acquired_at timestamptz not null,
    event_id uuid not null
);

create index acquisition_by_document on acquisition (document_id);

create table layer (
    id uuid primary key,
    document_id uuid not null references document (id),
    recipe_hash char(64) not null,
    recipe jsonb not null,
    output_hash char(64) not null,
    text_hash char(64) not null,
    page_count integer not null,
    created_at timestamptz not null default now(),
    unique (document_id, recipe_hash)
);

create table anchor (
    id uuid primary key,
    layer_id uuid not null references layer (id),
    document_id uuid not null references document (id),
    quote jsonb not null,
    text_hash char(64) not null,
    start_offset integer not null check (start_offset >= 0),
    end_offset integer not null check (end_offset > start_offset),
    boxes jsonb not null,
    cells jsonb not null,
    created_at timestamptz not null default now()
);

create index anchor_by_layer on anchor (layer_id);

create table anchor_mapping (
    id uuid primary key,
    old_anchor_id uuid not null references anchor (id),
    layer_id uuid not null references layer (id),
    new_anchor_id uuid references anchor (id),
    status text not null
        check (status in ('exact', 'candidate', 'ambiguous', 'unresolved', 'reviewer_confirmed')),
    method text not null,
    method_version text not null,
    alternative_anchor_ids uuid[] not null default '{}',
    reviewed_event_id uuid,
    created_at timestamptz not null default now(),
    unique (old_anchor_id, layer_id, method_version),
    check ((status in ('exact', 'candidate')) = (new_anchor_id is not null))
);

create table page_render (
    document_id uuid not null references document (id),
    page integer not null check (page > 0),
    scale numeric(4, 2) not null check (scale > 0),
    renderer text not null,
    artifact_hash char(64) not null,
    primary key (document_id, page, scale, renderer)
);

create function documents_immutable() returns trigger
language plpgsql as $$
begin
    raise exception '% rows are immutable (%)', tg_table_name, tg_op;
end;
$$;

create trigger layer_immutable before update or delete on layer
    for each row execute function documents_immutable();

create trigger anchor_immutable before update or delete on anchor
    for each row execute function documents_immutable();

-- migrate:down

drop trigger anchor_immutable on anchor;
drop trigger layer_immutable on layer;
drop function documents_immutable;
drop table page_render;
drop table anchor_mapping;
drop table anchor;
drop table layer;
drop table acquisition;
drop table document;
