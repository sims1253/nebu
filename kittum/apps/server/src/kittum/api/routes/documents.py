"""Documents for the workbench viewer: upload, pages, page images, word boxes, and anchors.

The viewer shows a page as a PNG and lays an invisible text layer over it from the word boxes.
Boxes are in unrotated PDF points; multiply by the image scale to get pixels. Offsets are Unicode
code points into the layer text; the browser converts its UTF-16 positions.
"""

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from kittum.api import Context  # noqa: TC001  FastAPI resolves it at runtime
from kittum.core import Id, NotFoundError, Record
from kittum.documents import (
    MAX_SCALE,
    MIN_SCALE,
    Anchor,
    Document,
    NotAPdf,
    PageTriage,
    PyMuPDFParser,
    derive,
    get_anchor,
    get_document,
    get_layer,
    ingest,
    list_layers,
    make_anchor,
    page_words,
    quote_spans,
    render_page,
    save_anchor,
)

if TYPE_CHECKING:
    from kittum.api import AppContext

router = APIRouter(prefix="/api", tags=["documents"])

_PDF_BODY = {
    "requestBody": {
        "required": True,
        "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}},
    }
}


class UploadedDocument(Record):
    document: Document
    layer_id: Id  # the base PyMuPDF layer


class PageView(Record):
    number: int
    width: float
    height: float
    rotation: int
    label: str | None
    triage: PageTriage | None


class LayerSummary(Record):
    id: Id
    engine: str
    engine_version: str


class DocumentView(Record):
    document: Document
    layers: list[LayerSummary]
    pages: list[PageView]  # from the first layer


class WordBox(Record):
    start: int
    end: int
    text: str
    x0: float
    y0: float
    x1: float
    y1: float


class PageWords(Record):
    layer_id: Id
    page: int
    width: float
    height: float
    rotation: int
    words: list[WordBox]


class AnchorRequest(Record):
    quote: str
    # 0-based, among whole-token occurrences; required when the quote occurs more than once.
    occurrence: Annotated[int, Field(ge=0)] | None = None


def _not_found(e: NotFoundError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(e))


def _upload(ctx: AppContext, pdf: bytes) -> UploadedDocument:
    with ctx.db.transaction() as tx:
        document = ingest(tx, ctx.artifacts, pdf)
        layer = derive(tx, document, PyMuPDFParser(), artifacts=ctx.artifacts)
    return UploadedDocument(document=document, layer_id=layer.id)


@router.post("/documents", operation_id="uploadDocument", status_code=201, openapi_extra=_PDF_BODY)
async def upload_document(request: Request, ctx: Context) -> UploadedDocument:
    """Store a PDF and parse its base layer. Uploading the same bytes again is a no-op."""
    pdf = await request.body()
    try:
        return await run_in_threadpool(_upload, ctx, pdf)
    except NotAPdf as e:
        raise HTTPException(status_code=415, detail=f"not a PDF: {e}") from e


@router.get("/documents/{document_id}", operation_id="getDocument")
def get_document_view(document_id: Id, ctx: Context) -> DocumentView:
    with ctx.db.transaction() as tx:
        try:
            document = get_document(tx, document_id)
        except NotFoundError as e:
            raise _not_found(e) from e
        layers = list_layers(tx, document_id)
        pages = get_layer(tx, layers[0][0], artifacts=ctx.artifacts).pages if layers else ()
    return DocumentView(
        document=document,
        layers=[
            LayerSummary(id=i, engine=r.engine, engine_version=r.engine_version) for i, r in layers
        ],
        pages=[
            PageView(
                number=p.number,
                width=p.width,
                height=p.height,
                rotation=p.rotation,
                label=p.label,
                triage=p.triage,
            )
            for p in pages
        ],
    )


@router.get(
    "/documents/{document_id}/pages/{page}/image",
    operation_id="renderPage",
    response_class=Response,
    responses={200: {"content": {"image/png": {}}}},
)
def render_document_page(
    document_id: Id,
    page: int,
    ctx: Context,
    scale: Annotated[float, Query(ge=MIN_SCALE, le=MAX_SCALE)] = 1.0,
) -> Response:
    """The page, unrotated, at `scale` pixels per PDF point."""
    with ctx.db.transaction() as tx:
        try:
            png = render_page(tx, ctx.artifacts, get_document(tx, document_id), page, scale)
        except NotFoundError as e:
            raise _not_found(e) from e
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "max-age=31536000, immutable"},
    )


@router.get("/layers/{layer_id}/pages/{page}/words", operation_id="getPageWords")
def get_page_words(layer_id: Id, page: int, ctx: Context) -> PageWords:
    with ctx.db.transaction() as tx:
        try:
            layer = get_layer(tx, layer_id, artifacts=ctx.artifacts)
        except NotFoundError as e:
            raise _not_found(e) from e
    info = next((p for p in layer.pages if p.number == page), None)
    if info is None:
        raise HTTPException(status_code=404, detail=f"page {page}")
    return PageWords(
        layer_id=layer.id,
        page=page,
        width=info.width,
        height=info.height,
        rotation=info.rotation,
        words=[
            WordBox(
                start=w.start,
                end=w.end,
                text=text,
                x0=w.box.x0,
                y0=w.box.y0,
                x1=w.box.x1,
                y1=w.box.y1,
            )
            for w, text in page_words(layer, page)
        ],
    )


@router.post("/layers/{layer_id}/anchors", operation_id="createAnchor", status_code=201)
def create_anchor(layer_id: Id, body: AnchorRequest, ctx: Context) -> Anchor:
    """Anchor a verbatim quote. 404 if the layer does not contain it, 409 if it occurs more than
    once and no `occurrence` is given."""
    with ctx.db.transaction() as tx:
        try:
            layer = get_layer(tx, layer_id, artifacts=ctx.artifacts)
        except NotFoundError as e:
            raise _not_found(e) from e
        # Whole tokens only, as `resolve` requires: an anchor must stay re-anchorable.
        spans = quote_spans(layer.text, body.quote)
        if not spans:
            raise HTTPException(
                status_code=404, detail="quote not found in the layer as whole words"
            )
        if body.occurrence is None and len(spans) > 1:
            raise HTTPException(status_code=409, detail=f"quote occurs {len(spans)} times")
        index = body.occurrence or 0
        if index >= len(spans):
            raise HTTPException(status_code=404, detail=f"quote occurs {len(spans)} times")
        return save_anchor(tx, make_anchor(layer, spans[index]))


@router.get("/anchors/{anchor_id}", operation_id="getAnchor")
def get_anchor_view(anchor_id: Id, ctx: Context) -> Anchor:
    """An anchor with its quote, offsets, one box per line, and table cells."""
    with ctx.db.transaction() as tx:
        try:
            return get_anchor(tx, anchor_id)
        except NotFoundError as e:
            raise _not_found(e) from e
