"""Rows for documents, layers, anchors, and anchor mappings.

Every function takes the caller's transaction. Bytes go to the artifact store before a row
references them.
"""

from collections import OrderedDict
from typing import TYPE_CHECKING, Any, cast

import pymupdf
from psycopg.types.json import Jsonb

from kittum.core import Id, NotFoundError, canonical_json, content_hash, new_id, sha256_hex
from kittum.documents.anchors import RESOLVE_VERSION, resolve
from kittum.documents.model import (
    Anchor,
    Box,
    CellRef,
    Document,
    Layer,
    Parser,
    ParserRecipe,
    RawLayer,
    Resolution,
    TextQuote,
)

if TYPE_CHECKING:
    from kittum.db import Tx
    from kittum.storage import ArtifactStore


class NotAPdf(ValueError):
    """The bytes do not open as a PDF."""


def recipe_hash(recipe: ParserRecipe) -> str:
    return content_hash(recipe, domain="kittum.documents.recipe.v1")


def _document(row: dict[str, Any]) -> Document:
    return Document(
        id=row["id"],
        artifact_hash=row["artifact_hash"],
        media_type=row["media_type"],
        page_count=row["page_count"],
    )


def ingest(tx: Tx, artifacts: ArtifactStore, pdf: bytes) -> Document:
    """Store a PDF once by its SHA-256. Identical bytes give the same document.

    PyMuPDF repairs what it can, so a cut-off download still opens. A PDF without pages, or
    without an end-of-file marker in its last 2 KiB, is refused as truncated.
    """
    if b"%PDF-" not in pdf[:1024]:
        raise NotAPdf("no PDF header")
    if b"%%EOF" not in pdf[-2048:]:
        raise NotAPdf("no end-of-file marker; the file looks truncated")
    try:
        with pymupdf.open(stream=pdf, filetype="pdf") as doc:
            pages = doc.page_count
    except (pymupdf.FileDataError, RuntimeError, ValueError) as e:
        raise NotAPdf(str(e)) from e
    if pages == 0:
        raise NotAPdf("no pages")
    digest = artifacts.put(pdf)
    tx.execute(
        """insert into document (id, artifact_hash, media_type, byte_size, page_count)
           values (%s, %s, 'application/pdf', %s, %s)
           on conflict (artifact_hash) do nothing""",
        (new_id(), digest, len(pdf), pages),
    )
    row = tx.execute("select * from document where artifact_hash = %s", (digest,)).fetchone()
    assert row is not None
    return _document(row)


