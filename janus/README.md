# Janus

Janus compares values printed in a PDF against reference data. You upload three files: the PDF, a comparison specification (JSON), and reference data (JSON, CSV, or XLSX). It extracts each field named in the specification, compares it against the reference value, and opens a review. Every row shows both values, where the document value came from (page, table, row, column), and a menu to record a decision.

Everything runs on your machine. Version 1 checks fixed scalar fields located by row labels in PDF tables. A person still judges the results.

## Inputs

1. Source document: PDF, up to 500 MB.
2. Comparison specification: JSON matching `packages/contracts/schema/comparison-specification.v1.json`. How to write one: `docs/comparison-specification-v1.md`.
3. Reference data: JSON, CSV, or XLSX, up to 50 MB. The specification has the same limit.

## Quick start

```bash
bun install --frozen-lockfile
bun run dev:web:all
```

Open http://localhost:3002 (the API serves on port 3001). Upload the three files. `examples/purchase-order/` has a document, a specification, and reference data where every field matches — start there.

## How a review runs

The server stores the uploads under `JANUS_UPLOAD_DIR` and runs the pipeline in the background. A review moves through the statuses `created`, `validating_inputs`, `reading_document`, `extracting_fields`, `comparing`, then `ready` (or `error`); the page shows progress while it runs.

- `SpecificationCompiler` (`apps/server/src/janus/comparison/compiler.py`) checks the specification against the JSON schema, normalizes the reference data, resolves every `reference.pointer`, and gives each field a stable ID.
- `TextLayerDocumentReader` reads the PDF with PyMuPDF: per-page text and detected tables, plus a hash of the file.
- `SchemaExtractor` finds each field by matching row labels (details in the specification guide) and takes the value from another column of the same row. A field with a `rows` generator yields one extraction per row label matching the generator's pattern. It reads the document and the field's locate and value rules, never the reference values.
- `ComparisonEngine` applies the field's normalizers in order, then its operator (`exact`, `numeric`, `date`, `boolean`) and tolerances.
- `ReviewArtifactStore` writes plan, evidence, extraction, result, and progress as `.v2.json` files under `JANUS_ARTIFACT_DIR`. Each write goes to a temporary file first, then moves into place.

Evidence is cached by document hash, and extraction by document hash plus a hash of the specification. A second review of the same PDF skips reading; with the same specification it also skips extraction.

The review page lists each field with the reference value, the document value, the status, and a resolution menu (`Accept`, `Reference value incorrect`, `Incorrect document match`, `Not present in document`). Select a row. The PDF opens on the page the value came from, and a panel beside it shows the confidence, the comparison explanation, and the row and column context. Buttons above the table filter by status and export the result as CSV or JSON.

## Configuration

Copy `apps/server/.env.example` to `apps/server/.env` and edit:

| Variable               | Default                       | Meaning                                                       |
| ---------------------- | ----------------------------- | ------------------------------------------------------------- |
| `JANUS_LOG_LEVEL`      | `INFO`                        | `DEBUG`, `INFO`, `WARNING`, or `ERROR`.                       |
| `JANUS_DATA_DIR`       | `~/.janus`                    | Root for everything below; set it to move all state at once.  |
| `JANUS_UPLOAD_DIR`     | `~/.janus/uploads/reviews`    | Uploaded documents, specifications, and references.           |
| `JANUS_ARTIFACT_DIR`   | `~/.janus/artifacts`          | Plan, evidence, extraction, result, and progress files.       |
| `JANUS_REVIEW_STORE`   | `memory`                      | `memory` loses review metadata on restart; `sqlite` keeps it. |
| `JANUS_REVIEW_DB_PATH` | `~/.janus/reviews-v2.sqlite3` | SQLite database, used only with `JANUS_REVIEW_STORE=sqlite`.  |

`.env.example` ships with `JANUS_REVIEW_STORE=sqlite`. Without a `.env`, the code default `memory` applies and the review list is empty after a restart.

## Reference normalization

Every `reference.pointer` is an RFC 6901 JSON Pointer into the reference data as the server reshapes it:

- JSON keeps its tree: `/order/id`.
- CSV becomes `{"rows": [...]}`, one object per record: `/rows/0/total`.
- XLSX becomes `{"sheets": {"Sheet1": {"rows": [...]}}}`: `/sheets/Sheet1/rows/0/total`.

CSV and XLSX need a first row of non-empty, unique column headers. The server rejects an XLSX workbook with more than 5,000,000 cells. A missing pointer on a required field fails compilation; the diagnostic names the specification path and a fix.

## Limitations

Version 1 handles PDF input, scalar fields, fixed compound values (`12 (34%)`, `10–20`), the `table_label` locator, and repeated rows that share one label pattern (`rows`: one field per row shape, not one field per row). It does not align repeated records whose rows share no label pattern, evaluate expressions, derive values across fields, or answer free-text questions. It reads the PDF text layer; a scan without one yields no values.

## Checks

```bash
bun run check   # lint, typecheck, tests, build
cd apps/server && uv run pytest
```

Activate the pre-commit formatter once per clone (it formats staged files; CI runs the full checks):

```bash
git config core.hooksPath .githooks
```
