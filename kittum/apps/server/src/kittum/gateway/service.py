"""The gateway implementation: throttling, retries, budgets, and the response cache."""

import base64
import random
import re
import threading
import time
import zlib
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, cast

import stamina
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb
from pyrate_limiter import AbstractBucket, Duration, InMemoryBucket, PostgresBucket, Rate, RateItem

from kittum.core import Clock, SystemClock, canonical_json, sha256_hex
from kittum.gateway.errors import (
    GatewayError,
    SourceBudgetExhausted,
    SourceUnavailable,
    UnknownSource,
)
from kittum.gateway.policies import default_policies
from kittum.gateway.types import (
    HttpRequest,
    HttpResponse,
    SourcePolicy,
    Transport,
    effective_request,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping

    from kittum.db import Database, Tx
    from kittum.storage import ArtifactStore

MAX_ATTEMPTS = 5
"""Attempts per request before the gateway gives up."""

RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})

_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 8.0
_MAX_REQUEST_WAIT_MS = 60_000
_CONCURRENCY_POLL_SECONDS = 0.05
_MAX_COOLDOWN_SLEEP_SECONDS = 60.0
_DEFAULT_POLICIES = default_policies()


class _Retryable(Exception):
    """A response worth another attempt. Carries the attempt number for jitter."""

    def __init__(self, status: int, attempt: int, retry_after: float | None) -> None:
        super().__init__(f"status {status}")
        self.status = status
        self.attempt = attempt
        self.retry_after = retry_after


class _SharedPool:
    """Lets pyrate-limiter keep its bucket on the caller's `Database` pool."""

    def __init__(self, database: Database) -> None:
        self._database = database

    @property
    def closed(self) -> bool:
        return False

    @contextmanager
    def connection(self) -> Generator[Tx]:
        with self._database.transaction() as tx:
            # pyrate-limiter indexes rows positionally (`row[0]`); our shared connection usually
            # yields dict rows. The bucket only uses this connection, so swap and restore.
            connection = cast("Any", tx)
            previous = connection.row_factory
            connection.row_factory = tuple_row
            try:
                yield tx
            finally:
                connection.row_factory = previous

    def close(self) -> None:
        """The database owns the pool, so a bucket must never close it."""


def request_hash(request: HttpRequest) -> str:
    """The cache and attempt key: SHA-256 over the request's canonical JSON."""
    request = effective_request(request)
    body = None if request.body is None else base64.b64encode(request.body).decode("ascii")
    payload = {
        "method": request.method.upper(),
        "url": request.url,
        "params": sorted(request.params),
        "headers": sorted((name.lower(), value) for name, value in request.headers),
        "body": body,
    }
    return sha256_hex(canonical_json(payload))


def _rates(policy: SourcePolicy) -> list[Rate]:
    """Translate a per-second rate into pyrate-limiter's window form.

    Rates of one per second or more use a one-second window; slower sources use ten seconds, so
    the integer limit never rounds up above the published rate.
    """
    if policy.requests_per_second >= 1:
        limit = max(1, int(policy.requests_per_second))
        interval = Duration.SECOND
    else:
        limit = max(1, int(policy.requests_per_second * 10))
        interval = 10 * Duration.SECOND
    return [Rate(limit=limit, interval=interval)]


def _table_name(source: str) -> str:
    return "gateway_" + re.sub(r"[^a-z0-9]+", "_", source.lower()).strip("_")


def _advisory_key(source: str) -> int:
    """A stable int32 for `pg_try_advisory_xact_lock`, equal in every process."""
    return zlib.crc32(source.encode()) & 0x7FFFFFFF


def _seconds(value: str | None, now: datetime) -> float | None:
    """Parse `Retry-After`: delay-seconds or an HTTP date."""
    if value is None:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now).total_seconds())


def _reset_seconds(value: str | None, now: datetime) -> float | None:
    """Parse a rate-limit reset, which is an epoch timestamp or a delay."""
    if value is None:
        return None
    try:
        number = float(value.strip())
    except ValueError:
        return _seconds(value, now)
    if number > 1_000_000_000:
        return max(0.0, number - now.timestamp())
    return max(0.0, number)


