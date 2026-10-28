"""The base class for values that cross module boundaries."""

from pydantic import BaseModel, ConfigDict


class Record(BaseModel):
    """An immutable value. Unknown fields are rejected, so typos fail loudly."""

    model_config = ConfigDict(frozen=True, extra="forbid")
