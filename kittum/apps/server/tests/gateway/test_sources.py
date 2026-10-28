import httpx2

from kittum.core import sha256_hex
from kittum.gateway import HttpRequest, HttpxTransport, default_policies, request_hash

PUBLISHED_PER_SECOND = {
    "pubmed": 3,
    "clinicaltrials": 50 / 60,
    "europepmc": 10,
    "openalex": 10,
    "crossref": 5,
    "unpaywall": 10,
    "pubtator": 3,
}


def test_default_policies_sit_ten_percent_under_published_limits() -> None:
    policies = default_policies()

    assert set(policies) == set(PUBLISHED_PER_SECOND)
    for source, published in PUBLISHED_PER_SECOND.items():
        policy = policies[source]
        assert policy.requests_per_second <= published * 0.9
        assert policy.concurrency >= 1
        assert policy.terms_url.startswith("https://")

    assert policies["unpaywall"].daily_budget == 90_000
    assert policies["openalex"].daily_budget == 9_000


def test_a_contact_email_unlocks_the_faster_pools() -> None:
    polite = default_policies(contact_email="librarian@example.org", ncbi_api_key="key")
    public = default_policies()

    assert polite["crossref"].requests_per_second > public["crossref"].requests_per_second
    assert polite["crossref"].concurrency > public["crossref"].concurrency
    assert polite["pubmed"].requests_per_second > public["pubmed"].requests_per_second


def test_transport_sends_the_version_and_contact_as_user_agent() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["user-agent"] = request.headers["user-agent"]
        return httpx2.Response(200, content=b"hello")

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    transport = HttpxTransport(contact_email="librarian@example.org", client=client)

    response = transport.send(
        HttpRequest(url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi")
    )

    assert response.status == 200
    assert response.body == b"hello"
    assert response.body_hash == sha256_hex(b"hello")
    assert seen["user-agent"].startswith("Kittum/")
    assert "librarian@example.org" in seen["user-agent"]


def test_transport_and_cache_key_keep_the_query_in_the_url() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        return httpx2.Response(200, content=b"ok")

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    transport = HttpxTransport(client=client)
    request = HttpRequest(url="https://example.org/search?term=cancer", params=(("page", "2"),))

    transport.send(request)

    assert seen["url"] == "https://example.org/search?term=cancer&page=2"
    assert request_hash(request) == request_hash(
        HttpRequest(url="https://example.org/search", params=(("term", "cancer"), ("page", "2")))
    )
