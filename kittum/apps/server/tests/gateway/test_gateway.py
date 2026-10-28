import os
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
import stamina

from kittum.core import FixedClock
from kittum.gateway import (
    HttpRequest,
    HttpResponse,
    SourceBudgetExhausted,
    SourceGateway,
    SourcePolicy,
    SourceUnavailable,
    Transport,
)
from kittum.gateway.testing import FakeTransport, response
from kittum.storage import MemoryStore

if TYPE_CHECKING:
    from kittum.db import Database

pytestmark = pytest.mark.postgres

START = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)


class RecordingSleep:
    """A sleep that advances the test clock and remembers how long it was asked to wait."""

    def __init__(self, clock: FixedClock) -> None:
        self.clock = clock
        self.durations: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.durations.append(seconds)
        self.clock.advance(timedelta(seconds=seconds))


def _policy(
    source: str,
    requests_per_second: float = 100.0,
    *,
    concurrency: int = 2,
    daily_budget: int | None = None,
) -> SourcePolicy:
    return SourcePolicy(
        source=source,
        requests_per_second=requests_per_second,
        concurrency=concurrency,
        daily_budget=daily_budget,
        terms_url="https://example.org/terms",
    )


def _gateway(
    database: Database,
    transport: Transport,
    *,
    policies: list[SourcePolicy],
    clock: FixedClock,
    sleep: RecordingSleep,
    store: MemoryStore | None = None,
    buckets: str = "postgres",
    max_attempts: int = 5,
) -> SourceGateway:
    return SourceGateway(
        database=database,
        transport=transport,
        artifacts=store or MemoryStore(),
        policies={policy.source: policy for policy in policies},
        clock=clock,
        buckets=buckets,
        max_attempts=max_attempts,
        sleep=sleep,
    )


def _request(url: str) -> HttpRequest:
    return HttpRequest(url=url)


def _attempts(database: Database) -> list[dict[str, object]]:
    with database.transaction() as tx:
        return tx.execute(
            "select source, attempt, status from gateway_attempt order by id"
        ).fetchall()


def _budget(database: Database, source: str) -> int:
    with database.transaction() as tx:
        row = tx.execute("select used from gateway_budget where source = %s", (source,)).fetchone()
    assert row is not None
    return int(row["used"])


def test_retry_after_then_success_records_both_attempts(database: Database) -> None:
    clock = FixedClock(START)
    sleep = RecordingSleep(clock)
    transport = FakeTransport(
        sequence=[
            response(429, b"slow down", (("Retry-After", "1"),)),
            response(200, b"abstract"),
        ]
    )
    gateway = _gateway(
        database,
        transport,
        policies=[_policy("pubmed")],
        clock=clock,
        sleep=sleep,
        buckets="memory",
    )

    with stamina.set_testing(True, attempts=10, cap=True):
        result = gateway.fetch("pubmed", _request("https://eutils.ncbi.nlm.nih.gov/esearch"))

    assert result.status == 200
    assert result.body == b"abstract"
    assert len(transport.sent) == 2
    assert len(sleep.durations) == 1
    assert sleep.durations[0] == pytest.approx(1.0)
    assert [(row["attempt"], row["status"]) for row in _attempts(database)] == [
        (1, 429),
        (2, 200),
    ]


def test_five_failures_give_up_with_a_clear_error(database: Database) -> None:
    clock = FixedClock(START)
    transport = FakeTransport(sequence=[response(503) for _ in range(6)])
    gateway = _gateway(
        database,
        transport,
        policies=[_policy("europepmc")],
        clock=clock,
        sleep=RecordingSleep(clock),
        buckets="memory",
    )

    with (
        stamina.set_testing(True, attempts=10, cap=True),
        pytest.raises(SourceUnavailable, match="europepmc still failing after 5 attempts"),
    ):
        gateway.fetch(
            "europepmc", _request("https://www.ebi.ac.uk/europepmc/webservices/rest/search")
        )

    assert len(transport.sent) == 5
    attempts = _attempts(database)
    assert [row["attempt"] for row in attempts] == [1, 2, 3, 4, 5]
    assert {row["status"] for row in attempts} == {503}


