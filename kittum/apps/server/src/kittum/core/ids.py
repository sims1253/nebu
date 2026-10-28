"""Identifiers.

Kittum uses UUIDv7 everywhere, so identifiers sort by creation time. Postgres generates the same
kind with `uuidv7()`.
"""

import uuid

type Id = uuid.UUID


def new_id() -> Id:
    return uuid.uuid7()
