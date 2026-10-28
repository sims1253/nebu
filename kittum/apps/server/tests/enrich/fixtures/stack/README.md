# Recorded source responses for the stack

What `KITTUM_GATEWAY_RECORDINGS` serves in the end-to-end journeys and `tests/app/test_enrichment.py`.
Recorded on 28.10.2026 by `uv run python -m tests.app.record_stack`, through Kittum's own HTTP
transport, with the contact address `test@kittum.dev` and no API key. `manifest.json` maps each
request URL to its body, status, and content type.

| Responses | Source and terms |
| --- | --- |
| `api.openalex.org` | OpenAlex works, CC0 |
| `api.crossref.org` | Crossref metadata, free to reuse; abstracts remain their publishers' |
| `www.ebi.ac.uk/europepmc` | Europe PMC search results; abstracts remain their publishers' |
| `api.unpaywall.org` | Unpaywall OA locations |
| `www.ncbi.nlm.nih.gov/research/pubtator3-api` | PubTator 3 annotations (NCBI public service); abstracts remain their publishers' |
| `europepmc.org`, `www.thelancet.com` | The HTML pages those hosts answered a PDF request with (HTTP 403) |
| `link.springer.com` | The two CC BY 4.0 trial PDFs, byte-identical to `tests/documents/fixtures/`, which the manifest references instead of copying |
