-- migrate:up

-- A `Retry-After` or an exhausted rate-limit window pauses a source for every worker, not only
-- the one that saw the header.

create table gateway_cooldown (
    source text primary key,
    until timestamptz not null
);

-- migrate:down

drop table gateway_cooldown;
