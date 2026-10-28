-- migrate:up

-- The AI judge: its configurations and the verdicts it recorded (WP-08). A verdict is never
-- changed; a new model, prompt, or protocol gives a new cache key and a new row.

create table judge_config (
    id text primary key,
    model_recipe_id text not null,
    model_recipe jsonb not null,
    weights_hash char(64) not null,
    prompt_version text not null,
    prompt_hash char(64) not null,
    schema_hash char(64) not null,
    decoding jsonb not null,
    created_at timestamptz not null
);

create table verdict (
    id uuid primary key,
    project_id uuid not null references project (id),
    judge_config_id text not null references judge_config (id),
    cache_key char(64) not null,
    item_ref text not null,
    scope_id text not null,
    stage text not null check (stage in ('title_abstract', 'full_text')),
    input_hash char(64) not null,
    protocol_hash char(64) not null,
    rendered_prompt_hash char(64) not null,
    schema_hash char(64) not null,
    criteria jsonb not null,
    excludes boolean not null,
    flags text[] not null,
    model_identity text,
    raw_output_hash char(64) not null,
    attempts jsonb not null,
    created_at timestamptz not null,
    unique (project_id, cache_key)
);

create index verdict_item on verdict (project_id, item_ref, scope_id);

create function verdict_is_immutable() returns trigger
language plpgsql as $$
begin
    raise exception 'verdict rows are immutable (% on %)', tg_op, old.id;
end;
$$;

create trigger verdict_immutable
    before update on verdict
    for each row execute function verdict_is_immutable();

-- migrate:down

drop trigger verdict_immutable on verdict;
drop function verdict_is_immutable;
drop table verdict;
drop table judge_config;
