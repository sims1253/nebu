"""Routers, one file per module. Add yours to ROUTERS, keeping the list sorted."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import APIRouter

ROUTERS: list[APIRouter] = []
