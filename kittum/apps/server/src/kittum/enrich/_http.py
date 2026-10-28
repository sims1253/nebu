"""One JSON request through the gateway, with failures that the caller can fall back from."""

import json

from kittum.gateway import Gateway, HttpRequest


class SourceUnavailable(Exception):
    """A source did not answer. The "first healthy source" policy moves to the next one."""

    def __init__(self, source: str, reason: str) -> None:
        super().__init__(f"{source}: {reason}")
        self.source = source
        self.reason = reason


def fetch_json(
    gateway: Gateway,
    source: str,
    url: str,
    *,
    headers: tuple[tuple[str, str], ...] = (),
) -> object | None:
    """Fetch one URL and decode its JSON body. `None` means the source has no such record."""
    request = HttpRequest(
        url=url,
        headers=(("Accept", "application/json"), *headers),
    )
    try:
        response = gateway.fetch(source, request)
    except Exception as exc:
        raise SourceUnavailable(source, f"{type(exc).__name__}: {exc}") from exc
    if response.status == 404:
        return None
    if response.status < 200 or response.status >= 300:
        raise SourceUnavailable(source, f"HTTP {response.status}")
    try:
        return json.loads(response.body)
    except ValueError as exc:
        raise SourceUnavailable(source, "response is not JSON") from exc


def chunks[T](items: list[T], size: int) -> list[list[T]]:
    if size < 1:
        raise ValueError("batch size must be positive")
    return [items[i : i + size] for i in range(0, len(items), size)]
