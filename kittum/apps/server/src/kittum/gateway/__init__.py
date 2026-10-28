"""All outbound HTTP (docs/sources.md, "How Kittum spreads load").

Per-source rate buckets persisted in Postgres, a safety margin under every published limit,
`Retry-After` handling, retries with jitter, daily budgets, and a content-addressed response
cache. A rerun of a frozen strategy makes no network calls. `RecordedTransport` answers from
responses recorded once from the real sources, for a stack that must stay offline.

Owner: WP-04.

    transport = HttpxTransport(contact_email=settings.contact_email)
    gateway = SourceGateway(
        database=db,
        transport=transport,
        artifacts=FilesystemStore(settings.artifact_dir),
        policies=default_policies(contact_email=settings.contact_email),
        clock=clock,
    )
    response = gateway.fetch("pubmed", HttpRequest(url=..., params=...))
"""

from kittum.gateway.errors import (
    GatewayError,
    SourceBudgetExhausted,
    SourceUnavailable,
    UnknownSource,
)
from kittum.gateway.policies import MARGIN, default_policies
from kittum.gateway.recorded import NotRecorded, RecordedTransport
from kittum.gateway.service import MAX_ATTEMPTS, SourceGateway, request_hash
from kittum.gateway.transport import HttpxTransport, user_agent
from kittum.gateway.types import Gateway, HttpRequest, HttpResponse, SourcePolicy, Transport

__all__ = [
    "MARGIN",
    "MAX_ATTEMPTS",
    "Gateway",
    "GatewayError",
    "HttpRequest",
    "HttpResponse",
    "HttpxTransport",
    "NotRecorded",
    "RecordedTransport",
    "SourceBudgetExhausted",
    "SourceGateway",
    "SourcePolicy",
    "SourceUnavailable",
    "Transport",
    "UnknownSource",
    "default_policies",
    "request_hash",
    "user_agent",
]
