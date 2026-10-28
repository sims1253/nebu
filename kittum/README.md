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
   screens, the audit gauge, the ledger check), opens full-text screening there with `n`, and
   confirms study links on **Studies** (`Enter`).
5. The reviewers screen the full texts: `]` jumps to a protocol term on the page, `Enter` adopts
   it as evidence, `y` `n` `?` give each criterion a verdict, `i` `e` `x` decide.

`uv run kittum ledger verify <project ID>` checks the project's hash chain. `uv run kittum
project --help` lists the lifecycle steps for a review of your own: create it, add people,
publish a protocol, import or run searches, ingest the corpus, attach full texts, open stages.

For development, `pnpm dev:web` serves the workbench with hot reload on port 5180 against
`kittum serve`, and `VITE_KITTUM_WORKBENCH=mock pnpm dev:web` shows recorded example data without
a server. `pnpm --dir apps/web e2e` runs the end-to-end journey against a migrated database in
`KITTUM_DATABASE_URL`.

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
