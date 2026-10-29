-- migrate:up

-- An item taken out of screening because the full text shown was not the record's report (a
-- reviewer's "wrong document"). Its assignments and decisions stay on record, but no queue,
-- count, outcome, or report includes it any more. A withdrawal is not an exclusion.
alter table screening_item
    add column withdrawn_reason text check (withdrawn_reason in ('wrong_document')),
    add column withdrawn_event_id uuid,
    add check ((withdrawn_reason is null) = (withdrawn_event_id is null));

-- As before, without withdrawn items.
create or replace function screening_reviewer_queue(p_stage_id uuid, p_reviewer uuid) returns table (
    assignment_id uuid,
    stage text,
    scope_id text,
    title text,
    text text,
    facts jsonb,
    etag text,
    state text,
    own_reveal jsonb,
    "position" bigint
)
language sql stable as $$
    select a.id, s.stage, s.scope_id, i.title, i.text, i.facts, a.etag, a.state,
        case when a.state = 'awaiting_final' then d.reveal end,
        row_number() over (
            order by case when s.mode = 'M3' then r.rank else 0 end, a.sort_key, a.id
        )
    from screening_assignment a
    join screening_stage s on s.id = a.stage_id
    join screening_item i on i.stage_id = a.stage_id and i.item_ref = a.item_ref
    join screening_ai_reading r on r.stage_id = a.stage_id and r.item_ref = a.item_ref
    left join screening_decision d on d.id = a.provisional_decision_id
    where a.stage_id = p_stage_id and a.reviewer_id = p_reviewer
        and i.withdrawn_event_id is null
$$;

create or replace function screening_reviewer_progress(p_stage_id uuid, p_reviewer uuid)
returns table (assigned bigint, decided bigint)
language sql stable as $$
    select count(*), count(*) filter (where a.state = 'decided')
    from screening_assignment a
    join screening_item i on i.stage_id = a.stage_id and i.item_ref = a.item_ref
    where a.stage_id = p_stage_id and a.reviewer_id = p_reviewer
        and i.withdrawn_event_id is null
$$;

-- migrate:down

create or replace function screening_reviewer_progress(p_stage_id uuid, p_reviewer uuid)
returns table (assigned bigint, decided bigint)
language sql stable as $$
    select count(*), count(*) filter (where a.state = 'decided')
    from screening_assignment a
    where a.stage_id = p_stage_id and a.reviewer_id = p_reviewer
$$;

create or replace function screening_reviewer_queue(p_stage_id uuid, p_reviewer uuid) returns table (
    assignment_id uuid,
    stage text,
    scope_id text,
    title text,
    text text,
    facts jsonb,
    etag text,
    state text,
    own_reveal jsonb,
    "position" bigint
)
language sql stable as $$
    select a.id, s.stage, s.scope_id, i.title, i.text, i.facts, a.etag, a.state,
        case when a.state = 'awaiting_final' then d.reveal end,
        row_number() over (
            order by case when s.mode = 'M3' then r.rank else 0 end, a.sort_key, a.id
        )
    from screening_assignment a
    join screening_stage s on s.id = a.stage_id
    join screening_item i on i.stage_id = a.stage_id and i.item_ref = a.item_ref
    join screening_ai_reading r on r.stage_id = a.stage_id and r.item_ref = a.item_ref
    left join screening_decision d on d.id = a.provisional_decision_id
    where a.stage_id = p_stage_id and a.reviewer_id = p_reviewer
$$;

alter table screening_item
    drop column withdrawn_event_id,
    drop column withdrawn_reason;
