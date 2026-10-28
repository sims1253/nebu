"""Page images for the workbench viewer.

Pages render unrotated, in the frame the layer's boxes use, so the viewer can lay word boxes over
the image by multiplying with the scale and turn the whole page by `Page.rotation` itself. PNGs are
stored in the artifact store and indexed per document, page, scale, and renderer.
"""

from typing import TYPE_CHECKING

import pymupdf

from kittum.core import NotFoundError
from kittum.documents.store import cached_render, record_render

if TYPE_CHECKING:
    from kittum.db import Tx
    from kittum.documents.model import Document
    from kittum.storage import ArtifactStore

RENDERER = f"pymupdf {pymupdf.VersionBind} unrotated png"
MIN_SCALE = 0.25
MAX_SCALE = 4.0


def render_png(pdf: bytes, page: int, scale: float) -> bytes:
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        if not 1 <= page <= doc.page_count:
            raise NotFoundError(f"page {page}")
        p = doc[page - 1]
        p.set_rotation(0)
        pixmap = p.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        return pixmap.tobytes("png")


def render_page(
    tx: Tx, artifacts: ArtifactStore, document: Document, page: int, scale: float
) -> bytes:
    """A PNG of one page at `scale` pixels per PDF point, rendered once and then served from the
    artifact store."""
    scale = round(scale, 2)
    if not MIN_SCALE <= scale <= MAX_SCALE:
        raise ValueError(f"scale must be between {MIN_SCALE} and {MAX_SCALE}")
    if document.page_count is not None and not 1 <= page <= document.page_count:
        raise NotFoundError(f"page {page}")
    cached = cached_render(tx, document.id, page, scale, RENDERER)
    if cached is not None and artifacts.exists(cached):
        return artifacts.get(cached)
    png = render_png(artifacts.get(document.artifact_hash), page, scale)
    record_render(tx, document.id, page, scale, RENDERER, artifacts.put(png))
    return png
