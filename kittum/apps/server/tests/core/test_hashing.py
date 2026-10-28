import uuid
from datetime import UTC, datetime

from kittum.core import Record, canonical_json, content_hash, new_id


def test_canonical_json_sorts_keys_and_drops_whitespace() -> None:
    assert canonical_json({"b": 1, "a": [True, None]}) == b'{"a":[true,null],"b":1}'


def test_models_hash_like_their_json() -> None:
    class Point(Record):
        x: int
        when: datetime

    p = Point(x=1, when=datetime(2026, 10, 28, tzinfo=UTC))
    assert canonical_json(p) == b'{"when":"2026-10-28T00:00:00Z","x":1}'
    assert content_hash(p, domain="t") == content_hash(p.model_dump(mode="json"), domain="t")


def test_domain_tag_separates_equal_values() -> None:
    assert content_hash({"a": 1}, domain="x") != content_hash({"a": 1}, domain="y")


def test_ids_are_uuid7_and_sort_by_time() -> None:
    a, b = new_id(), new_id()
    assert a.version == 7
    assert isinstance(a, uuid.UUID)
    assert a < b
