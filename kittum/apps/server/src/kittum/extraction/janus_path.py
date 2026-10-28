"""The deterministic path: a layer as Janus evidence, a Janus specification per schema, and
Janus's results mapped back to proposals anchored on the layer.

Janus reads the same PyMuPDF text layer Kittum does, but through Kittum's layer rather than its
own reader, so its locations map back to layer cells and lines one to one. The conversion:

- one `TextElement` per layer line, so Janus's text-label rows are layer line ordinals;
- one `TableStructure` per layer table, with the layer's header rows folded into one row whose
  cells carry the full header path ("Primary analysis set Control"), so `column_labels` can
  tell spanned groups apart;
- each body row's `section` is the run of group rows (a label and no values) above it.

Janus locates a cell; Kittum parses it with Janus's typed parser after normalizing typographic
minus signs and an inline estimator label ("RR:1.124 (0.978, 1.290)"), and anchors the value on
the cell's span in the layer with `make_anchor`.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from typing import TYPE_CHECKING, Any

from janus.comparison.content import (
    BoundingBox,
    PageContent,
    TableCell,
    TableStructure,
    TextElement,
)
from janus.comparison.models import CompoundShape, DocumentEvidence, ValueType
from janus.extraction import ExtractionSpecification
from janus.extraction.engine import extract_evidence, pointer
from janus.extraction.models import Scalar
from janus.extraction.parsing import parse_value

from kittum.core import Span
from kittum.documents import make_anchor
from kittum.extraction.model import Proposal
from kittum.extraction.numbers import (
    NUMBER,
    ascii_minus,
    normalize,
    printed_in,
    strip_label,
    well_formed,
)

if TYPE_CHECKING:
    from janus.comparison.models import EvidenceLocation
    from janus.extraction.models import FieldResult

    from kittum.documents import Anchor, Box, Layer
    from kittum.documents import TableCell as LayerCell
    from kittum.extraction.schema import Kind, SchemaField

JANUS_VERSION = version("janus-server")
READER_NAME = "kittum.layer"
# A fixed evidence time, so the same layer gives byte-identical evidence.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# What a table prints for a value that was not reported or does not apply.
_NOT_REPORTED = frozenset({"-", "\u2013", "\u2014", "na", "n/a", "nr", "ne", "not reported"})

_TYPES: dict[Kind, ValueType] = {
    "count": ValueType.INTEGER,
    "decimal": ValueType.DECIMAL,
    "percentage": ValueType.PERCENTAGE,
    "ratio": ValueType.DECIMAL,
    "p_value": ValueType.DECIMAL,
    "text": ValueType.TEXT,
}


@dataclass(frozen=True)
class JanusEvidence:
    """A layer as Janus evidence, with the way back from Janus coordinates to the layer."""

    evidence: DocumentEvidence
    cells: dict[tuple[int, int, int, int], LayerCell]  # (page, table index, row, col) -> cell
    lines: dict[tuple[int, int], tuple[int, int]]  # (page, line ordinal) -> layer span
    # (page, rounded x, y, width, height) -> layer span. A text label's value can sit on its own
    # line beside the label; Janus then reports the label line's ordinal but the value's box.
    line_boxes: dict[tuple[int, ...], tuple[int, int]]


def _box_key(page: int, box: dict[str, float]) -> tuple[int, ...]:
    return (page, *(round(box[k] * 100) for k in ("x", "y", "width", "height")))


def _bbox(box: Box) -> BoundingBox:
    return BoundingBox(
        x=max(box.x0, 0.0),
        y=max(box.y0, 0.0),
        width=max(box.x1 - box.x0, 0.0),
        height=max(box.y1 - box.y0, 0.0),
        page=box.page - 1,
    )


def _tables(
    layer: Layer, page: int
) -> tuple[list[TableStructure], dict[tuple[int, int, int], LayerCell]]:
    out: list[TableStructure] = []
    back: dict[tuple[int, int, int], LayerCell] = {}
    for table in (t for t in layer.tables if t.page == page):
        cells = [c for c in layer.cells if c.table_id == table.id]
        rows = sorted({c.row for c in cells})
        by_row = {r: [c for c in cells if c.row == r] for r in rows}
        header_rows = []
        for r in rows:
            if not all(c.header for c in by_row[r]):
                break
            header_rows.append(r)
        body = [r for r in rows if r not in header_rows]
        text = {id(c): layer.text[c.start : c.end] for c in cells}

        def flat(value: str) -> str:
            return " ".join(value.split())

        columns = sorted({c.col for c in cells})
        header: list[TableCell] = []
        for col in columns:
            first = next((c for r in body for c in by_row[r] if c.col == col), None)
            path = list(first.header_path) if first is not None else []
            if not path:
                path = [flat(text[id(c)]) for r in header_rows for c in by_row[r] if c.col == col]
            words = " ".join(p for p in path if p)
            header.append(TableCell(text=words, row=0, column=col))
        out_cells = list(header)
        section: list[str] = []
        in_group = False
        for index, r in enumerate(body, start=1):
            row = by_row[r]
            label = next((flat(text[id(c)]) for c in row if c.col == 0), "")
            values = [c for c in row if c.col != 0 and text[id(c)].strip()]
            if label and not values:
                # A group row; consecutive ones nest ("Pain severity" over "2 h").
                section = [*section, label] if in_group else [label]
                in_group = True
            else:
                in_group = False
            for c in row:
                out_cells.append(
                    TableCell(
                        text=text[id(c)].strip(),
                        row=index,
                        column=c.col,
                        bbox=_bbox(c.box),
                        section=" / ".join(section) if section and not in_group else None,
                    )
                )
                back[(len(out), index, c.col)] = c
        out.append(
            TableStructure(
                rows=len(body) + 1,
                columns=len(columns),
                cells=out_cells,
                bbox=_bbox(table.box),
                header_row=True,
                bordered=None,
            )
        )
    return out, back


def to_evidence(layer: Layer, document_hash: str) -> JanusEvidence:
    """Janus's `DocumentEvidence` contract for a layer."""
    cells: dict[tuple[int, int, int, int], LayerCell] = {}
    lines: dict[tuple[int, int], tuple[int, int]] = {}
    line_boxes: dict[tuple[int, ...], tuple[int, int]] = {}
    pages: list[PageContent] = []
    for page in layer.pages:
        number = page.number - 1
        elements: list[TextElement] = []
        for line in layer.lines:
            if line.box.page != page.number:
                continue
            text = layer.text[line.start : line.end].strip()
            if not text:
                continue
            lines[(number, len(elements))] = (line.start, line.end)
            box = _bbox(line.box)
            line_boxes[_box_key(number, box.model_dump())] = (line.start, line.end)
            elements.append(TextElement(text=text, confidence=1, element_type="line", bbox=box))
        tables, back = _tables(layer, page.number)
        cells.update({(number, *k): v for k, v in back.items()})
        pages.append(
            PageContent(
                page_number=number,
                width=page.width,
                height=page.height,
                text_elements=elements,
                full_text=layer.text[page.start : page.end],
                tables=tables,
            )
        )
    evidence = DocumentEvidence(
        document_id=str(layer.document_id),
        document_hash=document_hash,
        reader_name=READER_NAME,
        reader_configuration={"layer_id": str(layer.id), "layer_output_hash": layer.output_hash},
        pages=pages,
        created_at=_EPOCH,
    )
    return JanusEvidence(evidence=evidence, cells=cells, lines=lines, line_boxes=line_boxes)


