# Janus

Extract structured data from PDFs using their text and layout. Define the fields you need,
inspect their source locations, then export the data or compare it with a reference dataset.
Janus runs locally. It does not send documents to a model or OCR scanned pages.

## Start the workbench

Install Bun and uv, then run from this directory:

```bash
bun install --frozen-lockfile
cp apps/server/.env.example apps/server/.env
bun run dev
```

Open [localhost:3002](http://localhost:3002). Choose **Try purchase order** to run the bundled
example, or upload a PDF and an extraction specification. Select a field to highlight its
source on the PDF. Switch to JSON to inspect the nested output.

- **Export data** saves the nested values for your own tools.
- **Export with evidence** includes field statuses, source locations, and document identity.
- **Specification** lets you edit the saved specification and extract again as a separate run.
- **Compare data** checks the saved extraction against JSON, CSV, or XLSX reference data.

Missing, ambiguous, and invalid fields remain visible. Inspect unresolved fields before using
the exported data. Completed extractions are saved locally and can be reopened after a restart.
Comparison previews stay in the current tab; export them to retain the results.

## Use the library or CLI

The extraction engine works without the web server, reference data, or a review record.
From `apps/server`:

```bash
uv run janus extract ../../examples/purchase-order/document.pdf \
  ../../examples/purchase-order/extraction.json > extraction.json
uv run janus compare extraction.json ../../examples/purchase-order/reference.json \
  ../../examples/purchase-order/checks.json
```

Read the [structured extraction guide](docs/structured-extraction.md) for nested objects,
record tables, numeric formats, exit codes, and the Python interface.

## Guides

- [Structured extraction](docs/structured-extraction.md): specification, outputs, library, and CLI.
- [Locate PDF content](docs/locators.md): tables, labeled text, and prose sections.
- [Examples](examples/README.md): ready-to-run inputs.
- [Server setup and checks](apps/server/README.md): configuration, storage, and development.
- [How it works](DESIGN.md): extraction, comparison, and persistence.

The **Comparison reviews** page retains the existing three-input workflow and saved reviews.
Its [comparison specification](docs/comparison-specification-v1.md), [repeated-row rules](docs/repeated-rows.md),
and [authoring skill](skills/schema-authoring/SKILL.md) remain available.

Janus depends on readable PDF text and recoverable layout. It does not infer arbitrary record
identity across datasets, evaluate formulas, or answer free-text questions. Specifications are
JSON documents; the workbench can edit and rerun them.
