"""Local extraction workbench. Completed runs are published atomically on disk."""

from __future__ import annotations

import asyncio
import re
import shutil
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf
from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import TypeAdapter, ValidationError

from janus.comparison.reference import ReferenceSource, normalize_reference
from janus.config import data_root
from janus.extraction import CheckRule, ExtractionSpecification, compare, extract
from janus.extraction.models import CheckResult, ExtractionSummary, SavedExtraction

router = APIRouter(prefix="/api/extractions", tags=["extractions"])


def _root() -> Path:
    return data_root() / "extractions"


def _directory(run_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise HTTPException(404, "Extraction not found.")
    directory = _root() / run_id
    if not (directory / "result.json").is_file():
        raise HTTPException(404, "Extraction not found.")
    return directory


def _load(directory: Path) -> SavedExtraction:
    return SavedExtraction.model_validate_json((directory / "result.json").read_bytes())


async def _read(upload: UploadFile, allowed: set[str], limit: int) -> bytes:
    if Path(upload.filename or "").suffix.lower() not in allowed:
        raise HTTPException(422, f"Choose a file ending in {', '.join(sorted(allowed))}.")
    content = bytearray()
    while chunk := await upload.read(1024 * 1024):
        content.extend(chunk)
        if len(content) > limit:
            raise HTTPException(413, f"File exceeds the {limit // (1024 * 1024)} MB limit.")
    if not content:
        raise HTTPException(422, "The uploaded file is empty.")
    return bytes(content)


def _run(document: bytes, filename: str, specification: bytes) -> SavedExtraction:
    spec = ExtractionSpecification.model_validate_json(specification)
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    with TemporaryDirectory(prefix=".extract-", dir=root) as working:
        directory = Path(working)
        pdf = directory / "document.pdf"
        pdf.write_bytes(document)
        with pymupdf.open(pdf) as parsed:
            if parsed.needs_pass:
                raise ValueError("Unlock the PDF before extracting it.")
            if not parsed.page_count:
                raise ValueError("The PDF contains no pages.")
        result = extract(pdf, spec)
        saved = SavedExtraction(id=run_id, document_filename=filename, result=result)
        (directory / "specification.json").write_text(spec.model_dump_json(indent=2))
        (directory / "result.json").write_text(saved.model_dump_json(indent=2))
        directory.rename(root / run_id)
    return saved


@router.post("", response_model=SavedExtraction, status_code=201)
async def create_extraction(
    document: UploadFile = File(...),
    specification: UploadFile = File(...),
) -> SavedExtraction:
    pdf = await _read(document, {".pdf"}, 50 * 1024 * 1024)
    spec = await _read(specification, {".json"}, 2 * 1024 * 1024)
    try:
        return await asyncio.to_thread(_run, pdf, document.filename or "document.pdf", spec)
    except ValidationError as exc:
        raise HTTPException(
            422,
            [
                {"path": "/" + "/".join(map(str, e["loc"])), "message": e["msg"]}
                for e in exc.errors()
            ],
        ) from exc
    except (ValueError, pymupdf.FileDataError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("", response_model=list[ExtractionSummary])
async def list_extractions() -> list[ExtractionSummary]:
    def read() -> list[ExtractionSummary]:
        runs = []
        for path in _root().glob("*/result.json"):
            if path.parent.name.startswith("."):
                continue
            saved = _load(path.parent)
            runs.append(
                ExtractionSummary(
                    id=saved.id,
                    document_filename=saved.document_filename,
                    specification_name=saved.result.specification_name,
                    created_at=saved.result.created_at,
                    fields=len(saved.result.fields),
                    issues=sum(f.status != "extracted" for f in saved.result.fields),
                )
            )
        return sorted(runs, key=lambda run: run.created_at, reverse=True)

    return await asyncio.to_thread(read)


@router.get("/schema")
async def specification_schema() -> dict:
    return ExtractionSpecification.model_json_schema()


@router.get("/{run_id}", response_model=SavedExtraction)
async def get_extraction(run_id: str) -> SavedExtraction:
    return await asyncio.to_thread(_load, _directory(run_id))


@router.get("/{run_id}/specification")
async def get_specification(run_id: str) -> Response:
    content = await asyncio.to_thread((_directory(run_id) / "specification.json").read_bytes)
    return Response(content, media_type="application/json")


@router.get("/{run_id}/document")
async def get_document(run_id: str) -> FileResponse:
    return FileResponse(_directory(run_id) / "document.pdf", media_type="application/pdf")


@router.get("/{run_id}/pages/{page}")
async def get_page(run_id: str, page: int) -> Response:
    document = _directory(run_id) / "document.pdf"

    def render() -> bytes:
        with pymupdf.open(document) as pdf:
            if not 0 <= page < pdf.page_count:
                raise HTTPException(404, "PDF page not found.")
            source = pdf[page]
            source.set_rotation(0)
            scale = min(2.0, 1800 / max(source.rect.width, source.rect.height))
            return source.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes(
                "png"
            )

    return Response(
        await asyncio.to_thread(render),
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.post("/{run_id}/compare", response_model=list[CheckResult])
async def compare_extraction(
    run_id: str,
    reference: UploadFile = File(...),
    rules: str = Form(...),
) -> list[CheckResult]:
    directory = _directory(run_id)
    content = await _read(reference, {".json", ".csv", ".xlsx"}, 10 * 1024 * 1024)

    def check() -> list[CheckResult]:
        parsed_rules = TypeAdapter(list[CheckRule]).validate_json(rules)
        tree, _ = normalize_reference(
            ReferenceSource(content, reference.filename or "reference.json")
        )
        return compare(_load(directory).result, tree, parsed_rules)

    try:
        return await asyncio.to_thread(check)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/{run_id}", status_code=204)
async def delete_extraction(run_id: str) -> None:
    await asyncio.to_thread(shutil.rmtree, _directory(run_id))
