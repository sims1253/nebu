# Working on Kittum

Kittum runs systematic literature reviews for AMNOG dossiers, from the protocol to the Modul 4
annexes, with every decision in a hash-chained ledger. Read [DESIGN.md](DESIGN.md) before changing
behaviour. Pick work from [docs/work-packages.md](docs/work-packages.md).

## Layout

```text
kittum/
├── apps/
│   ├── server/                 # Python 3.14, uv: every module below, the API, and the CLI
│   │   ├── src/kittum/<module>/   # one package per module from DESIGN.md
│   │   ├── tests/<module>/        # that module's tests
│   │   └── db/migrations/         # dbmate SQL migrations
│   └── web/                    # the reviewer workbench (React 19, TanStack Router, Vite+)
├── packages/contracts/         # TypeScript client generated from the server's OpenAPI
├── infra/compose.yaml          # Postgres 18, dbmate, GROBID
└── docs/                       # design reference and work packages
```

## Setup and checks

```sh
# Server
cd apps/server
uv sync
uv run ruff format src tests && uv run ruff check src tests && uv run ty check src
uv run pytest                          # Postgres tests skip without a database

# Postgres for the `postgres` tests
docker compose -f infra/compose.yaml up -d --wait postgres
docker compose -f infra/compose.yaml run --rm migrate
export KITTUM_DATABASE_URL=postgresql://kittum:kittum@127.0.0.1:55432/kittum

# Workbench and contracts (from kittum/)
pnpm install
pnpm check                             # format, lint, typecheck, tests, knip, build
pnpm contracts:generate                # after any API change; commit the result
pnpm dev:web                           # with `uv run kittum serve` running
```

CI runs all of this, including Postgres 18 and a check that the generated contracts match the
server.

## Working in parallel

Several agents work at once, one work package each. The rules keep them out of each other's way.

- **One work package per branch**, named `kittum/wp-NN-short-name`, branched from `main`.
- **Stay inside your module.** Change `src/kittum/<module>/`, `tests/<module>/`, your own
  migrations, and your module's section in `docs/work-packages.md`. Nothing else.
- **Import other modules only through their package**: `from kittum.judge import Verdict`, never
  `from kittum.judge.prompts import ...`. `tests/test_architecture.py` enforces this and the
  allowed dependencies. Tests may also import `kittum.<module>.testing` fakes.
- **Treat other modules' public types as fixed.** If you need a change to another module's
  interface, or to `core`, `ledger/chain.py`, the `ALLOWED` table, or the foundation migration,
  open a small pull request for only that change first, and say why.
- **Build against fakes.** Every module with a seam ships a fake in `<module>/testing.py`
  (`InMemoryLedger`, `RecordedBackend`, `FakeTransport` exist already). If your module has a seam,
  ship its fake with your first pull request so others can use it.
- **Migrations** are new files named `<UTC timestamp>_<module>_<what>.sql` with `migrate:up` and
  `migrate:down`. Create only your module's tables; never alter another module's.

## Conventions

- Values that cross modules are `kittum.core.Record`s: frozen, unknown fields rejected.
- IDs come from `kittum.core.new_id()` (UUIDv7). Time comes from a `Clock`, never `datetime.now()`.
- Decimals stay exact strings with explicit units. Empty is not zero.
- A state change writes its ledger event in the same transaction as its rows. Deciders do no I/O;
  side effects run as jobs (see `kittum.runs`).
- All HTTP goes through `kittum.gateway`. All model calls go through `kittum.models.LlmBackend`.
- A quote counts only if `kittum.core.find_quote` finds it in the source.
- Tests never touch the network. Use the fakes, recorded responses, or fixtures in the repo.
- Unimplemented interface functions raise `NotImplementedError("WP-NN")`. Replace, don't wrap.
- Python and TypeScript match the surrounding code: comment density, naming, idiom.

## Dependencies and licences

Kittum is AGPL-3.0-or-later. Any open-source code licence is fine in a dependency. Model weights
must be on `kittum.models.ALLOWED_WEIGHTS_LICENCES`: no revenue caps, non-commercial terms,
field-of-use limits, or no-competition clauses. Add every new dependency or model to
[docs/dependencies.md](docs/dependencies.md) or [docs/document-tools.md](docs/document-tools.md)
with its licence and the reason, in the same pull request.

## Done means

- The module's interface functions work for the cases in its work package, with tests.
- Ruff, ty, pytest, and (if touched) `pnpm check` pass.
- The module's fake still matches its interface.
- `docs/work-packages.md` says what is done and what is left.
