-- migrate:up
-- Requests no longer carry a confidentiality tier (DESIGN.md, "Data protection and
-- confidentiality"); stored agent configs lose the field so they still load.
update extraction_run set agent = agent - 'confidentiality'
where agent ? 'confidentiality';

-- migrate:down
-- Restores the default tier; a run that asked for another one comes back as local_only.
update extraction_run set agent = agent || '{"confidentiality": "local_only"}'::jsonb
where agent is not null;
