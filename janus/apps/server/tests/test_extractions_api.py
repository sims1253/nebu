from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from janus.main import app

EXAMPLE = Path(__file__).parents[3] / "examples" / "purchase-order"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("JANUS_DATA_DIR", str(tmp_path))
    with TestClient(app) as client:
        yield client


def create(client, spec=None):
    return client.post(
        "/api/extractions",
        files={
            "document": (
                "purchase-order.pdf",
                (EXAMPLE / "document.pdf").read_bytes(),
                "application/pdf",
            ),
            "specification": (
                "extraction.json",
                spec or (EXAMPLE / "extraction.json").read_bytes(),
                "application/json",
            ),
        },
    )


def test_extract_reopen_render_compare_and_delete(client):
    response = create(client)
    assert response.status_code == 201, response.text
    saved = response.json()
    run_id = saved["id"]
    assert saved["result"]["data"] == {"order": {"number": "PO-1042", "total": "1275.50"}}
    assert client.get(f"/api/extractions/{run_id}").json() == saved
    assert len(client.get("/api/extractions").json()) == 1
    assert client.get(f"/api/extractions/{run_id}/specification").json()["name"] == "Purchase order"
    page = client.get(f"/api/extractions/{run_id}/pages/0")
    assert page.status_code == 200
    assert page.content.startswith(b"\x89PNG")
    assert client.get(f"/api/extractions/{run_id}/pages/-1").status_code == 404
    assert client.get(f"/api/extractions/{run_id}/pages/999").status_code == 404
    assert client.get(f"/api/extractions/{run_id}/document").content.startswith(b"%PDF-")
    comparison = client.post(
        f"/api/extractions/{run_id}/compare",
        files={
            "reference": (
                "reference.json",
                (EXAMPLE / "reference.json").read_bytes(),
                "application/json",
            )
        },
        data={"rules": (EXAMPLE / "checks.json").read_text()},
    )
    assert comparison.status_code == 200, comparison.text
    assert [check["status"] for check in comparison.json()] == ["match", "match"]
    assert client.get(f"/api/extractions/{run_id}").json() == saved
    assert client.delete(f"/api/extractions/{run_id}").status_code == 204
    assert client.get(f"/api/extractions/{run_id}").status_code == 404
    assert client.get("/api/extractions").json() == []


def test_bad_specification_does_not_publish_partial_run(client, tmp_path):
    response = create(client, b'{"name":"Invalid","fields":{"total":{"type":"bad"}}}')
    assert response.status_code == 422
    assert response.json()["detail"][0]["path"]
    assert client.get("/api/extractions").json() == []
    assert not list((tmp_path / "extractions").glob("*"))


def test_invalid_pdf_does_not_publish_partial_run(client):
    response = client.post(
        "/api/extractions",
        files={
            "document": ("broken.pdf", b"%PDF-broken", "application/pdf"),
            "specification": (
                "spec.json",
                (EXAMPLE / "extraction.json").read_bytes(),
                "application/json",
            ),
        },
    )
    assert response.status_code == 422
    assert client.get("/api/extractions").json() == []


def test_schema_is_available_and_unknown_extractions_return_404(client):
    assert client.get("/api/extractions/schema").json()["title"] == "ExtractionSpecification"
    assert client.get("/api/extractions/invalid").status_code == 404


def test_rotated_pdf_preview_uses_the_extraction_coordinate_system(client):
    import struct

    import pymupdf

    with pymupdf.open(EXAMPLE / "document.pdf") as pdf:
        expected_width, expected_height = pdf[0].rect.width, pdf[0].rect.height
        pdf[0].set_rotation(90)
        content = pdf.tobytes()
    response = client.post(
        "/api/extractions",
        files={
            "document": ("rotated.pdf", content, "application/pdf"),
            "specification": (
                "spec.json",
                (EXAMPLE / "extraction.json").read_bytes(),
                "application/json",
            ),
        },
    )
    assert response.status_code == 201
    saved = response.json()
    assert saved["result"]["data"]["order"]["total"] == "1275.50"
    assert saved["result"]["pages"][0] == {"width": expected_width, "height": expected_height}
    image = client.get(f"/api/extractions/{saved['id']}/pages/0")
    width, height = struct.unpack(">II", image.content[16:24])
    assert abs(width / height - expected_width / expected_height) < 0.01
    original = client.get(f"/api/extractions/{saved['id']}/document").content
    assert original == content
