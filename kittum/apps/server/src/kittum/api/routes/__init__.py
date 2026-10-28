"""Routers, one file per module. Add yours to ROUTERS, keeping the list sorted."""

from typing import TYPE_CHECKING

from kittum.api.routes import corpus, protocol, search

if TYPE_CHECKING:
    from fastapi import APIRouter

ROUTERS: list[APIRouter] = [corpus.router, protocol.router, search.router]
