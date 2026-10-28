"""The viewer's routes: upload, pages, page images, word boxes, and anchors."""

from pathlib import Path

import pymupdf
from fastapi.testclient import TestClient

from kittum.api import AppContext, create_app
from kittum.documents import Box
from kittum.settings import Settings
from kittum.storage import MemoryStore
from tests.documents.conftest import LESOTHO, ink, savepoint_db
from tests.documents.test_parsing import COLUMN_BREAK_QUOTE, GUTTER

PDF = {"Content-Type": "application/pdf"}


def _client(pg) -> tuple[TestClient, MemoryStore]:
    artifacts = MemoryStore()
    context = AppContext(
        settings=Settings(database_url="unused", artifact_dir=Path("unused")),
        db=savepoint_db(pg),
        artifacts=artifacts,
    )
    return TestClient(create_app(context)), artifacts


def test_upload_render_and_anchor_through_the_api(pg):
    client, artifacts = _client(pg)
    with client:
        uploaded = client.post("/api/documents", content=LESOTHO.read_bytes(), headers=PDF)
        assert uploaded.status_code == 201
        document_id = uploaded.json()["document"]["id"]
        layer_id = uploaded.json()["layer_id"]
        again = client.post("/api/documents", content=LESOTHO.read_bytes(), headers=PDF).json()
        assert (again["document"]["id"], again["layer_id"]) == (document_id, layer_id)

        view = client.get(f"/api/documents/{document_id}").json()
        assert len(view["pages"]) == 13
        assert [layer["engine"] for layer in view["layers"]] == ["pymupdf"]
        page7 = view["pages"][6]
        assert (page7["width"], page7["height"], page7["rotation"]) == (595.28, 793.7, 0)
        assert page7["triage"]["kind"] == "text"

        scale = 1.5
        image = client.get(f"/api/documents/{document_id}/pages/7/image", params={"scale": scale})
        assert image.status_code == 200 and image.headers["content-type"] == "image/png"
        pixmap = pymupdf.Pixmap(image.content)
        assert abs(pixmap.width - 595.28 * scale) <= 1 and abs(pixmap.height - 793.7 * scale) <= 1
        renders = pg.execute("select artifact_hash from page_render").fetchall()
        assert len(renders) == 1 and artifacts.get(renders[0]["artifact_hash"]) == image.content
        cached = client.get(f"/api/documents/{document_id}/pages/7/image", params={"scale": scale})
        assert cached.content == image.content
        assert len(pg.execute("select * from page_render").fetchall()) == 1

        words = client.get(f"/api/layers/{layer_id}/pages/7/words").json()
        assert words["width"] == 595.28
        age = next(w for w in words["words"] if w["text"] == "61.4")
        age_box = Box(page=7, x0=age["x0"], y0=age["y0"], x1=age["x1"], y1=age["y1"])
        assert ink(image.content, age_box, scale) > 0.05  # the word box sits on printed ink

        created = client.post(f"/api/layers/{layer_id}/anchors", json={"quote": COLUMN_BREAK_QUOTE})
        assert created.status_code == 201
        anchor = client.get(f"/api/anchors/{created.json()['id']}").json()
        boxes = [Box.model_validate(b) for b in anchor["boxes"]]
        assert len(boxes) == 3 and boxes[2].x0 > GUTTER
        for box in boxes:
            assert ink(image.content, box, scale) > 0.05
        # The gutter beside the quote's lines is blank paper.
        gutter = Box(page=7, x0=GUTTER - 3, y0=boxes[0].y0, x1=GUTTER + 3, y1=boxes[1].y1)
        assert ink(image.content, gutter, scale) == 0.0

        repeated = {"quote": "1.0 (1.0, 2.0)"}
        assert client.post(f"/api/layers/{layer_id}/anchors", json=repeated).status_code == 409
        third = client.post(f"/api/layers/{layer_id}/anchors", json={**repeated, "occurrence": 2})
        assert third.status_code == 201
        (cell,) = third.json()["cells"]
        assert cell["col"] == 3 and cell["header_path"] == ["Primary analysis set", "Total"]


def test_api_refuses_what_it_cannot_serve(pg):
    client, _ = _client(pg)
    with client:
        not_pdf = client.post("/api/documents", content=b"<html>login</html>", headers=PDF)
        assert not_pdf.status_code == 415
        uploaded = client.post("/api/documents", content=LESOTHO.read_bytes(), headers=PDF).json()
        document_id, layer_id = uploaded["document"]["id"], uploaded["layer_id"]
        image = f"/api/documents/{document_id}/pages"
        assert client.get(f"{image}/14/image").status_code == 404
        assert client.get(f"{image}/1/image", params={"scale": 10}).status_code == 422
        assert client.get(f"/api/layers/{layer_id}/pages/14/words").status_code == 404
        missing = {"quote": "a sentence this trial never printed"}
        assert client.post(f"/api/layers/{layer_id}/anchors", json=missing).status_code == 404
        anchors = f"/api/layers/{layer_id}/anchors"
        # Part of a word ("andomi" in "randomised") cannot be re-anchored, so it is refused.
        assert client.post(anchors, json={"quote": "andomi", "occurrence": 0}).status_code == 404
        # A negative occurrence must not pick the last match.
        repeated = {"quote": "1.0 (1.0, 2.0)", "occurrence": -1}
        assert client.post(anchors, json=repeated).status_code == 422
        unknown = "01a18000-0000-7000-8000-000000000000"
        assert client.get(f"/api/documents/{unknown}").status_code == 404
        assert client.get(f"/api/anchors/{unknown}").status_code == 404
