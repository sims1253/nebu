"""Routers, one file per module. Add yours to ROUTERS, keeping the list sorted."""

from typing import TYPE_CHECKING

from kittum.api.routes import corpus, documents, protocol, screening, search

if TYPE_CHECKING:
    from fastapi import APIRouter

ROUTERS: list[APIRouter] = [corpus.router, documents.router, protocol.router, screening.router, search.router]
