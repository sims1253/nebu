"""Recording the sources' responses in a live run, and replaying them offline.

A recording must hold no secret and no contact address, so a request that would put one into it
is refused before it is sent, and the address is replaced only where a source takes it. A replay
must answer as the sources did, in order: a source that failed and later recovered fails and
recovers again. The live run sees the same bytes its replay will, so their hashes agree.
"""

import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx2
import pytest

from kittum.core import FixedClock, sha256_hex
from kittum.db import Database
from kittum.gateway import (
    CONTACT_PLACEHOLDER,
    HttpRequest,
    HttpResponse,
    NotRecorded,
    RecordedTransport,
    RecordingRefused,
    RecordingTransport,
    SourceGateway,
    SourcePolicy,
    SourceUnavailable,
)
from kittum.gateway.testing import response
from kittum.storage import MemoryStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

ALICE = "alice@example.org"
SECRET = "cs-9f8e7d6c5b4a"
UNPAYWALL = f"https://api.unpaywall.org/v2/10.1186%2Fs12916-026-04943-4?email={ALICE}"
UNREACHABLE = f"https://api.unpaywall.org/v2/10.1186%2Fs12916-026-05037-x?email={ALICE}"


class Sources:
    """Answers each URL from its own script, in order, and remembers what was sent."""

    def __init__(self, scripts: dict[str, list[HttpResponse | Exception]]) -> None:
        self.scripts = scripts
        self.sent: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.sent.append(request)
        answer = self.scripts[request.url.split("?")[0]].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _written(directory: Path) -> bytes:
    return b"".join(p.read_bytes() for p in directory.iterdir() if p.is_file())