def test_repeated_request_is_served_from_the_cache(database: Database) -> None:
    clock = FixedClock(START)
    url = "https://api.crossref.org/works/10.1000/xyz"
    body = b'{"status": "ok", "message": {"DOI": "10.1000/xyz"}}'
    transport = FakeTransport(
        responses={url: body}, headers=(("Content-Type", "application/json"),)
    )
    store = MemoryStore()
    gateway = _gateway(
        database,
        transport,
        policies=[_policy("crossref")],
        clock=clock,
        sleep=RecordingSleep(clock),
        store=store,
        buckets="memory",
    )
    request = HttpRequest(url=url, params=(("mailto", "librarian@example.org"),))

    first = gateway.fetch("crossref", request)
    second = gateway.fetch("crossref", request)

    assert len(transport.sent) == 1
    assert not first.from_cache
    assert second.from_cache
    assert second.body == body
    assert second.status == 200
    assert ("Content-Type", "application/json") in second.headers
    assert store.exists(first.body_hash)
    assert len(_attempts(database)) == 1

    bypass = gateway.fetch("crossref", request, use_cache=False)
    assert not bypass.from_cache
    assert len(transport.sent) == 2


def test_two_sources_have_independent_buckets(database: Database) -> None:
    clock = FixedClock(START)
    sleep = RecordingSleep(clock)
    transport = FakeTransport(
        responses={
            "https://a.example/1": b"a1",
            "https://a.example/2": b"a2",
            "https://b.example/1": b"b1",
            "https://b.example/2": b"b2",
        }
    )
    gateway = _gateway(
        database,
        transport,
        policies=[_policy("pubmed", 1.0), _policy("europepmc", 10.0)],
        clock=clock,
        sleep=sleep,
    )

    gateway.fetch("pubmed", _request("https://a.example/1"))
    gateway.fetch("europepmc", _request("https://b.example/1"))
    gateway.fetch("europepmc", _request("https://b.example/2"))
    assert sleep.durations == []

    # PubMed's one request per second is now spent, so the second call waits a second.
    gateway.fetch("pubmed", _request("https://a.example/2"))
    assert len(sleep.durations) == 1
    assert 0.9 <= sleep.durations[0] <= 1.1

    assert [(row["source"], row["attempt"]) for row in _attempts(database)] == [
        ("pubmed", 1),
        ("europepmc", 1),
        ("europepmc", 1),
        ("pubmed", 1),
    ]


def test_daily_budget_survives_a_new_gateway_instance(database: Database) -> None:
    clock = FixedClock(START)
    sleep = RecordingSleep(clock)
    policy = [_policy("unpaywall", daily_budget=2)]
    first = _gateway(
        database,
        FakeTransport(
            responses={"https://api.unpaywall.org/1": b"1", "https://api.unpaywall.org/2": b"2"}
        ),
        policies=policy,
        clock=clock,
        sleep=sleep,
        buckets="memory",
    )
    assert first.fetch("unpaywall", _request("https://api.unpaywall.org/1")).body == b"1"
    assert first.fetch("unpaywall", _request("https://api.unpaywall.org/2")).body == b"2"

    second_transport = FakeTransport(responses={"https://api.unpaywall.org/3": b"3"})
    second = _gateway(
        database,
        second_transport,
        policies=policy,
        clock=clock,
        sleep=sleep,
        buckets="memory",
    )

    with pytest.raises(SourceBudgetExhausted, match="unpaywall used its 2 requests"):
        second.fetch("unpaywall", _request("https://api.unpaywall.org/3"))

    assert second_transport.sent == []
    assert _budget(database, "unpaywall") == 2


