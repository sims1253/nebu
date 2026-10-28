# Work packages

Each package is one module or one cross-cutting piece, sized for one agent and one branch. Every
interface exists as a stub, and the shared fakes exist, so all packages can start now. "Needs"
lists what a package calls at integration time, not what it waits for.

The two-week spike in [DESIGN.md](../DESIGN.md#two-week-spike) picks from these in order: WP-01,
02, 03, 05, 08, 13, then 09, 10, 11, 14.

## Status

| WP | Module | Status |
| --- | --- | --- |
| 00 | Scaffold: `core`, layout, CI, compose, contracts pipeline | Done |
| 01 | `ledger` | Done: Postgres and SQLite stores, checkpoints, `seal`, `kittum verify`; TSA and WORM copies open |
| 02 | `runs` | Done: job queue, worker, receipts, decider helper, fakes; run manifests and `diff` open |
| 03 | `protocol`, `profiles` | Done: schema, profiles, compile, diff, rendering, persistence, routes |
| 04 | `gateway` | Done: httpx transport, policies, shared buckets, budgets, cache, attempts; AppContext wiring open |
| 05 | `search` | Done |
| 06 | `enrich` | Clients, stored enrichment, and full-text waterfall done; local tagging open |
| 07 | `corpus` | Types done; open |
| 08 | `judge` and `models` | Types and recorded backend done; open |
| 09 | `screening` | Types done; open |
| 10 | `documents` | Types done; open |
| 11 | `extraction` | Types done; open |
| 12 | `appraisal` | Types done; open |
| 13 | `stats` | Done: intervals, stopping, capture-recapture, conformal threshold, agreement |
| 14 | `report` | Types done; open |
| 15 | `api` | Health route done; open |
| 16 | Workbench | Shell done; open |
| 17 | Evaluation harness | Open |

## WP-01 Ledger

Module `kittum.ledger`. Needs: `core`.

- A Postgres store on the foundation tables: append with per-project serialization (advisory lock
  or `select ... for update` on the chain head), events, and verify.
- A SQLite store with the same behaviour, for the handover bundle and tests.
- Signed checkpoints (Ed25519 with `cryptography`), with the public key ID recorded.
- `seal(cutoff_seq)`: the bundle manifest and the offline `verify` command.
- Acceptance: both stores pass one shared test suite. Verify catches a changed payload, a removed
  event, a reordered event, and a cut tail after a checkpoint.

**Done.** `PostgresLedger`, `SqliteLedger`, and `InMemoryLedger` share one implementation of the
chain rules (`ledger/store.py`); `tests/ledger/test_stores.py` runs one suite over all three.

- Postgres appends take `pg_advisory_xact_lock` on the project, held until the caller commits.
- `checkpoint(tx, project_id, signer)` verifies the chain, signs the head with Ed25519, and stores
  the row. `verify(tx, project_id, checkpoint=..., key=...)` catches a cut tail and a chain
  rewritten from some point on, which the chain alone cannot. `VerifyReport.problem` names what
  failed. The key comes from `KITTUM_CHECKPOINT_KEY` (`Settings.checkpoint_key_path`); the key ID
  is the SHA-256 of the raw public key.
- `seal(tx, project_id, cutoff_seq, store, out_dir, signer=..., actor_id=...)` writes
  `kittum.sqlite` (ledger through the cutoff, checkpoints, artifact index), `artifacts/`, and a
  signed `manifest.json`, and records a `ledger.bundle_sealed` event. Modules add tables and
  artifacts with `register_exporter(exporter)`; a withheld artifact is indexed by hash with a
  reason, and a withhold wins over an include.
- `kittum verify <bundle> [--public-key pem]` checks offline: the manifest signature, the
  checkpoint signature, chain continuity to the cutoff, file hashes, unlisted files, and the
  artifact index. Without `--public-key` it uses the bundle's own key and says the key is not
  pinned.

Left open:

- RFC 3161 timestamps over checkpoints (`tsa_token_hash` stays empty) and copies of checkpoints
  outside the database (WORM storage). Until then, pass `verify` a checkpoint kept elsewhere.
- Scheduled checkpoints at stage gates and daily, once `runs` and the stage gates exist.
- The `checkpoint` table has no append-only trigger, because it is in the foundation migration.
  A trusted external copy covers this; a trigger would be a small foundation change.
- Bundle contents from DESIGN.md beyond the ledger: `dossier/`, `registry/`, count
  reconciliation, and the "every accepted value has an anchor" check belong to the exporters of
  `report`, `extraction`, and `documents`.

## WP-02 Runs and jobs

Module `kittum.runs`. Needs: `ledger`.

- `JobQueue` on the `job` table with `for update skip locked`, leases, heartbeats, retries with
  backoff, and dead-lettering. Unique `stage_key` prevents double publication.
- Command receipts: a retried command returns the stored receipt and writes nothing.
- The decider pattern: a helper that commits events, rows, receipt, and jobs in one transaction.
- A worker loop with graceful shutdown, and an in-memory queue fake.
- Acceptance: two workers never run the same job; a crashed worker's job is reclaimed after its
  lease; a replayed command is a no-op.

**Done.** `tests/runs/test_queue.py` runs one suite over `PostgresJobQueue` and the
`InMemoryJobQueue` fake.

- `JobQueue` methods take the caller's `tx`. A claimed `Job` is the lease token: `heartbeat`,
  `complete`, and `fail` check owner and attempt and raise `LeaseLost` otherwise. Expired leases
  are reclaimed; an expired lease on the last attempt dead-letters the job.
- `fail` requeues with exponential backoff and jitter (`RetryPolicy`), and marks the job `dead`
  after `max_attempts`; `retryable=False` (or `PermanentJobError` in a handler) marks it `failed`.
- `enqueue` with an existing `stage_key` returns the existing job, or raises `StageKeyConflict` if
  the inputs differ.
- `Commands(ledger, queue, receipts).execute(tx, command, decide, load=..., apply=...)` locks the
  command ID, returns a stored receipt unchanged, or decides, appends events, applies rows,
  enqueues jobs, and saves the receipt in one transaction.
- `Worker(db, queue, handlers, name=...)` claims, heartbeats in a thread, and completes the job in
  the same transaction as the handler's `JobOutput.publish`. `stop()` (or `stop_on_signals()`)
  lets the current job finish. Fakes: `InMemoryJobQueue`, `InMemoryReceipts`, `NullTransactions`.

Left open:

- Run manifests, cache keys, branch runs, and `runs.diff` (DESIGN.md "Runs and reproducibility").
- A `kittum worker` entry point that builds handlers from the modules.
- Receipts do not store a command hash, so a reused command ID with a different payload but the
  same type returns the old receipt. Detecting that needs a column on `command_receipt`.

## WP-03 Protocol

Module `kittum.protocol` (domain-neutral core) and `kittum.profiles` (review-community profiles).
Needs: `core`, `db`, `ledger`.

Done:

- A generic schema: review questions (each a scope) with PICO, comparators (optionally
  alternatives), and outcome IDs; outcomes with a category; effect modifiers; search topics with
  criteria, source plan, reference set, screening mode with audit and stopping plans, and a
  freshness policy. All human text is localized (`{en: ..., de: ...}`) with a required
  `default_language`.
- Profiles: `register_profile(Profile)` adds extension schemas (protocol, question, comparator,
  outcome, topic, criterion), vocabularies (`outcome_category`, `topic_kind`), rules, labels, and
  default effect modifiers. The core never imports a profile; `api.create_app` calls
  `kittum.profiles.load_profiles()`.
- `kittum.profiles.gba_amnog`: dossier and Anwendungsgebiete, Teilpopulationen with zVT
  (comparators as alternatives, "nach ärztlicher Maßgabe", off-label options), JCA PICOs as linked
  questions, the four AMNOG endpoint categories, Modul 4 search kinds, the three-month search age,
  the Modul 4 source minimum, German as the default language.
- `compile(source)` from YAML, JSON, or a mapping: every issue with its path, duplicate YAML keys
  with line numbers, cross-checks (unique IDs, scopes exist, every topic and scope decidable at
  title and abstract, mode rules, reference identifiers), and a content hash over RFC 8785 JSON.
- `diff(a, b)`: added, removed, and changed criteria and scopes, mode changes, and field-level
  changes keyed by ID.
- `render_question` (reviewer question with answer keys, profile slot labels) and
  `render_prompt` (judge fragment asking for a verbatim quote, languages in stable order).
- Persistence in `protocol_version` (migration `20261028140000_protocol_versions.sql`):
  `save_draft` (idempotent per content hash), `publish` (human approver, `protocol.published`
  ledger event, reason required for a mode change, then frozen by trigger), `get_version`,
  `list_versions`, `latest_published`. `testing.InMemoryProtocolStore` is the fake.
- Routes: `POST /api/protocol/validate`, `POST /api/projects/{id}/protocol-versions`,
  `POST .../{version_id}/publish`, `GET .../protocol-versions`, `GET .../{version_id}`.
- Examples: `examples/protocols/capivasertib.yaml` (G-BA D-1110, `gba_amnog`) and
  `examples/protocols/nicotine-replacement.yaml` (no profile).

Left:

- Swap the test-only `SqlLedger` (tests/protocol/support.py) for the WP-01 Postgres ledger, and
  give `AppContext.ledger` a real default once it exists.
- Approver identity comes from the request body until the API has authentication (WP-15).
- A profile for EU JCA dossiers as a report target, if JCA needs more than linked questions.

## WP-04 Gateway

Module `kittum.gateway`. Needs: `core`.

- Done: `HttpxTransport` on httpx2, with timeouts, redirects, and a `User-Agent` naming the
  Kittum version and the configured contact email.
- Done: default policies for PubMed, ClinicalTrials.gov, Europe PMC, OpenAlex, Crossref,
  Unpaywall, and PubTator, 10% under docs/sources.md (`default_policies()`); a caller overrides
  any of them by passing its own mapping. A contact email moves Crossref to the polite pool and
  an NCBI key raises PubMed's rate.
- Done: `SourceGateway` with a pyrate-limiter bucket per source. The Postgres bucket shares the
  limit across processes; `buckets="memory"` is for tests. One table per source, named
  `ratelimit___gateway_<source>`. A per-source concurrency cap is enforced with Postgres advisory
  slots held for each send, so it spans workers too.
- Done: daily budgets in `gateway_budget`, consumed per attempt, so a new gateway instance
  continues where the previous one stopped. Exhaustion raises `SourceBudgetExhausted`; the
  caller decides whether to wait for the next day or fail the job.
- Done: `Retry-After` (seconds or HTTP date) and `X-RateLimit-Remaining`/`X-RateLimit-Reset`
  pause a source before the next attempt, with the full deadline in `gateway_cooldown`, so every
  worker waits. Retries with stamina on 429, 502, 503, and 504, five attempts, exponential
  backoff with full jitter.
- Done: content-addressed cache: the body goes to `ArtifactStore`, `gateway_cache` maps the
  canonical request hash to status, headers, and body hash. `use_cache=False` skips the read but
  stores the fresh response for later replays. Every transport attempt lands in `gateway_attempt`.
- Done: `FakeTransport` scripts a sequence of responses (`kittum.gateway.testing`).
- Left: entry points do not build a gateway yet. `AppContext` needs a `gateway` field (an
  interface change outside this module) before search and enrichment can call it.
- Left: PubMed's and OpenAlex's API keys are not attached to requests here; adapters pass them
  as request parameters. Retry-After on 503 is honoured, but there is no circuit breaker or
  source-health memory beyond the cooldown deadlines.
- Acceptance met: tests against real Postgres with `FakeTransport` show a 429 then 200 with both
  attempts recorded, five failures with a clear error, a cache hit with no transport call,
  independent buckets for two sources, a budget that survives a new gateway, the concurrency cap,
  and cooldowns shared across gateway instances.

## WP-05 Search

Module `kittum.search`. Needs: `gateway`, `protocol`, `ledger`.

- PubMed E-utilities: ESearch with history, date windows under the 10,000 cap, EFetch, query
  translation capture, structured abstracts with labels kept, registry IDs from `DataBankList` and abstract.
- ClinicalTrials.gov API v2 with Essie syntax, pagination across multiple pages, mapping to NCT ID, status, and phase.
- File imports: RIS (Generic, Cochrane Library CENTRAL, Embase) and CSV (CTIS, EU-CTR).
- Search execution and source occurrence persistence in Postgres with audit ledger event (`search.executed`).
- HTTP API routes (`/api/projects/{project_id}/search/*`) registered and contract generated.
- `FakeSource` in `search/testing.py`.
- Acceptance: recorded real responses in `tests/search/fixtures/` reproduce counts, query translation, and PRISMA-S fields; tests pass end-to-end without network.
- `run` and `import_file` take the caller's ledger, actor, and artifact store. Occurrence IDs are
  assigned at persistence; every raw hash must be in the store before rows are written; the
  `search.executed` payload is built once (`prisma_s_payload`). Routes take `actor_id` in the body
  and answer 503 without a ledger or gateway.
- Left: take the actor from the session once auth exists; run live searches as a job (the HTTP calls
  currently happen inside the route's transaction); map `PubmedBookArticle` records (today they are
  skipped, so they show only as reported > retrieved).

## WP-06 Enrichment

Module `kittum.enrich`. Needs: `gateway`, `search`, `models`.

- Done: OpenAlex (works by DOI and PMID in OR batches, inverted-index abstracts, OA locations),
  Crossref (`update-to`, `updated-by`, and `relation` notices, `clinical-trial-number`, polite
  pool), Europe PMC (batched ID search, `synonym=FALSE`, full-text availability), Unpaywall (best
  OA location and licence), and PubTator 3 (BioC JSON entities re-anchored in the title plus
  abstract).
- Done: "first healthy source" per field. A source that errors or is out of budget is recorded in
  `Enrichment.failures` and the next one answers; `FieldSource` records which source supplied each
  field. Identifier lookups batch where the API allows.
- Done: `enrich(tx, project_id, records, gateway)` writes one `record_enrichment` row per
  occurrence, replacing an earlier row for the same occurrence. `EnrichmentConfig` carries the
  contact email and OpenAlex key that entry points map from `Settings`.
- Done: `full_text_candidates` lists Europe PMC XML and PDF, then Unpaywall, then OpenAlex file
  URLs, each with its licence.
- Done: recorded real responses in `tests/enrich/fixtures/`, including a retracted paper whose
  Crossref notice DOI is flagged.
- Open: local entity tagging with the OpenMed GLiNER models, for text that must stay local.

## WP-07 Corpus

Module `kittum.corpus`. Needs: `search`, `enrich`, `ledger`.

- Dedupe rules (DOI, PMID, registry ID, normalized title, year) that keep every occurrence.
- Link proposals for report and study with evidence strings.
- Identity revisions written as ledger events; PRISMA counts from a frozen revision.
- Acceptance: a fixture with duplicates, a multi-report study, and a conference abstract.

## WP-08 Judge and models

Modules `kittum.judge` and `kittum.models`. Needs: `protocol`, `core`.

- An OpenAI-compatible backend for vLLM and Ollama with JSON Schema-constrained output; the model
  recipe registry with the licence allowlist; the confidentiality check.
- `judge`: the prompt per criterion set, decoding, `find_quote` verification, keyword and negation
  checks, downgrade to `unclear`, raw output capture.
- Gold-data metrics: per-judge recall and specificity, the double-fault rate, run-to-run kappa.
- Acceptance: recorded completions cover verified, unverified, paraphrased, and negated quotes.

## WP-09 Screening

Module `kittum.screening`. Needs: `protocol`, `corpus`, `judge`, `ledger`, `runs`, `stats`.

- Assignments per mode (M1 to M3), blinding enforced in queries, reveal-after-commit with
  provisional and final decisions, sentinels and audit samples hidden from reviewers, conflicts,
  adjudication.
- Acceptance: a reviewer can never read the other reviewer's decision or the AI verdict before
  committing; tests prove it at the service boundary.

## WP-10 Documents

Module `kittum.documents`. Needs: `gateway`, `ledger`.

- Page triage with pdf-inspector; parsers for PyMuPDF, pypdfium2, and Docling behind `Parser`;
  OCRmyPDF for scans; NFC canonical text with word boxes in PDF points.
- Layer storage by content hash; `resolve` with exact, candidate, ambiguous, and unresolved.
- The full-text waterfall (docs/sources.md).
- Acceptance: fixture PDFs with multi-column text and a merged-header table; anchors survive a
  re-parse with a different engine.

## WP-11 Extraction

Module `kittum.extraction`. Needs: `documents`, `protocol`, `models`.

- The Janus adapter: layer to Janus `DocumentEvidence`, a specification per endpoint schema.
- The PydanticAI agent with read-only tools and a call budget.
- Validators (events at most N, arms sum, CI brackets the estimate, percentages recompute) and
  reconciliation between paths.
- Acceptance: fixture tables give the same values on both paths; validators catch planted errors.

## WP-12 Appraisal

Module `kittum.appraisal`. Needs: `documents`, `extraction`.

- Instruments as data (RoB 2, ROBINS-I V2, plus profile-registered forms); quote assembly per
  question.

## WP-13 Statistics

Module `kittum.stats`. Needs: nothing.

- Every function in the module, with scipy where it helps. Done.
- `clopper_pearson` defaults to one-sided bounds, as Kittum states every bound: 10 of 10 gives
  a lower bound of 0.741, and the two-sided 0.692 figure remains available with `sided="two"`.
- `elusion_estimate` is a stratified Horvitz-Thompson total with simultaneous one-sided exact
  hypergeometric bounds (Bonferroni over strata, exact on census strata). Sampling is without
  replacement, so the bounds invert the hypergeometric tail rather than a binomial one.
- `stop_test` implements Callaghan and Müller-Hansen (2020) with the suffix-minimum p-value from
  the reference implementation. Pass `order` for the exact reference result; without it the
  test uses the whole sequence and is conservative.
- `capture_recapture` uses Chapman for two sources with an exact hypergeometric overlap
  inversion (the upper end is unbounded when the overlap is zero), and a Poisson log-linear
  model without a three-way interaction for three sources. Zero observed cells make that model
  non-estimable and raise a `ValueError` naming the cell.
- `conformal_threshold` is a split-conformal order statistic with a binomial coverage guarantee;
  `recall_needed` gives its calibration size for zero misses.
- `cohen_kappa`, `fleiss_kappa`, and `pabak` cover two raters and three or more runs.
- Acceptance: tests against published worked examples, including 10 of 10 giving a 0.741
  one-sided (0.692 two-sided) lower bound, the 119-record case from DESIGN.md, the buscarpy
  stopping reference, and seeded simulations for every interval. Done.
- Open: the evaluation harness (WP-17) still has to pool these bounds across topics.

## WP-14 Reports

Module `kittum.report`. Needs: most modules.

- The core targets: PRISMA 2020 flow per scope, RIS exports, the handover bundle, and the AI
  disclosure. Profile targets register through `report.register_target`; the `gba_amnog`
  targets are listed in docs/profiles/gba-amnog.md.
- Acceptance: golden files for a small fixture project.

## WP-15 API

Module `kittum.api`. Needs: the modules it exposes.

- Routes for the workbench screens, thin over module services. Regenerate contracts after each
  change.

## WP-16 Workbench

`apps/web`. Needs: WP-15.

- Screens from DESIGN.md "Reviewer workbench", starting with the title and abstract queue and its
  keyboard flow. Base UI for components, Legend List for queues, server-rendered PDF pages with
  word-box text layers for full text.

## WP-17 Evaluation harness

Not a runtime module; lives in `apps/server/eval/`.

- Loaders for SYNERGY and the gold-data tiers G1 to G3 (DESIGN.md "Evaluation"), a runner that
  scores judges and extraction, and a report.
