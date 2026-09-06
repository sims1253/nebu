# Examples

Upload `document.pdf`, `specification.json`, and the reference file from one directory.
The two bundled PDFs are generated fixtures. All their fields should extract and match.

| Example                          | Reference | Covers                                                |
| -------------------------------- | --------- | ----------------------------------------------------- |
| [purchase-order](purchase-order) | JSON      | Exact text, numeric tolerance, label aliases.         |
| [invoice](invoice)               | CSV       | Dates, booleans, percentages, and numeric separators. |

## Downloaded cohort report

[cohort-report](cohort-report) includes a specification and reference data. Download its PDF
from the EU post-authorisation studies catalogue before using it. From the Janus directory:

```bash
cd apps/server && uv run python scripts/fetch_cohort_example.py
```

The script checks the full report's size and SHA-256 before saving a three-page excerpt.
CI downloads and tests the same example. Without the PDF, the local test skips it.
