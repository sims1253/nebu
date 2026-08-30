"""HTTP routers for the Janus API."""

from janus.api.reviews import router as reviews_router

__all__ = [
    "reviews_router",
]
