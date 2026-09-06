"""Compile user inputs into a comparison plan."""

from __future__ import annotations

import hashlib
import json
from importlib import resources

import jsonschema
from pydantic import ValidationError

from janus.comparison.models import (
    ComparisonPlan,
    ComparisonSpecification,
    CompilationDiagnostic,
    CompilationError,
    PlannedField,
    field_identifier,
)
from janus.comparison.reference import (
    ReferenceDataError,
    ReferenceSource,
    normalize_reference,
    resolve_json_pointer,
)

_SCHEMA_RESOURCE = "comparison-specification.v1.json"
_validator = None


def _schema_validator():
    """Load the canonical schema bundled with the package.

    A test pins this copy byte-for-byte to packages/contracts/schema, so the
    server always enforces the published contract.
    """
    global _validator
    if _validator is None:
        text = resources.files("janus.schemas").joinpath(_SCHEMA_RESOURCE).read_text()
        schema = json.loads(text)
        jsonschema.Draft202012Validator.check_schema(schema)
        _validator = jsonschema.Draft202012Validator(schema)
    return _validator


def _hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


class SpecificationCompiler:
    """Turns a specification plus reference data into a ComparisonPlan:
    schema validation, JSON Pointer binding against the reference tree, applied
    defaults, and stable per-field ids."""

    def compile(self, specification: bytes, reference: ReferenceSource) -> ComparisonPlan:
        """Compile raw inputs into a plan.

        Raises CompilationError with one diagnostic per problem when the
        specification is not valid JSON, fails the schema, or a required
        reference pointer does not resolve.
        """
        try:
            raw_specification = json.loads(specification.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CompilationError(
                [
                    CompilationDiagnostic(
                        code="INVALID_SPECIFICATION_JSON",
                        specification_path="/",
                        message=f"The comparison specification is not valid JSON: {exc}",
                        remediation_hint="Provide a UTF-8 encoded JSON object.",
                    )
                ]
            ) from exc

        schema_errors = sorted(
            _schema_validator().iter_errors(raw_specification),
            key=lambda error: list(error.absolute_path),
        )
        if schema_errors:
            raise CompilationError(
                [
                    CompilationDiagnostic(
                        code="INVALID_SPECIFICATION_SCHEMA",
                        specification_path="/"
                        + "/".join(str(part) for part in error.absolute_path),
                        message=error.message,
                        remediation_hint="Correct the field to match comparison specification v1.",
                    )
                    for error in schema_errors[:20]
                ]
            )

        try:
            spec = ComparisonSpecification.model_validate(raw_specification)
        except ValidationError as exc:
            diagnostics = [
                CompilationDiagnostic(
                    code="INVALID_SPECIFICATION",
                    specification_path="/" + "/".join(str(part) for part in error["loc"]),
                    message=error["msg"],
                    remediation_hint="Correct the field to match comparison specification v1.",
                )
                for error in exc.errors()
            ]
            raise CompilationError(diagnostics) from exc

        try:
            reference_tree, reference_format = normalize_reference(reference)
        except ReferenceDataError as exc:
            raise CompilationError(
                [
                    CompilationDiagnostic(
                        code="INVALID_REFERENCE_DATA",
                        specification_path="/",
                        message=str(exc),
                        remediation_hint="Provide valid JSON, CSV, or XLSX reference data.",
                    )
                ]
            ) from exc

        canonical_spec = spec.model_dump(mode="json", by_alias=True, exclude_none=True)
        specification_id = _hash(canonical_spec)
        reference_hash = hashlib.sha256(reference.content).hexdigest()
        diagnostics: list[CompilationDiagnostic] = []
        fields: list[PlannedField] = []
        for section_index, section in enumerate(spec.sections):
            for field_index, field in enumerate(section.fields):
                present, expected = resolve_json_pointer(reference_tree, field.reference.pointer)
                path = f"/sections/{section_index}/fields/{field_index}/reference/pointer"
                if not present and field.reference.required:
                    diagnostics.append(
                        CompilationDiagnostic(
                            code="REFERENCE_POINTER_NOT_FOUND",
                            specification_path=path,
                            reference_pointer=field.reference.pointer,
                            message=f"Reference pointer {field.reference.pointer!r} does not exist.",
                            remediation_hint=(
                                "Correct the pointer, add the value to the reference dataset, "
                                "or set reference.required to false."
                            ),
                        )
                    )
                    continue
                if field.rows is not None and present and not isinstance(expected, dict):
                    # A rows field binds each generated row to a key of the
                    # pointed value, so the pointer must address an object —
                    # checkable here, against the reference itself.
                    diagnostics.append(
                        CompilationDiagnostic(
                            code="REFERENCE_POINTER_NOT_OBJECT",
                            specification_path=path,
                            reference_pointer=field.reference.pointer,
                            message=(
                                f"Field {field.key!r} generates rows, but reference pointer "
                                f"{field.reference.pointer!r} does not address an object."
                            ),
                            remediation_hint=(
                                "Point a rows field at a JSON object whose keys are the "
                                "generated row keys, or remove the rows generator."
                            ),
                        )
                    )
                    continue
                field_id = field_identifier(specification_id, section.key, field.key)
                fields.append(
                    PlannedField(
                        id=field_id,
                        section_key=section.key,
                        section_label=section.label,
                        key=field.key,
                        label=field.label,
                        reference_pointer=field.reference.pointer,
                        reference_required=field.reference.required,
                        expected_value=expected,
                        expected_value_present=present,
                        value=field.value,
                        locate=field.locate,
                        compare=field.compare,
                        rows=field.rows,
                    )
                )
        if diagnostics:
            raise CompilationError(diagnostics)

        # Field IDs include the whole specification hash, so specification
        # edits invalidate extraction reuse even if only comparison rules change.
        extraction_projection = [
            {
                "id": field.id,
                "value": field.value.model_dump(mode="json"),
                "locate": field.locate.model_dump(mode="json"),
                **({"rows": field.rows.model_dump(mode="json")} if field.rows else {}),
            }
            for field in fields
        ]
        comparison_projection = [
            {
                "id": field.id,
                "expected": field.expected_value,
                "present": field.expected_value_present,
                "compare": field.compare.model_dump(mode="json"),
                **({"rows": field.rows.model_dump(mode="json")} if field.rows else {}),
            }
            for field in fields
        ]
        return ComparisonPlan(
            specification_id=specification_id,
            specification_name=spec.name,
            reference_format=reference_format,
            reference_hash=reference_hash,
            extraction_hash=_hash(extraction_projection),
            comparison_hash=_hash(comparison_projection),
            fields=fields,
        )
