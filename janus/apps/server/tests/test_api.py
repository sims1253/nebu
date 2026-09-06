from __future__ import annotations

import json
import time

import fitz
import pytest
from fastapi.testclient import TestClient

from janus.main import app
from janus.store.artifact_store import ReviewArtifactStore, set_artifact_store
from janus.store.review_store import InMemoryReviewStore, set_review_store


def inputs() -> tuple[bytes, bytes]:
    specification = json.dumps(
        {
            "schema_version": "1",
            "name": "Order check",
            "sections": [
                {
                    "key": "summary",
                    "label": "Summary",
                    "fields": [
                        {
                            "key": "number",
                            "label": "Order number",
                            "reference": {"pointer": "/order/id"},
                            "value": {"type": "text"},
                            "locate": {"strategy": "table_label", "labels": ["Order number"]},
                            "compare": {"operator": "exact"},
                        }
                    ],
                }
            ],
        }
    ).encode()
    return specification, b'{"order":{"id":"PO-1"}}'


def pdf() -> bytes:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Order number: PO-1")
    content = document.tobytes()
    document.close()
    return content


@pytest.fixture(autouse=True)
def isolated_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("JANUS_UPLOAD_DIR", str(tmp_path / "uploads"))
    set_review_store(InMemoryReviewStore())
    set_artifact_store(ReviewArtifactStore(results_dir=tmp_path / "artifacts"))
    yield
    set_review_store(None)
    set_artifact_store(None)


def test_validate_only_returns_compiled_identity() -> None:
    specification, reference = inputs()
    with TestClient(app) as client:
        response = client.post(
            "/api/reviews/validate",
            files={
                "specification": ("specification.json", specification, "application/json"),
                "reference": ("reference.json", reference, "application/json"),
            },
        )
    assert response.status_code == 200
    assert response.json()["field_count"] == 1


def test_creation_uses_three_inputs_and_reaches_ready() -> None:
    specification, reference = inputs()
    with TestClient(app) as client:
        response = client.post(
            "/api/reviews",
            files={
                "document": ("order.pdf", pdf(), "application/pdf"),
                "specification": ("specification.json", specification, "application/json"),
                "reference": ("reference.json", reference, "application/json"),
            },
        )
        assert response.status_code == 201
        review_id = response.json()["id"]
        for _ in range(50):
            metadata = client.get(f"/api/reviews/{review_id}").json()
            if metadata["status"] in {"ready", "error"}:
                break
            time.sleep(0.01)
        assert metadata["status"] == "ready"
        result = client.get(f"/api/reviews/{review_id}/result")
        assert result.status_code == 200
        assert result.json()["comparisons"][0]["status"] == "not_compared"
