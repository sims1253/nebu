"""Routers, one file per module. Add yours to ROUTERS, keeping the list sorted."""

from typing import TYPE_CHECKING

from kittum.api.routes import protocol

if TYPE_CHECKING:
    from fastapi import APIRouter

ROUTERS: list[APIRouter] = [protocol.router]