def test_rate_limit_headers_pause_before_the_next_call(database: Database) -> None:
    clock = FixedClock(START)
    sleep = RecordingSleep(clock)
    transport = FakeTransport(
        sequence=[
            response(
                200,
                b"one",
                (("X-RateLimit-Remaining", "0"), ("X-RateLimit-Reset", "2")),
            ),
            response(200, b"two"),
        ]
    )
    gateway = _gateway(
        database,
        transport,
        policies=[_policy("openalex")],
        clock=clock,
        sleep=sleep,
        buckets="memory",
    )

    assert gateway.fetch("openalex", _request("https://api.openalex.org/1")).body == b"one"
    assert gateway.fetch("openalex", _request("https://api.openalex.org/2")).body == b"two"

    assert len(sleep.durations) == 1
    assert sleep.durations[0] == pytest.approx(2.0)


class CountingTransport:
    """A slow transport that records how many sends overlap."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def send(self, _http_request: HttpRequest) -> HttpResponse:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.1)
            return response(200, b"ok")
        finally:
            with self._lock:
                self.active -= 1


def test_concurrency_cap_serializes_a_source(database: Database) -> None:
    clock = FixedClock(START)
    transport = CountingTransport()
    gateway = _gateway(
        database,
        transport,
        policies=[_policy("pubmed", concurrency=1)],
        clock=clock,
        sleep=RecordingSleep(clock),
        buckets="memory",
    )
    failures: list[BaseException] = []

    def fetch(url: str) -> None:
        try:
            gateway.fetch("pubmed", _request(url))
        except BaseException as error:  # pragma: no cover - only on a broken gateway
            failures.append(error)

    threads = [
        threading.Thread(target=fetch, args=(f"https://eutils.ncbi.nlm.nih.gov/{i}",))
        for i in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures
    assert transport.max_active == 1


def test_a_new_execution_bypasses_the_cache_and_refreshes_it(database: Database) -> None:
    clock = FixedClock(START)
    url = "https://clinicaltrials.gov/api/v2/studies/NCT01234567"
    before, after = b'{"hasResults": false}', b'{"hasResults": true}'
    transport = FakeTransport(sequence=[response(200, before), response(200, after)])
    gateway = _gateway(
        database,
        transport,
        policies=[_policy("ctgov")],
        clock=clock,
        sleep=RecordingSleep(clock),
        store=MemoryStore(),
        buckets="memory",
    )
    request = HttpRequest(url=url)

    assert gateway.fetch("ctgov", request).body == before
    update = gateway.fetch("ctgov", request, use_cache=False)
    replay = gateway.fetch("ctgov", request)

    assert update.body == after
    assert not update.from_cache
    assert replay.from_cache
    assert replay.body == after
    assert len(transport.sent) == 2


def test_concurrency_cap_is_shared_between_gateway_instances(database: Database) -> None:
    transport = CountingTransport()

    def gateway() -> SourceGateway:
        clock = FixedClock(START)
        return _gateway(
            database,
            transport,
            policies=[_policy("crossref", concurrency=1)],
            clock=clock,
            sleep=RecordingSleep(clock),
            buckets="memory",
        )

    gateways = [gateway(), gateway()]
    barrier = threading.Barrier(2)
    failures: list[BaseException] = []

    def fetch(instance: SourceGateway, number: int) -> None:
        try:
            barrier.wait()
            instance.fetch("crossref", _request(f"https://api.crossref.org/works/{number}"))
        except BaseException as error:  # pragma: no cover - only on a broken gateway
            failures.append(error)

    threads = [
        threading.Thread(target=fetch, args=(instance, number))
        for number, instance in enumerate(gateways)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not failures
    assert transport.max_active == 1


def test_a_cooldown_is_shared_between_gateway_instances(database: Database) -> None:
    clock = FixedClock(START)
    blocked_sleep = RecordingSleep(clock)
    blocked = _gateway(
        database,
        FakeTransport(sequence=[response(429, b"slow down", (("Retry-After", "30"),))]),
        policies=[_policy("openalex")],
        clock=clock,
        sleep=blocked_sleep,
        buckets="memory",
        max_attempts=1,
    )

    with pytest.raises(SourceUnavailable):
        blocked.fetch("openalex", _request("https://api.openalex.org/a"))
    assert blocked_sleep.durations == []

    waiting_sleep = RecordingSleep(clock)
    waiting_transport = FakeTransport(responses={"https://api.openalex.org/b": b"b"})
    waiting = _gateway(
        database,
        waiting_transport,
        policies=[_policy("openalex")],
        clock=clock,
        sleep=waiting_sleep,
        buckets="memory",
    )

    assert waiting.fetch("openalex", _request("https://api.openalex.org/b")).body == b"b"
    assert sum(waiting_sleep.durations) == pytest.approx(30.0)
    assert len(waiting_transport.sent) == 1


def test_retry_after_keeps_the_full_deadline(database: Database) -> None:
    clock = FixedClock(START)
    sleep = RecordingSleep(clock)
    transport = FakeTransport(
        sequence=[
            response(429, b"slow down", (("Retry-After", "7200"),)),
            response(200, b"ok"),
        ]
    )
    gateway = _gateway(
        database,
        transport,
        policies=[_policy("openalex")],
        clock=clock,
        sleep=sleep,
        buckets="memory",
    )

    with stamina.set_testing(True, attempts=10, cap=True):
        result = gateway.fetch("openalex", _request("https://api.openalex.org/1"))

    assert result.status == 200
    assert sum(sleep.durations) == pytest.approx(7_200.0)
    assert max(sleep.durations) <= 60.0


def test_a_request_queued_for_a_slot_honours_a_cooldown_set_meanwhile(database: Database) -> None:

    import psycopg
    from psycopg.pq import TransactionStatus

    from kittum.gateway.service import _advisory_key

    clock = FixedClock(START)
    deadline = START + timedelta(seconds=30)
    holder = psycopg.connect(os.environ["KITTUM_DATABASE_URL"])
    holder.execute("select pg_advisory_xact_lock(%s, 0)", (_advisory_key("crossref"),))
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        if holder.info.transaction_status != TransactionStatus.IDLE:
            # First poll for the slot: the request holding it gets a Retry-After, then finishes.
            holder.execute(
                "insert into gateway_cooldown (source, until) values ('crossref', %s)"
                " on conflict (source) do update set until = excluded.until",
                (deadline,),
            )
            holder.commit()
        sleeps.append(seconds)
        clock.advance(timedelta(seconds=seconds))

    sent_at: list[object] = []

    class StampingTransport:
        def send(self, _request: HttpRequest) -> HttpResponse:
            sent_at.append(clock.now())
            return response(200, b"ok")

    gateway = _gateway(
        database,
        StampingTransport(),
        policies=[_policy("crossref", concurrency=1)],
        clock=clock,
        sleep=sleep,
        buckets="memory",
    )
    try:
        gateway.fetch("crossref", _request("https://api.crossref.org/works/queued"))
    finally:
        holder.close()

    assert sent_at == [sent_at[0]]
    assert sent_at[0] >= deadline


def test_concurrent_requests_to_one_source_wait_their_turn(database: Database) -> None:
    """Counting several searches at once: their rate tokens come from one bucket, and requests
    the rate refuses wait for a token instead of failing."""
    clock = FixedClock(START)
    transport = FakeTransport(
        responses={f"https://eutils.ncbi.nlm.nih.gov/{i}": b"<Count>1</Count>" for i in range(24)}
    )
    gateway = _gateway(
        database,
        transport,
        policies=[_policy("pubmed", 3.0, concurrency=6)],
        clock=clock,
        sleep=RecordingSleep(clock),
    )
    barrier = threading.Barrier(6)
    failures: list[BaseException] = []

    def count(first: int) -> None:
        try:
            barrier.wait()
            for i in range(first, 24, 6):
                gateway.fetch("pubmed", _request(f"https://eutils.ncbi.nlm.nih.gov/{i}"))
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=count, args=(n,), daemon=True) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    # A deadlock fails here rather than hanging the run.
    assert not any(thread.is_alive() for thread in threads)
    assert failures == []
    assert len(transport.sent) == 24
    assert [a["status"] for a in _attempts(database)] == [200] * 24
