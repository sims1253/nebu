"""Shared building blocks that every module may use.

Identifiers (Kittum's own, and the canonical form of a DOI), content hashes, canonical JSON, the
clock, and the base model class. Nothing here knows about the review domain.
"""

from kittum.core.clock import Clock, FixedClock, SystemClock
from kittum.core.doi import canonical_doi
from kittum.core.errors import KittumError, NotFoundError
from kittum.core.hashing import Sha256, canonical_json, content_hash, sha256_hex
from kittum.core.ids import Id, new_id
from kittum.core.model import Record
from kittum.core.text import Span, find_quote

__all__ = [
    "Clock",
    "FixedClock",
    "Id",
    "KittumError",
    "NotFoundError",
    "Record",
    "Sha256",
    "Span",
    "SystemClock",
    "canonical_doi",
    "canonical_json",
    "content_hash",
    "find_quote",
    "new_id",
    "sha256_hex",
]
