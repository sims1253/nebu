# Janus

Compare a PDF with reference data, then review each result beside the source document.
Upload the PDF, a JSON comparison specification, and reference data in JSON, CSV, or XLSX.
Janus runs locally and reads the PDF text layer; scans need text before Janus can read them.

## Start

Install Bun and uv, then run from this directory:

```bash
bun install --frozen-lockfile
cp apps/server/.env.example apps/server/.env
bun run dev
```

Open [localhost:3002](http://localhost:3002). Try the three files in
[examples/purchase-order](examples/purchase-order) first; both fields should match.
Select a result to see its PDF page and extraction details. Record a decision in the
resolution menu, or export the review as CSV or JSON.

## Guides

- [Examples](examples/README.md): ready-to-run inputs.
- [Write a specification](docs/comparison-specification-v1.md): reference pointers, values, and comparisons.
- [Locate PDF content](docs/locators.md): tables, labeled text, and prose sections.
- [Repeated rows](docs/repeated-rows.md): one rule for many matching labels.
- [Server setup and checks](apps/server/README.md): configuration, storage, and development.
- [How it works](DESIGN.md): pipeline, caches, and review screen.
- [Schema-authoring skill](skills/schema-authoring/SKILL.md): instructions for an agent writing a specification.

A reviewer must check the results. Janus does not do OCR, evaluate formulas, derive values
across fields, align arbitrary repeated records, or answer free-text questions. Specifications
are written outside the browser.
