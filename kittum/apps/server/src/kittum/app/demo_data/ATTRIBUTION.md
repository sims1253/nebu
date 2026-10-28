# Demo data

What `kittum demo seed` loads. Nothing here is fetched at run time.

| File | What | Source and licence |
| --- | --- | --- |
| `pubmed_esearch.xml`, `pubmed_efetch.xml` | The replies of NCBI E-utilities to the demo strategy (`kittum.app.demo.PUBMED_STRATEGY`), 20 records, recorded on 28.10.2026 by `scripts/record_demo_search.py` | PubMed citation data from the U.S. National Library of Medicine; abstracts remain their publishers' |
| `central_export.ris` | An illustrative CENTRAL export: six fictitious nicotine replacement trials (DOIs under the reserved `10.1000` example prefix) and one real record that PubMed also returns, so the corpus proposes a duplicate | Written for Kittum |
| `citation_search.ris` | The two trials below as found by a citation search, with their abstracts, and the registry entry of the Lesotho trial's cohort (NCT05596773), so the corpus proposes a study link | Abstracts CC BY 4.0, see below; the registry entry from the ClinicalTrials.gov API on 28.10.2026, public domain |
| `tests/documents/fixtures/*.pdf` (not copied) | The full texts the demo attaches | CC BY 4.0; see that directory's ATTRIBUTION.md |

The two CC BY trials are off-topic for the nicotine review on purpose: the abstracts do not say
whether participants smoke, so they pass title and abstract screening as unclear, and full text
shows the reviewer why they go.