def field_path(field: SchemaField) -> str:
    k = field.key
    path = ""
    for part in (k.endpoint, k.timepoint, k.arm, k.quantity):
        path = pointer(path, part)
    return path


def specification(name: str, fields: tuple[SchemaField, ...]) -> ExtractionSpecification:
    """One Janus specification for every field that has a locator, nested by key.

    Each field is a text scalar: Janus locates the cell, and `_parse` reads the value from it.
    """
    root: dict[str, Any] = {}
    for f in fields:
        if f.locate is None:
            continue
        k = f.key
        node = root
        for part in (k.endpoint, k.timepoint, k.arm):
            node = node.setdefault(part, {"type": "object", "fields": {}})["fields"]
        locate: dict[str, Any] = {"strategy": f.locate.strategy, "labels": list(f.locate.rows)}
        if f.locate.sections:
            locate["section_labels"] = list(f.locate.sections)
        if f.locate.columns and f.locate.strategy == "table_label":
            locate["column_labels"] = list(f.locate.columns)
        node[k.quantity] = {"type": "text", "locate": locate}
    return ExtractionSpecification.model_validate({"name": name, "fields": root})


def _parse(raw: str, field: SchemaField) -> tuple[str | None, str | None]:
    """The value in a cell, as an exact string, or the reason it cannot be read."""
    text, _label = strip_label(normalize(raw))
    shape = component = None
    if field.locate is not None and field.locate.cell is not None:
        name, component = field.locate.cell.split(".")
        shape = CompoundShape(name)
    if field.kind == "p_value":
        # "<0.001" is an interval statement; keep it as printed.
        stripped = text.replace(" ", "")
        if stripped[:1] in "<>\u2264\u2265" and shape is None:
            problem = well_formed(stripped, "p_value")
            return (None, problem) if problem else (stripped, None)
    rule = Scalar(type=_TYPES[field.kind], source_shape=shape, component=component)
    try:
        value = parse_value(text, rule)
    except ValueError as e:
        if shape is CompoundShape.COUNT_PERCENTAGE and component == "count":
            # A zero count is often printed without its percentage: "0" in an "n (%)" column.
            try:
                value = parse_value(text, Scalar(type=ValueType.INTEGER))
            except ValueError:
                return None, f"cannot read {raw!r}: {e}"
        else:
            return None, f"cannot read {raw!r}: {e}"
    value = str(value)
    problem = well_formed(value, field.kind)
    return (None, problem) if problem else (value, None)


