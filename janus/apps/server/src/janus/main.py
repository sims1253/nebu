"""FastAPI application entry point for the Janus backend."""

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from janus.api.reviews import router as reviews_router
from janus.pipeline.review_pipeline import get_review_pipeline

# Load apps/server/.env (runtime config). Does not override variables already
# present in the environment, so explicit env settings and test monkeypatches
# always win.
load_dotenv()

_log_level = os.environ.get("JANUS_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    """Health check response schema."""

    status: str
    version: str


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Build the review pipeline at startup so the configured stores open (and
    fail, if misconfigured) before the first request, not during it."""
    get_review_pipeline()
    logger.info("Janus document comparison backend starting up")
    yield
    logger.info("Janus document comparison backend shutting down")


app = FastAPI(
    title="Janus Document Comparison API",
    description="Compile comparison specifications, extract grounded PDF values, and review comparisons.",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS origins for the local web app dev server.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3002",
        "http://127.0.0.1:3002",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(reviews_router)


@app.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Report service health and version."""
    return HealthResponse(status="healthy", version="2.0.0")


@app.get("/")
async def root() -> dict[str, str]:
    """Report the API name and version."""
    return {"name": "Janus Document Comparison API", "version": "2.0.0"}
