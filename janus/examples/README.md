# Examples

Each directory is a complete run: upload `document.pdf`, `specification.json`, and the reference file to the web app (or the three-file `POST /api/reviews`). Every example verifies end-to-end: all fields extract and match.

| Example          | Reference format | Demonstrates                                                                                                                             |
| ---------------- | ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `purchase-order` | JSON             | text `exact` comparison, `numeric` with tolerance, alias labels                                                                          |
| `invoice`        | CSV              | `date` and `boolean` operators, `percentage` values with the `percent_symbol` normalizer, thousands separators via `numeric_punctuation` |

The `purchase-order` and `invoice` documents are generated fixtures (see the reader tests in `apps/server` for similar generators), so they carry no third-party licensing.

`cohort-report` downloads its document on demand from the EU post-authorisation studies catalogue (a finalised cohort study's public report; direct link, no login):

```bash
cd apps/server && uv run --no-sync python scripts/fetch_cohort_example.py
```

The fetch verifies exact size and sha256 before writing a three-page excerpt. CI runs the same download and test.
