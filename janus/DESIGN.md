# How Janus works

```mermaid
flowchart LR
    S[Specification + reference] --> C[Compile plan]
    P[PDF] --> R[Read evidence]
    C --> E[Extract fields]
    R --> E
    E --> M[Compare values]
    C --> M
    M --> U[Human review]
```

The [pipeline](apps/server/src/janus/pipeline/review_pipeline.py) compiles reference pointers,
reads PDF text and tables, locates the requested values, then compares them. Extraction uses
the specification and document evidence; it does not read expected reference values.

Reviews move through `created`, `validating_inputs`, `reading_document`, `extracting_fields`,
`comparing`, and `ready`, or end in `error`. Each stage saves a versioned JSON artifact.
Writes use a temporary file followed by an atomic rename. Evidence is cached by document hash;
extraction is cached by document and extraction hashes. Changing only reference data reuses
both caches. Any specification change changes field IDs and invalidates extraction reuse.
Reviewer corrections change the review's extraction and result, leaving the shared cache intact.

The review screen puts fields on the left and the PDF on the right. Selecting a row opens its
PDF page and shows confidence, comparison explanation, extraction status, and row/column context.
Enter or Space selects a focused row. Each row has a resolution menu; the toolbar filters by
status and exports CSV or JSON. Status labels accompany colors. The initial theme uses the
system preference, and the header toggle saves the chosen theme.

`not_compared` means Janus could not compare the field; `ambiguous` means it could not choose
one extraction. Neither status means that the document disagrees with the reference.
A `mismatch` requires reviewer attention, including when a generated row is missing on either side.
