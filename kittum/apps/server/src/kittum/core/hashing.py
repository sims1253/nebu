"""Content hashes over canonical JSON.

Every hash that the ledger, the cache, or a manifest stores comes from here, so two modules can
never disagree about how a value is serialized.
"""

import hashlib
from typing import Annotated, Any

import rfc8785
from pydantic import BaseModel, StringConstraints

type Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> bytes:
    """Serialize a value as RFC 8785 canonical JSON.

    Pydantic models are dumped in JSON mode first, so UUIDs, dates, and decimals become strings.
    """
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return rfc8785.dumps(value)


def content_hash(value: Any, *, domain: str) -> str:
    """Hash a value under a domain tag, so equal JSON in different roles never collides."""
    return sha256_hex(domain.encode() + b"\x00" + canonical_json(value))
