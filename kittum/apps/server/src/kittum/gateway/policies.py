"""Default source policies, 10% under the limits in docs/sources.md.

Configuration overrides any of these by passing a full mapping to
`kittum.gateway.SourceGateway`, for example `{**default_policies(), "pubmed": my_policy}`.
"""

from kittum.gateway.types import SourcePolicy

MARGIN = 0.9
"""Share of a published limit Kittum may use."""


def _under(limit: float) -> float:
    return round(limit * MARGIN, 3)


def _budget(limit: int) -> int:
    return int(limit * MARGIN)


def default_policies(
    *, contact_email: str | None = None, ncbi_api_key: str | None = None
) -> dict[str, SourcePolicy]:
    """The free-source policies from docs/sources.md, narrowed by `MARGIN`.

    A configured contact email or NCBI key unlocks Crossref's polite pool and PubMed's higher
    rate. Concurrency caps are Kittum's choice where a source publishes none.
    """

    pubmed_rate = 10 if ncbi_api_key else 3
    crossref_rate, crossref_concurrency = (10, 3) if contact_email else (5, 1)
    return {
        "pubmed": SourcePolicy(
            source="pubmed",
            requests_per_second=_under(pubmed_rate),
            concurrency=3 if ncbi_api_key else 2,
            terms_url="https://www.ncbi.nlm.nih.gov/books/NBK25497/",
        ),
        # 50 requests per minute.
        "clinicaltrials": SourcePolicy(
            source="clinicaltrials",
            requests_per_second=_under(50 / 60),
            concurrency=2,
            terms_url="https://clinicaltrials.gov/about-site/terms-conditions",
        ),
        "europepmc": SourcePolicy(
            source="europepmc",
            requests_per_second=_under(10),
            concurrency=4,
            terms_url="https://europepmc.org/terms",
        ),
        # The free key's USD 1 per day is about 10,000 requests at USD 0.0001 each.
        "openalex": SourcePolicy(
            source="openalex",
            requests_per_second=_under(10),
            concurrency=4,
            daily_budget=_budget(10_000),
            terms_url="https://openalex.org/terms",
        ),
        "crossref": SourcePolicy(
            source="crossref",
            requests_per_second=_under(crossref_rate),
            concurrency=crossref_concurrency,
            terms_url="https://www.crossref.org/documentation/retrieve-metadata/rest-api/",
        ),
        "unpaywall": SourcePolicy(
            source="unpaywall",
            requests_per_second=_under(10),
            concurrency=2,
            daily_budget=_budget(100_000),
            terms_url="https://unpaywall.org/products/api",
        ),
        "pubtator": SourcePolicy(
            source="pubtator",
            requests_per_second=_under(3),
            concurrency=1,
            terms_url="https://www.ncbi.nlm.nih.gov/research/pubtator3/",
        ),
        # The PMC ID converter takes NCBI's E-utilities rate; the PMC Article Datasets bucket on
        # AWS publishes no limit, so it shares the converter's.
        "pmc": SourcePolicy(
            source="pmc",
            requests_per_second=_under(pubmed_rate),
            concurrency=2,
            terms_url="https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/",
        ),
        # api.biorxiv.org publishes no limit; one request a second is Kittum's choice.
        "biorxiv": SourcePolicy(
            source="biorxiv",
            requests_per_second=_under(1),
            concurrency=1,
            terms_url="https://api.biorxiv.org/",
        ),
        # One request every three seconds, on one connection.
        "arxiv": SourcePolicy(
            source="arxiv",
            requests_per_second=_under(1 / 3),
            concurrency=1,
            terms_url="https://info.arxiv.org/help/api/tou.html",
        ),
        # One request a second with a key; without one, a pool shared by every caller.
        "semanticscholar": SourcePolicy(
            source="semanticscholar",
            requests_per_second=_under(1),
            concurrency=1,
            terms_url="https://www.semanticscholar.org/product/api/license",
        ),
        # A registered key: 10 requests a minute and 1,000 a day at the lowest tier.
        "core": SourcePolicy(
            source="core",
            requests_per_second=_under(10 / 60),
            concurrency=1,
            daily_budget=_budget(1_000),
            terms_url="https://core.ac.uk/terms",
        ),
    }


__all__ = ["MARGIN", "default_policies"]
