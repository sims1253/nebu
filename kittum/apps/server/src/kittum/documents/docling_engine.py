"""Docling for layout and table structure, PyMuPDF for every character.

Docling's layout model gives text regions in reading order and TableFormer gives table grids with
row and column spans. Kittum keeps only that geometry: each PyMuPDF word goes to the region or
table cell that contains it, so cell text comes from the text layer, never from a model. Pages
Docling did not process fall back to the PyMuPDF layer's own order and tables.

Needs the optional `docling` extra and its model weights on disk (`docling-tools models download
layout tableformer`). The parser never downloads anything.
"""

import hashlib
import io
import os
from functools import cache
from importlib import metadata
from pathlib import Path
from typing import Any

import pymupdf

from kittum.documents.engines import pymupdf_page_input
from kittum.documents.layout import (
    ASSEMBLY_VERSION,
    Grid,
    PageInput,
    PBlock,
    PWord,
    Rect,
    assemble,
    contains,
)
from kittum.documents.model import ParserRecipe, RawLayer
from kittum.documents.tables import CellSpec, grid_from_cells
from kittum.documents.triage import triage

_LAYOUT_WEIGHTS = "docling-project--docling-layout-heron/model.safetensors"
_TABLE_WEIGHTS = (
    "docling-project--docling-models/model_artifacts/tableformer/accurate/"
    "tableformer_accurate.safetensors"
)


class DoclingUnavailable(RuntimeError):
    """The `docling` extra or its model weights are missing."""


def docling_artifacts() -> Path:
    return Path(os.environ.get("KITTUM_DOCLING_ARTIFACTS", Path.home() / ".cache/docling/models"))


def docling_available(artifacts: Path | None = None) -> bool:
    root = artifacts or docling_artifacts()
    try:
        metadata.version("docling")
    except metadata.PackageNotFoundError:
        return False
    return (root / _LAYOUT_WEIGHTS).is_file() and (root / _TABLE_WEIGHTS).is_file()


@cache
def _file_sha256(path: Path, size: int, mtime_ns: int) -> str:
    del size, mtime_ns  # part of the cache key only
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _weights_hash(path: Path) -> str:
    stat = path.stat()
    return _file_sha256(path, stat.st_size, stat.st_mtime_ns)


def _top_left(bbox: Any, page_height: float) -> Rect:
    b = bbox.to_top_left_origin(page_height)
    return (min(b.l, b.r), min(b.t, b.b), max(b.l, b.r), max(b.t, b.b))


