from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import uuid

    from kittum.db import Tx


@pytest.fixture
def project_id(pg: Tx) -> uuid.UUID:
    row = pg.execute(
        "insert into project (name, policy_version) values ('search-test', 'v1') returning id"
    ).fetchone()
    assert row
    return row["id"]


@pytest.fixture
def actor_id(pg: Tx) -> uuid.UUID:
    row = pg.execute(
        "insert into actor (identity_ref, kind) values ('reviewer@example.org', 'human') "
        "returning id"
    ).fetchone()
    assert row
    return row["id"]
