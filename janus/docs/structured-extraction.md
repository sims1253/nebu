# Structured extraction

Extract data from a PDF without a reference dataset or a review server. The specification
names the output fields and tells Janus where to find them. Comparison is a separate step.

From `apps/server`:

```bash
uv sync --all-extras
uv run janus extract ../../examples/purchase-order/document.pdf \
  ../../examples/purchase-order/extraction.json > extraction.json
uv run janus compare extraction.json ../../examples/purchase-order/reference.json \
  ../../examples/purchase-order/checks.json
```

`extract --data-only` prints only the nested data for your own loader. The full result also
includes field statuses, PDF locations, page dimensions, and input hashes. Exit status is
0 for a complete extraction (or all comparisons matching), 2 for unresolved fields or
nonmatching comparisons, and 1 for invalid inputs or a command failure. Check the exit status
before loading data: unresolved scalar values are `null`, and a missing table is `[]` with a
field issue. Standard output contains JSON; command errors go to standard error.

## Specification

Use `kind: "extraction"`, `schema_version: "1"`, a `name`, and a `fields` object. Each key
becomes an output key. `uv run janus schema` prints the complete JSON schema.

A scalar field declares `type` and `locate`. Types are `text`, `integer`, `decimal`,
`percentage`, `boolean`, and `date`. Locators use the existing [locator rules](locators.md).
Use an object field to nest fields:

```json
{
  "name": "Order",
  "fields": {
    "order": {
      "type": "object",
      "fields": {
        "number": {
          "type": "text",
          "locate": { "strategy": "text_label", "labels": ["Order number"] }
        }
      }
    }
  }
}
```

Numbers use `.` as the decimal separator and no grouping by default. Set
`decimal_separator` and `group_separator` to the printed conventions; Janus rejects invalid
grouping. `strip_prefix` and `strip_suffix` require and remove literal affixes such as `$` or
`kg`. Percentage values remove an optional `%` and retain percentage units: `12.5%` becomes
`"12.5"`, not `"0.125"`. Decimal and percentage outputs are strings to preserve precision.
Integers are JSON numbers within the exact JavaScript integer range. Dates become ISO dates;
the input must be accepted by Python's ISO date parser. Booleans accept true/false, yes/no, or 1/0.
Text trims surrounding whitespace. Compound cells can declare `source_shape` and `component`
using the [existing compound syntax](comparison-specification-v1.md#values-and-compound-cells).

## Records

Record fields select tables by their column headers and return one object per physical row:

```json
{
  "type": "records",
  "max_rows": 200,
  "columns": {
    "description": { "type": "text", "labels": ["Description", "Item"] },
    "quantity": { "type": "integer", "labels": ["Qty", "Quantity"] },
    "amount": {
      "type": "decimal",
      "group_separator": ",",
      "labels": ["Amount"]
    }
  }
}
```

The first detected row must contain every requested header. Header aliases match after case
folding and whitespace collapse. Duplicate matching headers produce an ambiguity issue.
Column order may vary; cells stay associated by table and row coordinates. Empty cells become
`null` with a missing-field issue. Repeated headers are skipped. All matching tables contribute
records in page order; this does not distinguish tables with identical headers by meaning.
Missing tables and row limits produce explicit issues. `max_rows` defaults to 200 and permits
up to 2000. The reader must recover the table structure correctly; this interface does not
repair arbitrary merged cells or perform OCR.

## Output and comparison

`data` contains nested values. `fields` describes each scalar by JSON Pointer, type, status,
source location, and message. Status is `extracted`, `missing`, `ambiguous`, `invalid`, or `error`.
Record-level issues use the record array's pointer. Locations use zero-based pages and PDF
points; user interfaces display page numbers starting at one. An extracted field's score is
not presented as a probability of correctness.

Comparison rules explicitly pair an output `path` with a `reference_pointer`. JSON, CSV, and
XLSX references use the [reference normalization rules](comparison-specification-v1.md#reference-data).
At least one rule is required. Numbers compare with an optional nonnegative `absolute_tolerance` (default zero). Other values
use typed equality after parsing date and boolean reference cells. Date checks compare calendar
days, ignoring a reference datetime's time component. Numeric comparison supports precision up to
10,000 decimal places; greater ranges are not compared. Missing or invalid inputs produce `not_compared`. Record comparisons must
name the intended row pointers; Janus does not infer record identity across datasets.

## Python

```python
from pathlib import Path
from janus.extraction import ExtractionSpecification, extract

spec = ExtractionSpecification.model_validate_json(Path("extraction.json").read_bytes())
result = extract(Path("document.pdf"), spec)
print(result.data)
for field in result.fields:
    if field.status != "extracted":
        print(field.path, field.status, field.message)
```

The library does not write files. The existing three-input comparison specifications and saved
review artifacts retain their existing format and behavior.