class DoclingParser:
    """`page_range` (1-based, inclusive) limits the pages Docling reads; the rest use PyMuPDF."""

    def __init__(
        self, *, artifacts: Path | None = None, page_range: tuple[int, int] | None = None
    ) -> None:
        self._artifacts = artifacts or docling_artifacts()
        if not docling_available(self._artifacts):
            raise DoclingUnavailable(
                "install the `docling` extra and run `docling-tools models download layout tableformer`"
            )
        self._page_range = page_range
        settings = [
            ("assembly", ASSEMBLY_VERSION),
            ("text", f"pymupdf {pymupdf.VersionBind}"),
            ("table_mode", "accurate"),
            ("ocr", "off"),
        ]
        if page_range:
            settings.append(("page_range", f"{page_range[0]}-{page_range[1]}"))
        self.recipe = ParserRecipe(
            engine="docling",
            engine_version=(
                f"docling {metadata.version('docling')}, "
                f"docling-ibm-models {metadata.version('docling-ibm-models')}"
            ),
            weights=(
                ("docling-layout-heron", _weights_hash(self._artifacts / _LAYOUT_WEIGHTS)),
                ("tableformer-accurate", _weights_hash(self._artifacts / _TABLE_WEIGHTS)),
            ),
            settings=tuple(settings),
        )

    def _convert(self, pdf: bytes) -> Any:
        from docling.datamodel.base_models import DocumentStream, InputFormat
        from docling.datamodel.pipeline_options import (
            PdfPipelineOptions,
            TableFormerMode,
            TableStructureOptions,
        )
        from docling.document_converter import DocumentConverter, PdfFormatOption

        options = PdfPipelineOptions(
            artifacts_path=self._artifacts,
            do_ocr=False,
            do_table_structure=True,
            table_structure_options=TableStructureOptions(mode=TableFormerMode.ACCURATE),
        )
        converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
        )
        kwargs: dict[str, Any] = {}
        if self._page_range:
            kwargs["page_range"] = self._page_range
        result = converter.convert(
            DocumentStream(name="document.pdf", stream=io.BytesIO(pdf)), **kwargs
        )
        return result.document

    def parse(self, pdf: bytes) -> RawLayer:
        from docling_core.types.doc import ContentLayer, TableItem

        document = self._convert(pdf)
        regions: dict[int, list[tuple[Rect, Grid | None]]] = {}
        heights = {no: page.size.height for no, page in document.pages.items()}
        layers = {ContentLayer.BODY, ContentLayer.FURNITURE}
        for item, _ in document.iterate_items(included_content_layers=layers, with_groups=False):
            for prov in getattr(item, "prov", ()):
                rect = _top_left(prov.bbox, heights[prov.page_no])
                grid = None
                if isinstance(item, TableItem):
                    grid = _table_grid(item, rect, heights[prov.page_no])
                regions.setdefault(prov.page_no, []).append((rect, grid))

        pages = triage(pdf)
        inputs: list[PageInput] = []
        with pymupdf.open(stream=pdf, filetype="pdf") as doc:
            for page in doc:
                number = page.number + 1  # ty: ignore[unsupported-operator]
                page_input = pymupdf_page_input(page)
                if number in regions and page.rotation == page_input.frame == 0:
                    page_input = _docling_page(page, regions[number])
                page_input.triage = pages[number - 1]
                inputs.append(page_input)
        return assemble(inputs)


def _table_grid(item: Any, rect: Rect, page_height: float) -> Grid | None:
    data = item.data
    if data.num_rows < 1 or data.num_cols < 1:
        return None
    specs = [
        CellSpec(
            row=c.start_row_offset_idx,
            col=c.start_col_offset_idx,
            row_span=max(1, c.row_span),
            col_span=max(1, c.col_span),
            rect=_top_left(c.bbox, page_height) if c.bbox is not None else (0.0, 0.0, 0.0, 0.0),
            header=bool(c.column_header),
        )
        for c in data.table_cells
    ]
    return grid_from_cells(rect, data.num_rows, data.num_cols, specs)


def _docling_page(page: pymupdf.Page, regions: list[tuple[Rect, Grid | None]]) -> PageInput:
    """Blocks in Docling's reading order; words outside every region keep PyMuPDF's grouping."""
    base = pymupdf_page_input(page, with_tables=False)
    ranked: list[PBlock] = []
    grids: list[Grid] = []
    lines_by_region: list[dict[int, list[PWord]]] = [{} for _ in regions]
    leftover: list[PBlock] = []
    in_tables: list[PWord] = []  # claimed by the grids in `assemble`
    line_no = 0
    for block in base.blocks:
        rest: list[list[PWord]] = []
        for line in block.lines:
            line_no += 1
            orphans = []
            for word in line:
                hit = next(
                    (
                        i
                        for i, (r, _) in enumerate(regions)
                        if contains(r, word.cx, word.cy, slack=1.0)
                    ),
                    None,
                )
                if hit is None:
                    orphans.append(word)
                elif regions[hit][1] is None:
                    lines_by_region[hit].setdefault(line_no, []).append(word)
                else:
                    in_tables.append(word)
            if orphans:
                rest.append(orphans)
        if rest:
            leftover.append(PBlock(lines=rest))
    for rank, ((_, grid), lines) in enumerate(zip(regions, lines_by_region, strict=True)):
        if grid is not None:
            grid.order = float(rank)
            grids.append(grid)
        elif lines:
            ranked.append(PBlock(lines=[lines[k] for k in sorted(lines)], order=float(rank)))
    base.blocks = [*ranked, *leftover, *([PBlock(lines=[in_tables])] if in_tables else [])]
    base.grids = grids
    return base
