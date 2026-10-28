"""HTTP transport for the reviewer workbench.

Routes stay thin: decode the request, call one module function or service method, map errors.
Each module that exposes HTTP adds `api/routes/<module>.py` with a `router` and registers it in
`api/routes/__init__.py`. The OpenAPI document is the contract for the TypeScript client
(`pnpm contracts:generate`).
"""

from contextlib import asynccontextmanager
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from kittum.app import AppContext
from kittum.core import Record
from kittum.settings import Settings

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


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
        if not hasattr(app.state, "context"):
            app.state.context = context or AppContext.from_settings(Settings.from_env())
        try:
            yield
        finally:
            if owned:
                app.state.context.db.close()

    app = FastAPI(title="Kittum", version="0.0.1", lifespan=lifespan)
    if context is not None:
        app.state.context = context

    @app.get("/api/health", operation_id="getHealth")
    def health() -> Health:
        return Health(status="ok", version=app.version)

    from kittum.api.routes import ROUTERS

    for router in ROUTERS:
        app.include_router(router)
    web_dist = (context.settings if context else Settings.from_env()).web_dist
    if web_dist is not None:
        _serve_workbench(app, web_dist)
    return app


def _serve_workbench(app: FastAPI, dist: Path) -> None:
    """Serve the built workbench from the same process: its files, and `index.html` for every
    other path outside `/api`, so the client router handles deep links."""
    root = dist.resolve()
    index = root / "index.html"
    if not index.is_file():
        raise RuntimeError(f"{dist} has no index.html; build the workbench with `pnpm build`")

    @app.get("/{path:path}", include_in_schema=False)
    def workbench(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(status_code=404)
        target = (root / path).resolve()
        if path and target.is_file() and target.is_relative_to(root):
            immutable = target.parent.name == "assets"
            headers = {"Cache-Control": "max-age=31536000, immutable"} if immutable else None
            return FileResponse(target, headers=headers)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


__all__ = ["AppContext", "Context", "Health", "create_app", "get_context"]