def _narrow(layer: Layer, start: int, end: int, value: str) -> Span:
    """The value's own token inside a cell or line, when it occurs there once; else the whole."""
    text = ascii_minus(layer.text[start:end])
    hits = [m for m in NUMBER.finditer(text) if m.group(0).lstrip("+") == value]
    if len(hits) == 1:
        return Span(start + hits[0].start(), start + hits[0].end())
    return Span(start, end)


def _span(
    janus: JanusEvidence, field: SchemaField, source: EvidenceLocation
) -> tuple[int, int] | None:
    if field.locate is not None and field.locate.strategy == "text_label":
        if source.cell_bbox is not None:
            return janus.line_boxes.get(_box_key(source.page, source.cell_bbox))
        return janus.lines.get((source.page, source.row))
    cell = janus.cells.get((source.page, source.table_index, source.row, source.column))
    return (cell.start, cell.end) if cell is not None and cell.end > cell.start else None


def run(
    layer: Layer, document_hash: str, fields: tuple[SchemaField, ...], name: str
) -> tuple[list[Proposal], dict[str, Anchor]]:
    """Janus proposals for every field, and the anchors they reference (not yet saved)."""
    janus = to_evidence(layer, document_hash)
    spec = specification(name, fields)
    results: dict[str, FieldResult] = {}
    if any(f.locate is not None for f in fields):
        for r in extract_evidence(janus.evidence, spec).fields:
            results[r.path] = r
    proposals: list[Proposal] = []
    anchors: dict[str, Anchor] = {}

    def proposal(field: SchemaField, status, value=None, anchor=None, message=None, quote=None):
        return Proposal(
            key=field.key,
            value=value,
            unit=field.spec.unit if value is not None else None,
            status=status,
            path="janus",
            anchor_ids=(anchor.id,) if anchor is not None else (),
            message=message,
            quote=quote,
        )

    for f in fields:
        if f.locate is None:
            proposals.append(proposal(f, "missing", message="the schema gives Janus no locator"))
            continue
        result = results.get(field_path(f))
        if result is None:
            proposals.append(proposal(f, "error", message="Janus returned no result"))
            continue
        if result.status != "extracted" or result.source is None:
            proposals.append(proposal(f, result.status, message=result.message or None))
            continue
        span = _span(janus, f, result.source)
        raw = result.source.raw_text
        if raw.strip().casefold() in _NOT_REPORTED:
            proposals.append(proposal(f, "missing", message=f"the cell prints {raw!r}", quote=raw))
            continue
        if span is None:
            proposals.append(
                proposal(f, "error", message="Janus located a cell the layer cannot anchor")
            )
            continue
        value, problem = _parse(raw, f)
        if value is None:
            proposals.append(proposal(f, "invalid", message=problem, quote=raw))
            continue
        target = _narrow(layer, *span, value) if f.kind != "text" else Span(*span)
        if printed_in(value, f.kind, layer.text[target.start : target.end]) is None:
            # An anchor must print its value; never widen to a span that does not.
            proposals.append(
                proposal(f, "error", message="the located text does not print the value", quote=raw)
            )
            continue
        anchor = make_anchor(layer, target)
        anchors[str(anchor.id)] = anchor
        proposals.append(proposal(f, "extracted", value=value, anchor=anchor, quote=raw))
    return proposals, anchors
