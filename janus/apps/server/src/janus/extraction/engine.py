"""Extract nested objects and physical table records through one public interface."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from janus.comparison.content import PageContent, TableCell
from janus.comparison.extractor import SchemaExtractor
from janus.comparison.models import (
    DocumentEvidence,
    EvidenceLocation,
    ExtractionField,
    ExtractionPlan,
    ExtractionStatus,
    ValueRule,
)
from janus.comparison.reader import TextLayerDocumentReader
from janus.extraction.models import (
    ExtractionSpecification,
    FieldResult,
    ObjectField,
    OutputField,
    PageSize,
    RecordsField,
    Scalar,
    ScalarField,
    StructuredExtraction,
)
from janus.extraction.parsing import parse_value

if TYPE_CHECKING:
    from pathlib import Path


def pointer(parent: str, key: str) -> str:
    return f"{parent}/{key.replace('~', '~0').replace('/', '~1')}"


def _fold(text: str) -> str:
    return " ".join(text.casefold().split())


def extract(document: Path, specification: ExtractionSpecification) -> StructuredExtraction:
    """Read a PDF and extract data. Does not create a review or write files."""
    evidence = TextLayerDocumentReader().read(document, document.name)
    return extract_evidence(evidence, specification)


def extract_evidence(
    evidence: DocumentEvidence,
    specification: ExtractionSpecification,
) -> StructuredExtraction:
    """Extract from reader evidence, retaining source locations and unresolved fields."""
    pages = [PageContent.model_validate(page) for page in evidence.pages]
    spec_hash = hashlib.sha256(specification.model_dump_json().encode()).hexdigest()
    scalars: dict[str, ScalarField] = {}

    def collect(fields: dict[str, OutputField], parent: str = "") -> None:
        for key, field in fields.items():
            path = pointer(parent, key)
            if isinstance(field, ScalarField):
                scalars[path] = field
            elif isinstance(field, ObjectField):
                collect(field.fields, path)

    collect(specification.fields)
    plan = ExtractionPlan(
        specification_id=spec_hash,
        extraction_hash=spec_hash,
        fields=[
            ExtractionField(
                id=path,
                section_key="root",
                section_label=specification.name,
                key=path,
                label=path,
                # Component parsing happens once, with typed conversion below.
                value=ValueRule(type=field.type),
                locate=field.locate,
            )
            for path, field in scalars.items()
        ],
    )
    extracted = {f.field_id: f for f in SchemaExtractor().extract(plan, evidence).fields}
    results: list[FieldResult] = []

    def convert(raw: str, rule: Scalar, path: str, source: EvidenceLocation | None) -> Any:
        try:
            value = parse_value(raw, rule)
        except ValueError as exc:
            results.append(
                FieldResult(
                    path=path, type=rule.type, status="invalid", source=source, message=str(exc)
                )
            )
            return None
        results.append(FieldResult(path=path, type=rule.type, status="extracted", source=source))
        return value

    def records(rule: RecordsField, path: str) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        matched_tables = 0
        for page in pages:
            for table_index, table in enumerate(page.tables):
                rows: dict[int, dict[int, TableCell]] = {}
                for cell in table.cells:
                    rows.setdefault(cell.row, {})[cell.column] = cell
                if not rows:
                    continue
                header_row = min(rows)
                header = rows[header_row]
                columns: dict[str, int] = {}
                duplicate = False
                for key, column in rule.columns.items():
                    aliases = {_fold(label) for label in column.labels}
                    matches = [
                        index for index, cell in header.items() if _fold(cell.text) in aliases
                    ]
                    if len(matches) > 1:
                        duplicate = True
                    if len(matches) == 1:
                        columns[key] = matches[0]
                if duplicate:
                    # Only report a candidate table when every requested header occurs.
                    if all(
                        any(
                            _fold(c.text) in {_fold(s) for s in col.labels} for c in header.values()
                        )
                        for col in rule.columns.values()
                    ):
                        results.append(
                            FieldResult(
                                path=path,
                                type="records",
                                status="ambiguous",
                                message=f"Page {page.page_number + 1} has duplicate matching column headers.",
                            )
                        )
                    continue
                if len(columns) != len(rule.columns):
                    continue
                matched_tables += 1
                for row_number, row in sorted(rows.items()):
                    if row_number == header_row or not any(c.text.strip() for c in row.values()):
                        continue
                    # A header repeated within a table is not a data record.
                    if all(
                        _fold(row.get(index, TableCell(text="", row=0, column=0)).text)
                        in {_fold(s) for s in rule.columns[key].labels}
                        for key, index in columns.items()
                    ):
                        continue
                    if len(output) >= rule.max_rows:
                        results.append(
                            FieldResult(
                                path=path,
                                type="records",
                                status="error",
                                message=f"Stopped at {rule.max_rows} records. Increase max_rows to include the remaining rows.",
                            )
                        )
                        return output
                    record: dict[str, Any] = {}
                    for key, column_index in columns.items():
                        field_path = pointer(f"{path}/{len(output)}", key)
                        cell = row.get(column_index)
                        if cell is None or not cell.text.strip():
                            record[key] = None
                            results.append(
                                FieldResult(
                                    path=field_path,
                                    type=rule.columns[key].type,
                                    status="missing",
                                    message="The table cell is empty.",
                                )
                            )
                            continue
                        source = EvidenceLocation(
                            page=page.page_number,
                            table_index=table_index,
                            row=row_number,
                            column=column_index,
                            raw_text=cell.text,
                            cell_bbox=cell.bbox.model_dump() if cell.bbox else None,
                        )
                        record[key] = convert(cell.text, rule.columns[key], field_path, source)
                    output.append(record)
        if not matched_tables and not any(f.path == path for f in results):
            results.append(
                FieldResult(
                    path=path,
                    type="records",
                    status="missing",
                    message="No table contains all the requested column headers.",
                )
            )
        return output

    def build(fields: dict[str, OutputField], parent: str = "") -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, field in fields.items():
            path = pointer(parent, key)
            if isinstance(field, ObjectField):
                output[key] = build(field.fields, path)
            elif isinstance(field, RecordsField):
                output[key] = records(field, path)
            else:
                item = extracted[path]
                if item.status is ExtractionStatus.EXTRACTED:
                    output[key] = convert(item.raw_document_value or "", field, path, item.location)
                else:
                    output[key] = None
                    state = (
                        "missing"
                        if item.status is ExtractionStatus.NOT_EXTRACTED
                        else item.status.value
                    )
                    results.append(
                        FieldResult(
                            path=path,
                            type=field.type,
                            status=state,
                            source=item.location,
                            message=item.explanation,
                        )
                    )
        return output

    data = build(specification.fields)
    return StructuredExtraction(
        specification_name=specification.name,
        specification_hash=spec_hash,
        document_hash=evidence.document_hash,
        data=data,
        fields=results,
        pages=[PageSize(width=page.width or 612, height=page.height or 792) for page in pages],
    )
