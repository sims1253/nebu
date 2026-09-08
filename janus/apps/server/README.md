# Janus server

The FastAPI server validates inputs, reads PDFs with PyMuPDF, extracts fields, and saves
comparison results. It uses the PDF text layer without OCR.

## Run

From this directory:

```bash
cp .env.example .env
uv run uvicorn janus.main:app --reload --port 3001
```

Open [API documentation](http://localhost:3001/docs) for request and response schemas.
`POST /api/reviews` accepts multipart uploads named `document`, `specification`, and
`reference`. `POST /api/reviews/validate` checks only the specification and reference.
PDFs have a 500 MB limit; each other input has a 50 MB limit.

`GET /api/reviews/{review_id}` returns the current stage in `status` and any failure
message in `error_message`. Fetch `/api/reviews/{review_id}/result` when the status is `ready`.

## Extraction API

`POST /api/extractions` accepts a `document` PDF (up to 50 MB) and `specification` JSON
(up to 2 MB). It returns the completed extraction, including nested data and field evidence.
The request waits for extraction to finish. `GET /api/extractions` lists completed runs;
`GET /api/extractions/{id}` reopens one. The document, specification, and rendered pages are
available under `/{id}/document`, `/{id}/specification`, and `/{id}/pages/{zero_based_page}`.
`GET /api/extractions/schema` returns the extraction specification JSON schema.

`POST /api/extractions/{id}/compare` accepts a `reference` file (JSON, CSV, or XLSX, up to
10 MB) and a `rules` form field containing a JSON list of output/reference pointer pairs.
It returns comparison checks without changing the saved extraction. `DELETE /api/extractions/{id}`
removes the saved run and PDF. Extractions are stored under `JANUS_DATA_DIR/extractions`,
independently of the review metadata backend. Back up that directory to keep completed runs.

## Configuration and storage

Environment variables override `.env` settings. Paths default under `~/.janus`.

| Variable               | Default                       | Purpose                                                |
| ---------------------- | ----------------------------- | ------------------------------------------------------ |
| `JANUS_LOG_LEVEL`      | `INFO`                        | Logging level: `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |
| `JANUS_DATA_DIR`       | `~/.janus`                    | Root for the default storage paths below.              |
| `JANUS_UPLOAD_DIR`     | `~/.janus/uploads/reviews`    | Uploaded files.                                        |
| `JANUS_ARTIFACT_DIR`   | `~/.janus/artifacts`          | Review artifacts and shared caches.                    |
| `JANUS_REVIEW_STORE`   | `memory`                      | `memory` or `sqlite`.                                  |
| `JANUS_REVIEW_DB_PATH` | `~/.janus/reviews-v2.sqlite3` | SQLite metadata database.                              |

The example `.env` selects `sqlite`, which keeps review metadata across restarts. Without
that setting, the review list lives in memory and disappears on restart, even though uploads
and artifacts remain on disk. A restart does not resume reviews that were still processing.
Keep the database, uploads, and artifacts together when backing up or moving saved reviews.
Deleting a review removes its metadata, uploads, and artifacts; shared caches remain.

## Checks

Install development and optional authoring dependencies before checking all source files. From this directory:

```bash
uv sync --all-extras
uv run --no-sync pytest
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync ty check src
```

From the Janus directory, `bun run check` runs workspace lint, type checks, tests, and the web
build. `bun run format:check` checks formatting. Browser checks use `bun run test:e2e`; install
Chromium first with `(cd apps/web && bunx playwright install chromium)` if needed.

The optional schema-authoring experiments use `uv sync --all-extras`, an external model, and
local files under `janus/benchmarks/schema-authoring`. That corpus is not published in this
repository. These experiments are separate from the review server.

To enable the formatter for staged files, run `git config core.hooksPath .githooks` from the
repository root. CI runs the full checks.