class SourceGateway:
    """Fetches from one source at a time, within its policy and budget.

    Limits live in pyrate-limiter buckets: Postgres tables in production, so every worker shares
    them, and in-memory buckets when `buckets="memory"` (tests). Daily budgets and every attempt
    are rows in Postgres. Responses are cached content-addressed in the `ArtifactStore`.
    """

    def __init__(
        self,
        *,
        database: Database,
        transport: Transport,
        artifacts: ArtifactStore,
        policies: Mapping[str, SourcePolicy] | None = None,
        clock: Clock | None = None,
        buckets: str = "postgres",
        max_attempts: int = MAX_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if buckets not in {"postgres", "memory"}:
            raise ValueError("buckets must be 'postgres' or 'memory'")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._database = database
        self._transport = transport
        self._artifacts = artifacts
        self._policies = dict(policies) if policies is not None else dict(_DEFAULT_POLICIES)
        self._clock = clock or SystemClock()
        self._bucket_backend = buckets
        self._sleep = sleep

        self._pool = _SharedPool(database)
        self._buckets: dict[str, AbstractBucket] = {}
        self._buckets_lock = threading.Lock()
        self._retrying = stamina.RetryingCaller(attempts=max_attempts, timeout=None)

    # -- public surface -------------------------------------------------------------------

    def fetch(self, source: str, request: HttpRequest, *, use_cache: bool = True) -> HttpResponse:
        """Return the response, from the cache when possible, else from the source.

        `use_cache=False` is for a new execution of a query, such as an update search: it skips
        the cache read but still stores the fresh response, so later replays see the newest one.
        """
        policy = self._policy(source)
        key = request_hash(request)
        if use_cache:
            cached = self._cached(key)
            if cached is not None:
                return cached

        attempts = [0]
        try:
            response = self._retrying(
                self._retry_delay, self._attempt, source, policy, request, key, attempts
            )
        except _Retryable as error:
            raise SourceUnavailable(source, error.status, attempts[0]) from error

        if 200 <= response.status < 300:
            self._remember(source, key, response)
        return response

    def close(self) -> None:
        with self._buckets_lock:
            for bucket in self._buckets.values():
                bucket.close()
            self._buckets.clear()

    # -- one attempt ----------------------------------------------------------------------

    def _attempt(
        self,
        source: str,
        policy: SourcePolicy,
        request: HttpRequest,
        key: str,
        attempts: list[int],
    ) -> HttpResponse:
        attempts[0] += 1
        number = attempts[0]
        # Take the slot first: a request queued behind another must see the cooldown that request
        # may set, and must not spend a rate token or budget while it waits.
        with self._concurrency(policy):
            self._wait_for_cooldown(source)
            self._acquire(source, policy)
            self._consume_budget(policy)
            started = self._clock.now()
            try:
                response = self._transport.send(request)
            except Exception as error:
                self._record_attempt(
                    source, key, number, None, self._elapsed_ms(started), str(error)
                )
                raise

            self._record_attempt(source, key, number, response.status, self._elapsed_ms(started))
            self._observe(source, response)
            if response.status in RETRYABLE_STATUSES:
                retry_after = _seconds(self._header(response, "retry-after"), self._clock.now())
                raise _Retryable(response.status, number, retry_after)
            return response

    def _retry_delay(self, error: Exception) -> bool | float:
        """Stamina hook: full jitter, or let the source cooldown handle `Retry-After`."""
        if not isinstance(error, _Retryable):
            return False
        if error.retry_after is not None:
            return 0.0
        ceiling = min(_BACKOFF_CAP, _BACKOFF_BASE * 2 ** (error.attempt - 1))
        return random.uniform(0.0, ceiling)

    def _elapsed_ms(self, started: datetime) -> int:
        return max(0, int((self._clock.now() - started).total_seconds() * 1000))

    # -- throttling -----------------------------------------------------------------------

    def _acquire(self, source: str, policy: SourcePolicy) -> None:
        bucket = self._bucket(policy)
        for _ in range(1_000):
            self._wait_for_cooldown(source)
            now = int(self._clock.now().timestamp() * 1000)
            cast("int", bucket.leak(now))
            item = RateItem(source, now)
            if cast("bool", bucket.put(item)):
                return
            wait_ms = cast("int", bucket.waiting(item))
            self._sleep(min(max(wait_ms, 1), _MAX_REQUEST_WAIT_MS) / 1000)
        raise GatewayError(f"could not acquire a rate-limit token for {source}")

    def _bucket(self, policy: SourcePolicy) -> AbstractBucket:
        with self._buckets_lock:
            bucket = self._buckets.get(policy.source)
            if bucket is None:
                rates = _rates(policy)
                if self._bucket_backend == "memory":
                    bucket = InMemoryBucket(rates)
                else:
                    bucket = PostgresBucket(
                        cast("Any", self._pool), _table_name(policy.source), rates
                    )
                self._buckets[policy.source] = bucket
            return bucket

    @contextmanager
    def _concurrency(self, policy: SourcePolicy) -> Generator[None]:
        """Hold one of the source's Postgres advisory slots for the duration of the send.

        The lock lives in the transaction, so every process shares the cap and a worker that dies
        mid-request releases its slot.
        """
        key = _advisory_key(policy.source)
        slots = max(1, policy.concurrency)
        while True:
            for slot in range(slots):
                with self._database.transaction() as tx:
                    row = tx.execute(
                        "select pg_try_advisory_xact_lock(%s, %s) as locked", (key, slot)
                    ).fetchone()
                    if row is not None and row["locked"]:
                        yield
                        return
            self._sleep(_CONCURRENCY_POLL_SECONDS)

    def _wait_for_cooldown(self, source: str) -> None:
        """Pause until the persisted deadline passes, re-checking after every bounded sleep."""
        while True:
            with self._database.transaction() as tx:
                row = tx.execute(
                    "select until from gateway_cooldown where source = %s", (source,)
                ).fetchone()
            if row is None:
                return
            remaining = (cast("datetime", row["until"]) - self._clock.now()).total_seconds()
            if remaining <= 0:
                return
            self._sleep(min(remaining, _MAX_COOLDOWN_SLEEP_SECONDS))

    def _observe(self, source: str, response: HttpResponse) -> None:
        """Slow down before the source's own headers say we must."""
        now = self._clock.now()
        retry_after = _seconds(self._header(response, "retry-after"), now)
        if retry_after is not None:
            self._cool(source, retry_after)
            return

        remaining = self._header(response, "x-ratelimit-remaining") or self._header(
            response, "ratelimit-remaining"
        )
        reset = self._header(response, "x-ratelimit-reset") or self._header(
            response, "ratelimit-reset"
        )
        if remaining is None or reset is None:
            return
        try:
            left = int(remaining)
        except ValueError:
            return
        if left <= 0:
            seconds = _reset_seconds(reset, now)
            if seconds is not None:
                self._cool(source, seconds)

    def _cool(self, source: str, seconds: float) -> None:
        until = self._clock.now() + timedelta(seconds=seconds)
        with self._database.transaction() as tx:
            tx.execute(
                "insert into gateway_cooldown (source, until) values (%s, %s) "
                "on conflict (source) do update set"
                " until = greatest(gateway_cooldown.until, excluded.until)",
                (source, until),
            )

    @staticmethod
    def _header(response: HttpResponse, name: str) -> str | None:
        for key, value in response.headers:
            if key.lower() == name:
                return value
        return None

    # -- budgets --------------------------------------------------------------------------

    def _consume_budget(self, policy: SourcePolicy) -> None:
        if policy.daily_budget is None:
            return
        day = self._clock.now().date()
        with self._database.transaction() as tx:
            row = tx.execute(
                "insert into gateway_budget (source, day, used) "
                "select %s, %s, 1 where %s > 0 "
                "on conflict (source, day) do update set used = gateway_budget.used + 1 "
                "where gateway_budget.used < %s returning used",
                (policy.source, day, policy.daily_budget, policy.daily_budget),
            ).fetchone()
        if row is None:
            raise SourceBudgetExhausted(policy.source, day, policy.daily_budget)

    # -- attempts -------------------------------------------------------------------------

    def _record_attempt(
        self,
        source: str,
        key: str,
        number: int,
        status: int | None,
        duration_ms: int,
        error: str | None = None,
    ) -> None:
        with self._database.transaction() as tx:
            tx.execute(
                "insert into gateway_attempt"
                " (source, request_hash, attempt, status, duration_ms, error, recorded_at)"
                " values (%s, %s, %s, %s, %s, %s, %s)",
                (source, key, number, status, duration_ms, error, self._clock.now()),
            )

    # -- cache ----------------------------------------------------------------------------

    def _policy(self, source: str) -> SourcePolicy:
        policy = self._policies.get(source)
        if policy is None:
            raise UnknownSource(source)
        return policy

    def _cached(self, key: str) -> HttpResponse | None:
        with self._database.transaction() as tx:
            row = tx.execute(
                "select status, headers, body_hash from gateway_cache where request_hash = %s",
                (key,),
            ).fetchone()
        if row is None:
            return None

        body_hash = cast("str", row["body_hash"])
        try:
            body = self._artifacts.get(body_hash)
        except KeyError, FileNotFoundError, ValueError:
            # The index outlived its artifact; drop the entry and fetch again.
            with self._database.transaction() as tx:
                tx.execute("delete from gateway_cache where request_hash = %s", (key,))
            return None

        headers = tuple((str(name), str(value)) for name, value in row["headers"])
        return HttpResponse(
            status=cast("int", row["status"]),
            headers=headers,
            body=body,
            body_hash=body_hash,
            from_cache=True,
        )

    def _remember(self, source: str, key: str, response: HttpResponse) -> None:
        body_hash = self._artifacts.put(response.body)
        headers = [[name, value] for name, value in response.headers]
        with self._database.transaction() as tx:
            tx.execute(
                "insert into gateway_cache"
                " (request_hash, source, status, headers, body_hash, stored_at)"
                " values (%s, %s, %s, %s, %s, %s)"
                " on conflict (request_hash) do update set source = excluded.source,"
                " status = excluded.status, headers = excluded.headers,"
                " body_hash = excluded.body_hash, stored_at = excluded.stored_at",
                (key, source, response.status, Jsonb(headers), body_hash, self._clock.now()),
            )


__all__ = ["MAX_ATTEMPTS", "RETRYABLE_STATUSES", "SourceGateway", "request_hash"]
