# Janus product

Janus helps a reviewer check the values printed in a PDF against reference data — an export from the system that issued the document, a spreadsheet someone maintains, or plain JSON. One review takes three files: the PDF, a JSON comparison specification, and reference data in JSON, CSV, or XLSX.

The machine extracts and compares; the person decides. Every extracted value keeps its page, table, row, and column, a confidence score, and a sentence that explains the comparison result, so a reviewer can check each mismatch against the document itself. Janus does not replace the person who reads the results.

Version 1 covers fixed scalar fields, four compound shapes (`count_percentage`, `mean_standard_deviation`, `range`, `estimate_interval`), the `table_label` locator, and the `exact`, `numeric`, `date`, and `boolean` operators. Out of scope for now: aligning repeated records, formulas, free-text questions, and a specification editor in the browser.
