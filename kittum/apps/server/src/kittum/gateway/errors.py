"""Errors a gateway caller is expected to handle."""

from typing import TYPE_CHECKING

from kittum.core import KittumError

if TYPE_CHECKING:
    from datetime import date


class GatewayError(KittumError):
    """Base for every gateway failure."""


class UnknownSource(GatewayError):
    """No policy is configured for the requested source."""

    def __init__(self, source: str) -> None:
        super().__init__(f"no source policy for {source!r}")
        self.source = source


class SourceBudgetExhausted(GatewayError):
    """The source's daily request budget is used up; try again on the next day."""

    def __init__(self, source: str, day: date, budget: int) -> None:
        super().__init__(f"{source} used its {budget} requests for {day.isoformat()}")
        self.source = source
        self.day = day
        self.budget = budget


class SourceUnavailable(GatewayError):
    """Every attempt failed with a status worth retrying, or (with `reason`, and no status) the
    source could not be reached at all, as offline with no recorded response. `body` is the last
    response's body, when there was one: a source often says in it why it failed."""

    def __init__(
        self,
        source: str,
        status: int | None,
        attempts: int,
        reason: str | None = None,
        body: bytes | None = None,
    ) -> None:
        super().__init__(
            f"{source} is unreachable: {reason}"
            if reason
            else f"{source} still failing after {attempts} attempts (last status {status})"
        )
        self.source = source
        self.status = status
        self.attempts = attempts
        self.reason = reason
        self.body = body


__all__ = ["GatewayError", "SourceBudgetExhausted", "SourceUnavailable", "UnknownSource"]