def get_document(tx: Tx, document_id: Id) -> Document:
    row = tx.execute("select * from document where id = %s", (document_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"document {document_id}")
    return _document(row)


_LAYER_CACHE: OrderedDict[str, RawLayer] = OrderedDict()
_LAYER_CACHE_SIZE = 8


def _raw_layer(artifacts: ArtifactStore, output_hash: str) -> RawLayer:
    """Layers are immutable, so the parsed JSON of recent ones is kept in memory."""
    raw = _LAYER_CACHE.get(output_hash)
    if raw is None:
        raw = RawLayer.model_validate_json(artifacts.get(output_hash))
    _remember(output_hash, raw)
    return raw


def _remember(output_hash: str, raw: RawLayer) -> None:
    _LAYER_CACHE[output_hash] = raw
    _LAYER_CACHE.move_to_end(output_hash)
    while len(_LAYER_CACHE) > _LAYER_CACHE_SIZE:
        _LAYER_CACHE.popitem(last=False)


def _layer(row: dict[str, Any], raw: RawLayer) -> Layer:
    return Layer(
        id=row["id"],
        document_id=row["document_id"],
        recipe=ParserRecipe.model_validate(row["recipe"]),
        recipe_hash=row["recipe_hash"],
        output_hash=row["output_hash"],
        **{name: getattr(raw, name) for name in RawLayer.model_fields},
    )


def derive(tx: Tx, document: Document, parser: Parser, *, artifacts: ArtifactStore) -> Layer:
    """Parse a document with one recipe and store the layer. Idempotent per recipe: a second call
    returns the stored layer without parsing again."""
    r_hash = recipe_hash(parser.recipe)
    row = tx.execute(
        "select * from layer where document_id = %s and recipe_hash = %s", (document.id, r_hash)
    ).fetchone()
    if row is not None:
        return _layer(row, _raw_layer(artifacts, row["output_hash"]))
    raw = parser.parse(artifacts.get(document.artifact_hash))
    body = canonical_json(raw)
    output_hash = artifacts.put(body)
    _remember(output_hash, raw)
    tx.execute(
        """insert into layer (id, document_id, recipe_hash, recipe, output_hash, text_hash, page_count)
           values (%s, %s, %s, %s, %s, %s, %s)
           on conflict (document_id, recipe_hash) do nothing""",
        (
            new_id(),
            document.id,
            r_hash,
            Jsonb(parser.recipe.model_dump(mode="json")),
            output_hash,
            sha256_hex(raw.text.encode()),
            len(raw.pages),
        ),
    )
    row = tx.execute(
        "select * from layer where document_id = %s and recipe_hash = %s", (document.id, r_hash)
    ).fetchone()
    assert row is not None
    return _layer(row, _raw_layer(artifacts, row["output_hash"]))


def get_layer(tx: Tx, layer_id: Id, *, artifacts: ArtifactStore) -> Layer:
    row = tx.execute("select * from layer where id = %s", (layer_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"layer {layer_id}")
    return _layer(row, _raw_layer(artifacts, row["output_hash"]))


def list_layers(tx: Tx, document_id: Id) -> list[tuple[Id, ParserRecipe]]:
    """The document's layers, oldest first, without loading their text."""
    rows = tx.execute(
        "select id, recipe from layer where document_id = %s order by created_at, id",
        (document_id,),
    ).fetchall()
    return [(r["id"], ParserRecipe.model_validate(r["recipe"])) for r in rows]


def _anchor(row: dict[str, Any]) -> Anchor:
    return Anchor(
        id=row["id"],
        layer_id=row["layer_id"],
        document_id=row["document_id"],
        quote=TextQuote.model_validate(row["quote"]),
        text_hash=row["text_hash"],
        start=row["start_offset"],
        end=row["end_offset"],
        boxes=tuple(Box.model_validate(b) for b in row["boxes"]),
        cells=tuple(CellRef.model_validate(c) for c in row["cells"]),
    )


def save_anchor(tx: Tx, anchor: Anchor) -> Anchor:
    tx.execute(
        """insert into anchor (id, layer_id, document_id, quote, text_hash, start_offset,
                               end_offset, boxes, cells)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           on conflict (id) do nothing""",
        (
            anchor.id,
            anchor.layer_id,
            anchor.document_id,
            Jsonb(anchor.quote.model_dump(mode="json")),
            anchor.text_hash,
            anchor.start,
            anchor.end,
            Jsonb([b.model_dump(mode="json") for b in anchor.boxes]),
            Jsonb([c.model_dump(mode="json") for c in anchor.cells]),
        ),
    )
    return anchor


def get_anchor(tx: Tx, anchor_id: Id) -> Anchor:
    row = tx.execute("select * from anchor where id = %s", (anchor_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"anchor {anchor_id}")
    return _anchor(row)


def remap(tx: Tx, anchor: Anchor, layer: Layer) -> Resolution:
    """Resolve a stored anchor onto another layer and record the mapping. A second call for the
    same anchor, layer, and method version returns the recorded mapping."""
    row = tx.execute(
        """select * from anchor_mapping
           where old_anchor_id = %s and layer_id = %s and method_version = %s""",
        (anchor.id, layer.id, RESOLVE_VERSION),
    ).fetchone()
    if row is not None:
        alternatives = tuple(get_anchor(tx, a) for a in row["alternative_anchor_ids"])
        return Resolution(
            status=row["status"],
            anchor=get_anchor(tx, row["new_anchor_id"]) if row["new_anchor_id"] else None,
            method=row["method"],
            method_version=row["method_version"],
            alternatives=alternatives,
        )
    resolution = resolve(anchor, layer)
    if resolution.anchor is not None and resolution.anchor.id != anchor.id:
        save_anchor(tx, resolution.anchor)
    for alternative in resolution.alternatives:
        save_anchor(tx, alternative)
    tx.execute(
        """insert into anchor_mapping (id, old_anchor_id, layer_id, new_anchor_id, status, method,
                                       method_version, alternative_anchor_ids)
           values (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            new_id(),
            anchor.id,
            layer.id,
            resolution.anchor.id if resolution.anchor else None,
            resolution.status,
            resolution.method,
            resolution.method_version,
            [a.id for a in resolution.alternatives],
        ),
    )
    return resolution


def cached_render(tx: Tx, document_id: Id, page: int, scale: float, renderer: str) -> str | None:
    row = tx.execute(
        """select artifact_hash from page_render
           where document_id = %s and page = %s and scale = %s and renderer = %s""",
        (document_id, page, scale, renderer),
    ).fetchone()
    return cast("str", row["artifact_hash"]) if row else None


def record_render(
    tx: Tx, document_id: Id, page: int, scale: float, renderer: str, artifact_hash: str
) -> None:
    tx.execute(
        """insert into page_render (document_id, page, scale, renderer, artifact_hash)
           values (%s, %s, %s, %s, %s) on conflict do nothing""",
        (document_id, page, scale, renderer, artifact_hash),
    )
