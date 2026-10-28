"""One JSON request through the gateway, with failures that the caller can fall back from."""

import json
from typing import TYPE_CHECKING

from kittum.gateway import Gateway, HttpRequest, SourceBudgetExhausted, source_message
from kittum.gateway import SourceUnavailable as GatewayUnavailable

if TYPE_CHECKING:
    from kittum.enrich.types import FailureKind


class SourceUnavailable(Exception):
    """A source did not answer. The "first healthy source" policy moves to the next one.

    `status` is the last HTTP status, when the source answered at all; `message` is what the
    source said about the failure in its own words, when it said anything."""

    def __init__(
        self,
        source: str,
        reason: str,
        *,
        status: int | None = None,
        message: str | None = None,
        kind: FailureKind = "http_error",
        url: str | None = None,
    ) -> None:
        super().__init__(f"{source}: {reason}")
        self.source = source
        self.reason = reason
        self.status = status
        self.message = message
        self.kind = kind
        self.url = url


def unavailable(source: str, error: Exception) -> SourceUnavailable:
    """The enrichment failure for an exception the gateway raised."""
    if isinstance(error, GatewayUnavailable):
        if error.status is None:
            return SourceUnavailable(source, str(error), kind="unreachable")
        return SourceUnavailable(
            source,
            f"HTTP {error.status} after {error.attempts} attempts",
            status=error.status,
            message=source_message(error.body),
        )
    if isinstance(error, SourceBudgetExhausted):
        return SourceUnavailable(source, str(error), kind="budget")
    return SourceUnavailable(source, f"{type(error).__name__}: {error}", kind="unreachable")


def fetch(
    gateway: Gateway,
    source: str,
    url: str,
    *,
    accept: str,
    headers: tuple[tuple[str, str], ...] = (),
) -> bytes | None:
    """Fetch one URL and return its body. `None` means the source has no such record."""
    request = HttpRequest(url=url, headers=(("Accept", accept), *headers))
    try:
        response = gateway.fetch(source, request)
    except Exception as exc:
        error = unavailable(source, exc)
        error.url = url
        raise error from exc
    if response.status == 404:
        return None
    if response.status < 200 or response.status >= 300:
        raise SourceUnavailable(
            source,
            f"HTTP {response.status}",
            status=response.status,
            message=source_message(response.body),
            url=url,
        )
    return response.body


def fetch_json(
    gateway: Gateway,
    source: str,
    url: str,
    *,
    headers: tuple[tuple[str, str], ...] = (),
) -> object | None:
    """Fetch one URL and decode its JSON body. `None` means the source has no such record."""
    body = fetch(gateway, source, url, accept="application/json", headers=headers)
    if body is None:
        return None
    try:
        return json.loads(body)
    except ValueError as exc:
        raise SourceUnavailable(source, "response is not JSON", kind="malformed", url=url) from exc


def chunks[T](items: list[T], size: int) -> list[list[T]]:
    if size < 1:
        raise ValueError("batch size must be positive")
    return [items[i : i + size] for i in range(0, len(items), size)]
