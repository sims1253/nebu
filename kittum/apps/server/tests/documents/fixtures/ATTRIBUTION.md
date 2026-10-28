# Fixture PDFs

Both files are unmodified publisher PDFs from the PubMed Central Open Access subset, downloaded on
28.10.2026 from the PMC open-data bucket (`s3://pmc-oa-opendata/<PMCID>.1/<PMCID>.1.pdf`). Both
articles are licensed under the Creative Commons Attribution 4.0 International License
(CC BY 4.0, <https://creativecommons.org/licenses/by/4.0/>), as stated on their first pages and in
the PMC metadata (`license_code: CC BY`). They are used here unchanged, as test inputs.

| File | Article | Licence | SHA-256 |
| --- | --- | --- | --- |
| `gerber2026-lesotho-t2d-crct.pdf` | Gerber F, …, Labhardt ND, Amstutz A. Community health worker-led versus facility-based type 2 diabetes care in rural Lesotho: a cluster-randomized trial within the ComBaCaL cohort study. *BMC Medicine* 2026;24:400. doi:[10.1186/s12916-026-04943-4](https://doi.org/10.1186/s12916-026-04943-4). PMC13374172, PMID 42174613. | CC BY 4.0, © The Author(s) 2026 | `5df13e59b723f42e9b3e61cb59c1265a5e8b36d1049502d26381e09a6fcc8199` |
| `li2026-vitamin-c-kidney-biopsy-rct.pdf` | Li J, Zhao F, Lu R, …, He L. Effect of Vitamin C on bleeding and pain after kidney biopsy: a randomized controlled trial. *BMC Medicine* 2026;24:515. doi:[10.1186/s12916-026-05037-x](https://doi.org/10.1186/s12916-026-05037-x). PMC13613656, PMID 42399956. | CC BY 4.0, © The Author(s) 2026 | `ce719de5bc158fc9e6cb6dab9c35e1ac8c10c5ce05ad42512a46447e40e6260e` |

Why these two: both use the two-column BMC layout. The Lesotho trial's baseline table (Table 1,
page 7) has merged column headers ("Primary analysis set" and "All participants set" over three
arms each). The Vitamin C trial's results tables (Tables 4 and 5, page 7) have row-spanning header
cells and two tables on one page.

## `jmir/`: the wrong document of issue #60

Recorded by `tests/documents/record_jmir.py`. Both PDFs are unmodified and licensed under CC BY 4.0
(PMC metadata `license_code: CC BY`; Unpaywall `license: cc-by`).

| File | What it is | SHA-256 |
| --- | --- | --- |
| `app2.pdf` | The CONSORT-EHEALTH checklist the authors filed as Multimedia Appendix 2 of Morgan, He, Nicholson, El Shahawy, Sherman, Stevens. Using Text Messaging Ecological Momentary Assessment to Record Changes in e-Cigarette and Combustible Cigarette Use: Pilot Randomized Clinical Trial. *JMIR Formative Research* 2025;9:e66709. doi:[10.2196/66709](https://doi.org/10.2196/66709). Copied from the 28 October 2026 live run, where Unpaywall named it as the article's PDF. | `696b4a6a3b27bbb2e945bd717142235d155042a66cc116592c93a909859f7674` |
| `f2e610f7…` | The article itself, from the PMC Article Datasets (PMC11951811, PMID 40116747). | `f2e610f76c1ab51c8be7d6a01fa7e3cff1e3bf30df53465c04116c699da68994` |