def test_a_recording_refuses_secrets_and_holds_the_contact_address_only_as_a_placeholder(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "sources"
    ok = response(200, b'{"message": "ok"}', (("Content-Type", "application/json"),))
    live = Sources(
        {
            "https://api.crossref.org/works/10.1000%2Fx": [ok],
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi": [ok],
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search": [ok],
        }
    )
    recording = RecordingTransport(live, directory, contact_email=ALICE, secrets=(SECRET,))

    def refused(request: HttpRequest, match: str) -> None:
        with pytest.raises(RecordingRefused, match=match) as caught:
            recording.send(request)
        assert SECRET not in str(caught.value) and ALICE not in str(caught.value)

    # A configured secret in a parameter Kittum does not know as a credential: refused, by name.
    refused(
        HttpRequest(
            url="https://api.crossref.org/works/10.1000%2Fx", params=(("client_secret", SECRET),)
        ),
        "parameter 'client_secret'",
    )
    refused(HttpRequest(url="https://reader:pw@api.crossref.org/works/10.1000%2Fx"), "user info")
    # The address anywhere but its source's contact parameter: a search for Alice is not Alice's
    # contact address, and must not turn into the placeholder.
    refused(
        HttpRequest(url=f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query={ALICE}"),
        "parameter 'query'",
    )
    refused(
        HttpRequest(url=f"https://api.crossref.org/works/10.1000%2Fx?query.author={ALICE.upper()}"),
        "parameter 'query.author'",
    )
    assert live.sent == []  # nothing refused was sent

    # Where a source takes the address, it is replaced; a known credential parameter is dropped.
    crossref = HttpRequest(url=f"https://api.crossref.org/works/10.1000%2Fx?mailto={ALICE}")
    ncbi = HttpRequest(
        url="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
        params=(("id", "1"), ("email", ALICE), ("api_key", SECRET)),
    )
    search = HttpRequest(
        url=f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query={CONTACT_PLACEHOLDER}"
    )
    for request in (crossref, ncbi, search):
        assert recording.send(request).status == 200
    manifest = json.loads((directory / "manifest.json").read_text())
    assert sorted(manifest) == [
        "https://api.crossref.org/works/10.1000%2Fx?mailto=contact%40example.invalid",
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?id=1&email=contact%40example.invalid",
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=contact%40example.invalid",
    ]
    written = _written(directory).lower()
    assert SECRET.encode() not in written and b"alice" not in written

    # A replay with another contact address finds Bob's requests; Alice's search never matches.
    replay = RecordedTransport(directory, contact_email="bob@example.org")
    bob = HttpRequest(url="https://api.crossref.org/works/10.1000%2Fx?mailto=bob@example.org")
    assert replay.send(bob).body == ok.body
    with pytest.raises(NotRecorded):
        replay.send(
            HttpRequest(
                url="https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=bob@example.org"
            )
        )


@pytest.fixture
def database() -> Iterator[Database]:
    url = os.environ.get("KITTUM_DATABASE_URL")
    if not url:
        pytest.skip("KITTUM_DATABASE_URL is not set")
    db = Database(url, max_size=2)
    try:
        yield db
    finally:
        db.close()


@pytest.mark.postgres
def test_a_replay_answers_in_the_recorded_order_with_the_bytes_the_live_run_saw(
    database: Database, tmp_path: Path
) -> None:
    """Unpaywall answers 503 twice, so the first lookup gives up; asked again, it answers, with
    a body that names the contact address. For another DOI the connection fails before it answers.
    The replay gives up, fails, and answers in the same way, with the same bytes and the same hash
    as the live run."""
    directory = tmp_path / "sources"
    echo = json.dumps({"doi": "10.1186/s12916-026-04943-4", "email": ALICE}).encode()
    live = Sources(
        {
            UNPAYWALL.split("?")[0]: [
                response(503),
                response(503),
                response(200, echo, (("Content-Type", "application/json"),)),
            ],
            UNREACHABLE.split("?")[0]: [
                httpx2.ConnectError(f"connection refused for {UNREACHABLE}"),
                response(200, b"{}", (("Content-Type", "application/json"),)),
            ],
        }
    )
    clock = FixedClock(datetime(2026, 10, 28, 9, tzinfo=UTC))
    policy = SourcePolicy(
        source="unpaywall", requests_per_second=100, concurrency=2, terms_url="https://x.test"
    )

    def gateway(transport: RecordingTransport | RecordedTransport) -> SourceGateway:
        return SourceGateway(
            database=database,
            transport=transport,
            artifacts=MemoryStore(),
            policies={"unpaywall": policy},
            clock=clock,
            buckets="memory",
            cache="memory",
            max_attempts=2,
            sleep=lambda _seconds: None,
        )

    def session(transport: RecordingTransport | RecordedTransport, contact: str) -> HttpResponse:
        sources = gateway(transport)
        url, unreachable = UNPAYWALL.replace(ALICE, contact), UNREACHABLE.replace(ALICE, contact)
        with pytest.raises(SourceUnavailable):
            sources.fetch("unpaywall", HttpRequest(url=url))
        with pytest.raises(httpx2.ConnectError):
            sources.fetch("unpaywall", HttpRequest(url=unreachable))
        assert sources.fetch("unpaywall", HttpRequest(url=unreachable)).body == b"{}"
        return sources.fetch("unpaywall", HttpRequest(url=url))

    answered = session(RecordingTransport(live, directory, contact_email=ALICE), ALICE)
    assert answered.status == 200 and len(live.sent) == 5
    # A failure is recorded by its kind alone: its message quoted the address.
    assert b"alice" not in _written(directory).lower()
    # The live run gets the bytes the recording holds: the address is the placeholder.
    assert json.loads(answered.body)["email"] == CONTACT_PLACEHOLDER
    assert answered.body_hash == sha256_hex(answered.body)

    replay = RecordedTransport(directory, contact_email="bob@example.org")
    replayed = session(replay, "bob@example.org")
    assert (replayed.status, replayed.body, replayed.body_hash) == (
        200,
        answered.body,
        answered.body_hash,
    )
    # The live run asked three times; a fourth has nothing left to answer it.
    with pytest.raises(NotRecorded, match="no recorded response 4"):
        replay.send(HttpRequest(url=UNPAYWALL.replace(ALICE, "bob@example.org")))
