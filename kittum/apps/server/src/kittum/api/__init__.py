"""HTTP transport for the workbench.

Routes stay thin: decode the request, call one module function or service method, map errors.
The OpenAPI document is the contract for the TypeScript client (`pnpm contracts:generate`).

Owner: WP-15.
"""

from fastapi import FastAPI

from kittum.core import Record


class Health(Record):
    status: str
    version: str


def create_app() -> FastAPI:
    app = FastAPI(title="Kittum", version="0.0.1")

    @app.get("/api/health", operation_id="getHealth")
    def health() -> Health:
        return Health(status="ok", version=app.version)

    return app


__all__ = ["Health", "create_app"]
