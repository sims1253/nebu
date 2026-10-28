"""Routers, one file per module. Add yours to ROUTERS, keeping the list sorted."""

from typing import TYPE_CHECKING

from kittum.api.routes import (
    corpus,
    documents,
    extraction,
    ledger,
    projects,
    protocol,
    report,
    screening,
    search,
)

if TYPE_CHECKING:
    from fastapi import APIRouter

ROUTERS: list[APIRouter] = [
    corpus.router,
    documents.router,
    extraction.router,
    ledger.router,
    projects.router,
    protocol.router,
    report.router,
    screening.router,
    search.router,
]
