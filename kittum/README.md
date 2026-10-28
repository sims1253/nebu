# Kittum

Kittum is a workbench for auditable, AI-assisted systematic literature reviews. It runs the review
from the protocol to the final reports. AI screens and extracts beside two human reviewers, and
every decision lands in a hash-chained ledger. The deliverable is the review output plus one
database file that holds the whole audit trail.

The core does not depend on any jurisdiction. The rules of a particular agency or jurisdiction come
from an optional [profile](DESIGN.md#profiles).

Status: a working review from the protocol to full-text screening, with the workbench on the
real API; extraction, appraisal, and reports are being built. The work is split into
[work packages](docs/work-packages.md). [AGENTS.md](AGENTS.md) explains how to build, test, and
work on it in parallel.

## Run Kittum

You need Docker, [uv](https://docs.astral.sh/uv/), Node 26 with pnpm, and, for live AI screening,
[Ollama](https://ollama.com) with `ollama pull qwen3.5:4b`. From `kittum/`:

```sh
# 1. Postgres 18, migrated
docker compose -f infra/compose.yaml up -d --wait postgres
docker compose -f infra/compose.yaml run --rm migrate

# 2. Install, and build the workbench
(cd apps/server && uv sync)
pnpm install && pnpm build

# 3. Settings: copy and adjust, then load them in each shell below
cp .env.example .env
cd apps/server && set -a && . ../../.env && set +a

# 4. A complete demo review: the nicotine replacement example protocol, a recorded PubMed search,
#    a CENTRAL export, a citation search, two CC BY trial PDFs, and title and abstract screening
#    with the AI judge's readings. Without a model server, replay the recorded answers:
uv run kittum demo seed
#    or judge live with the local model in KITTUM_LLM_ENDPOINTS (a few seconds per record on a
#    GPU, much longer on a CPU):
uv run kittum demo seed --live

# 5. The API and the workbench from one process, and a worker for the AI judge's jobs. Both use
#    the judge the demo was seeded with: add KITTUM_JUDGE_BACKEND=demo for a replayed demo.
uv run kittum serve            # http://127.0.0.1:8100
uv run kittum worker           # in a second shell
```

Open <http://127.0.0.1:8100>, choose the review, and choose who you are. There is no sign-in yet:
anyone who can reach the server can act as anyone, so run it only where every user is trusted.
Two reviewers can screen side by side in two browser tabs; each tab remembers its own person.
`?as=<actor ID>` on any workbench URL picks the person too, and `kittum demo seed` prints the IDs.

A walk through the demo:

1. **Sam Okafor** and **Jun Park** screen title and abstract (`i` include, `e` then `1` to `3`
   exclude on a criterion, `u` unclear, `Backspace` undo, `Space` next). The AI judge shows its
   reading only after a reviewer's own decision, and only when it disagrees.
2. **Rita Nunes**, the auditor, screens the random audit sample and, among it, the records both
   reviewers excluded while the model saw no failing criterion.
3. **Elena Weiss** adjudicates the human conflicts (`1`, `2` adopt a reviewer's decision); the AI's
   reading opens after she decides.
4. **Morgan Lee**, the methods lead, watches **Progress** (the PRISMA flow, kappa, the extra
   screens, the audit gauge, the ledger check), opens full-text screening there with `n`,
   confirms study links on **Studies** (`Enter`), and on **Reports** renders the PRISMA flow (`p`)
   and seals the handover bundle (`s`; needs `KITTUM_CHECKPOINT_KEY`), which `uv run kittum verify
   <path>` checks offline.
5. The reviewers screen the full texts: `]` jumps to a protocol term on the page, `Enter` adopts
   it as evidence, `y` `n` `?` give each criterion a verdict, `i` `e` `x` decide.

`uv run kittum ledger verify <project ID>` checks the project's hash chain.

For a review of your own, press `c` on the start page and set it up in the browser as its methods
lead: Setup walks through the steps in order. Add the people with their roles, write the protocol
from an example in `examples/protocols/` or your own file and publish it, import each database's
export (RIS, CENTRAL, Embase, PubMed XML, registry CSV) or run PubMed and ClinicalTrials.gov live,
build the corpus, open title and abstract screening with its sentinels
(`examples/sentinels/nicotine-replacement.yaml` for the example protocol), and fetch or upload the
full texts on Retrieval. `uv run kittum project --help` lists the same steps for the command line.

Building the corpus queues its enrichment, which `kittum worker` runs through the source gateway:
OpenAlex, Europe PMC, Crossref, Unpaywall, and PubTator 3 fill missing identifiers and abstracts,
list open-access locations, flag retractions and corrections, and tag drugs and diseases. A source
that fails is recorded and the others still answer. **Studies** shows the run per source and, with
`o` on a record, where each of its fields came from; `r` enriches again. Once records are included
at title and abstract, `o` on **Retrieval** has the worker walk the open-access waterfall for each
report without a full text (Europe PMC, Unpaywall, OpenAlex, then the publisher's openly licensed
PDF listed in Crossref), lists every attempt under its report, and stores each PDF it gets with its
licence; upload the rest by hand with `u`. Set `KITTUM_CONTACT_EMAIL` first: Crossref's polite
pool and Unpaywall need it, and without it enrichment skips Unpaywall. `KITTUM_OPENALEX_API_KEY`
and `KITTUM_NCBI_API_KEY` are optional and raise those sources' limits (see `.env.example`).

To judge with a hosted OpenAI-compatible API instead, add it as a recipe of your own in a file
under `.kittum/` (git ignores it), point `KITTUM_RECIPES_FILE` at it, and name the environment
variable that holds the key in `KITTUM_LLM_API_KEYS`. Kittum sends the key as a bearer token and
never stores it; the AI disclosure records the recipe, `serving: hosted`, and the endpoint. Which
records may go to a hosted model is your decision. For example, in
`apps/server/.kittum/recipes.yaml`:

```yaml
recipes:
  - id: example-provider-model
    family: example
    repository: example-org/example-model # where the weights and their licence come from
    served_name: example-model # the model name the API expects
    revision: "2026-10"
    weights_licence: apache-2.0 # must be on ALLOWED_WEIGHTS_LICENCES
    licence_url: https://example.org/example-model/LICENSE
    serving: hosted # hosted recipes pin no weight files
    context_tokens: 131072
    reasoning: true
    extra_body: {"thinking": {"type": "disabled"}} # sent unchanged with every request
```

```sh
export EXAMPLE_PROVIDER_API_KEY=...   # in your shell, not in .env
export KITTUM_RECIPES_FILE=.kittum/recipes.yaml
export KITTUM_LLM_ENDPOINTS=example-provider-model=https://api.example-provider.test/v1
export KITTUM_LLM_API_KEYS=example-provider-model=EXAMPLE_PROVIDER_API_KEY
export KITTUM_JUDGE_MODEL=example-provider-model
uv run kittum demo seed --live
```

For development, `pnpm dev:web` serves the workbench with hot reload on port 5180 against
`kittum serve`, and `VITE_KITTUM_WORKBENCH=mock pnpm dev:web` shows recorded example data without
a server. `pnpm --dir apps/web e2e` runs the end-to-end journeys against a migrated database in
`KITTUM_DATABASE_URL`, with the source gateway answering from the recorded responses in
`apps/server/tests/enrich/fixtures/stack` (`KITTUM_OFFLINE=1` with `KITTUM_GATEWAY_RECORDINGS`),
so nothing touches the network. `uv run python -m tests.app.record_stack` in `apps/server` records them again.

## Documents

- [Design](DESIGN.md): the pipeline, modules, data model, audit ledger, reviewer UI, stack, and
  the two-week spike.
- [Sources](docs/sources.md): free and commercial search and enrichment sources, rate limits,
  licences, and how federation fits PRISMA-S.
- [Document and model tools](docs/document-tools.md): PDF parsers, OCR, extraction libraries, and
  local models, with licence verdicts.
- [Dependencies](docs/dependencies.md): the licence decision and an evaluation of every dependency
  for fit, quality, and maintenance.
- [Standards and regulation](docs/standards.md): what PRISMA, RAISE, the EU AI Act, GDPR, and
  quality frameworks require of the system.
- [Profiles](DESIGN.md#profiles): optional agency and jurisdiction rules, with a list of the
  profiles and their docs.
- [Work packages](docs/work-packages.md): the build split into independent pieces, with status.
- [Council record](docs/council.md): how the design was reached, where the council disagreed, and
  which claims were checked.

## Name

Kittum (Akkadian *kittu*, "truth, reliability") was a Babylonian deity, the child of the justice
god Šamaš. Legal texts invoke Kittum as a witness. The product exists to produce a witnessed,
checkable record, so the name describes the job. Say it KIT-toom.

## Licence

Kittum, like the rest of Nebu, is licensed under the
[GNU Affero General Public License v3.0 or later](../LICENSE).
