import re
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pymupdf
import pypdfium2
import pytest

from kittum.core import sha256_hex
from kittum.documents import (
    Box,
    DoclingParser,
    PdfiumParser,
    PyMuPDFParser,
    RawLayer,
    docling_available,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from kittum.db import Database, Tx
    from kittum.documents import Parser, ParserRecipe

FIXTURES = Path(__file__).parent / "fixtures"
LESOTHO = FIXTURES / "gerber2026-lesotho-t2d-crct.pdf"  # Table 1 on page 7: merged headers
VITAMIN_C = FIXTURES / "li2026-vitamin-c-kidney-biopsy-rct.pdf"  # Tables 4 and 5 on page 7

_PARSED: dict[tuple[str, str], RawLayer] = {}


class CachedParser:
    """Parses each PDF once per test session; the recipe is the wrapped parser's."""

    def __init__(self, inner: Parser) -> None:
        self.inner = inner
        self.recipe: ParserRecipe = inner.recipe
        self.calls = 0

    def parse(self, pdf: bytes) -> RawLayer:
        self.calls += 1
        key = (sha256_hex(pdf), self.recipe.model_dump_json())
        if key not in _PARSED:
            _PARSED[key] = self.inner.parse(pdf)
        return _PARSED[key]


@pytest.fixture(scope="session")
def pymupdf_parser() -> CachedParser:
    return CachedParser(PyMuPDFParser())


@pytest.fixture(scope="session")
def pdfium_parser() -> CachedParser:
    return CachedParser(PdfiumParser())


@pytest.fixture(scope="session")
def docling_page7() -> CachedParser:
    """Docling over page 7 only, where both fixtures have their tables (about 30 s on a CPU)."""
    if not docling_available():
        pytest.skip("the docling extra or its model weights are not installed")
    return CachedParser(DoclingParser(page_range=(7, 7)))


class SavepointDatabase:
    """A `Database` whose transactions are savepoints inside the test's rolled-back connection."""

    def __init__(self, tx: Tx) -> None:
        self._tx = tx
        tx.execute("select 1")  # open the outer transaction, so ours become savepoints

    @contextmanager
    def transaction(self) -> Iterator[Tx]:
        with self._tx.transaction():
            yield self._tx

    def close(self) -> None:
        pass


def savepoint_db(tx: Tx) -> Database:
    return cast("Database", SavepointDatabase(tx))


def new_project(tx: Tx) -> Any:
    row = tx.execute(
        "insert into project (name, policy_version) values ('wp10 test', 'v1') returning id"
    ).fetchone()
    assert row is not None
    return row["id"]


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def text_under(pdf: bytes, box: Box) -> str:
    """What PDFium (not the engine that made the box) reads inside a box.

    Probes the middle half of the box across the text direction: line boxes run from ascender to
    descender, so the tall letters of the neighbouring line reach into them.
    """
    doc = pypdfium2.PdfDocument(pdf)
    page = doc[box.page - 1]
    left, _, _, top = page.get_cropbox()
    textpage = page.get_textpage()
    x0, y0, x1, y1 = box.x0, box.y0, box.x1, box.y1
    if x1 - x0 < y1 - y0:  # a sideways line
        x0, x1 = x0 + (x1 - x0) / 4, x1 - (x1 - x0) / 4
    else:
        y0, y1 = y0 + (y1 - y0) / 4, y1 - (y1 - y0) / 4
    text = textpage.get_text_bounded(left=left + x0, bottom=top - y1, right=left + x1, top=top - y0)
    textpage.close()
    page.close()
    doc.close()
    return squash(text.replace("­", "-").replace("\x02", "-"))


def ink(png: bytes, box: Box, scale: float) -> float:
    """Share of dark pixels inside a box on a rendered page."""
    pixmap = pymupdf.Pixmap(png)
    x0, y0 = int(box.x0 * scale), int(box.y0 * scale)
    x1, y1 = int(box.x1 * scale), int(box.y1 * scale)
    dark = total = 0
    for y in range(max(y0, 0), min(y1, pixmap.height)):
        for x in range(max(x0, 0), min(x1, pixmap.width)):
            r, g, b = pixmap.pixel(x, y)[:3]
            total += 1
            dark += (r + g + b) / 3 < 128
    return dark / total if total else 0.0
