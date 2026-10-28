"""HTTP transport for the workbench.

Routes stay thin: decode the request, call one module function or service method, map errors.
Each module that exposes HTTP adds `api/routes/<module>.py` with a `router` and registers it in
`api/routes/__init__.py`. The OpenAPI document is the contract for the TypeScript client
(`pnpm contracts:generate`).
"""

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, FastAPI, Request

from kittum.core import Clock, Record, SystemClock
from kittum.db import Database
from kittum.settings import Settings
from kittum.storage import ArtifactStore, FilesystemStore

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from kittum.ledger import Ledger


@dataclass
class AppContext:
    """What routes may use. Built once per process; tests build their own."""

    settings: Settings
    db: Database
    artifacts: ArtifactStore
    clock: Clock = field(default_factory=SystemClock)
    ledger: Ledger | None = None  # None until the Postgres ledger (WP-01) exists

    @classmethod
    def from_settings(cls, settings: Settings) -> AppContext:
        return cls(
            settings=settings,
            db=Database(settings.database_url),
            artifacts=FilesystemStore(settings.artifact_dir),
        )


def get_context(request: Request) -> AppContext:
    return request.app.state.context


Context = Annotated[AppContext, Depends(get_context)]


class Health(Record):
    status: str
    version: str


def create_app(context: AppContext | None = None) -> FastAPI:
    """Build the app. Without a context, one is built from the environment at startup."""
    from kittum.profiles import load_profiles

    load_profiles()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        owned = context is None
        app.state.context = context or AppContext.from_settings(Settings.from_env())
        try:
            yield
        finally:
            if owned:
                app.state.context.db.close()

    app = FastAPI(title="Kittum", version="0.0.1", lifespan=lifespan)

    @app.get("/api/health", operation_id="getHealth")
    def health() -> Health:
        return Health(status="ok", version=app.version)

    from kittum.api.routes import ROUTERS

    for router in ROUTERS:
        app.include_router(router)
    return app


__all__ = ["AppContext", "Context", "Health", "create_app", "get_context"]
