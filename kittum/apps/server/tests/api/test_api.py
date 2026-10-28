import json

from fastapi.testclient import TestClient

from kittum.api import create_app
from kittum.cli import main


def test_health() -> None:
    response = TestClient(create_app()).get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_cli_prints_openapi(capsys) -> None:
    assert main(["openapi"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert "/api/health" in document["paths"]
