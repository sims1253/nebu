from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from kittum.storage import FilesystemStore

if TYPE_CHECKING:
    from pathlib import Path


def test_concurrent_writers_of_the_same_artifact_never_expose_partial_content(
    tmp_path: Path,
) -> None:
    store = FilesystemStore(tmp_path)
    data = b"x" * 4_000_000
    with ThreadPoolExecutor(max_workers=16) as pool:
        digests = set(pool.map(lambda _: store.put(data), range(64)))
    (digest,) = digests
    assert store.get(digest) == data
    assert not [p for p in tmp_path.rglob(".*") if p.is_file()], "temporary files left behind"
