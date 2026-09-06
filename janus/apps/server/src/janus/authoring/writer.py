"""DSPy schema writers: Predict generates once; Flex can validate drafts.

Flex requires Deno and uses an experimental DSPy API."""

from __future__ import annotations

import shutil
from pathlib import Path

import dspy

from janus.authoring.metric import (
    JANUS_ROOT,
    _harness,
    evaluate_specification,
    provided_reference_for,
    summarize_tool_result,
)

_TOOL_DESCRIPTION = (
    "Validate a draft comparison specification against the real scoring "
    "pipeline: compiles the specification (with your reference_json when "
    "given, else the provided reference for the document), then runs the "
    "reader, extractor, and engine on the document. Returns compact JSON: "
    "compiles, diagnostics, fields, extracted, ambiguous, not_extracted, "
    "match, mismatch, sample_problems. Call this on drafts and fix what it "
    "reports before finalizing."
)


class SchemaWriterSignature(dspy.Signature):
    """You write comparison specifications for the Janus document-comparison tool.

    Read the grammar documentation, the task, and the document text carefully;
    pick labels that actually appear in the document. Produce a comparison
    specification as a JSON object. When reference data was provided, use it
    as-is and do NOT output a reference; otherwise, when the task asks for
    reference data, author the reference dataset as a JSON object too, and
    make every pointer in the specification resolve against it.
    """

    task: str = dspy.InputField(desc="the user's task, in their own words")
    grammar_docs: str = dspy.InputField(
        desc="grammar documentation and JSON schema for comparison specifications v1"
    )
    document_text: str = dspy.InputField(
        desc="text layer of the PDF the specification must run against"
    )
    document_path: str = dspy.InputField(
        desc="path of the document; pass it to validate_specification to check a draft"
    )
    reference_text: str = dspy.InputField(
        desc="provided reference data; empty string when the task expects you to author it"
    )
    specification_json: str = dspy.OutputField(
        desc="the comparison specification as one JSON object"
    )
    reference_json: str = dspy.OutputField(
        desc='the reference dataset as one JSON object, or "" when reference data was '
        "provided or none is needed"
    )


def _resolve_document(document_path: str) -> Path | None:
    """Resolve a tool argument to a benchmark document.

    Accepts a repo-relative path (what the examples carry in document_path),
    an absolute path, or a bare case slug.
    """
    if not document_path.strip():
        return None
    candidate = Path(document_path.strip())
    for path in (candidate, JANUS_ROOT / candidate):
        if path.is_file():
            return path
    return _harness()._document_for(candidate.stem)


def deno_available() -> bool:
    """Whether a Deno executable is reachable, mirroring dspy's own lookup."""
    if shutil.which("deno"):
        return True
    try:
        from importlib import import_module

        deno_module = import_module("deno")  # deno-py, dspy's fallback source
    except ImportError:
        return False
    try:
        return Path(deno_module.find_deno_bin()).is_file()
    except Exception:
        return False


def validate_specification(
    specification_json: str, reference_json: str = "", document_path: str = ""
) -> str:
    """Compile, extract, and compare a draft against document_path.

    Return compact status counts and sample problems for the writer."""
    document = _resolve_document(document_path)
    if document is None:
        return (
            '{"error": "unknown document path: pass the document_path from the '
            'inputs, e.g. \\"examples/purchase-order/document.pdf\\""}'
        )
    result = evaluate_specification(
        document, specification_json, reference_json, provided_reference_for(document)
    )
    return summarize_tool_result(result)


class PredictSchemaWriter(dspy.Module):
    """Stable baseline: one typed dspy.Predict over SchemaWriterSignature."""

    def __init__(self) -> None:
        super().__init__()
        self.generate = dspy.Predict(SchemaWriterSignature)

    def forward(
        self,
        task: str,
        grammar_docs: str,
        document_text: str,
        reference_text: str = "",
        document_path: str = "",
    ) -> dspy.Prediction:
        return self.generate(
            task=task,
            grammar_docs=grammar_docs,
            document_text=document_text,
            reference_text=reference_text,
            document_path=document_path,
        )


class FlexSchemaWriter(dspy.Module):
    """Let GEPA edit a Flex program that can call validate_specification.

    Construction requires Deno; importing the class does not."""

    def __init__(self) -> None:
        super().__init__()
        if not deno_available():
            raise RuntimeError(
                "FlexSchemaWriter requires Deno: dspy.Flex executes optimizer-authored "
                "code inside a Deno sandbox and no `deno` executable was found on PATH "
                "(and the deno-py package is not installed to locate one). Install Deno "
                "(https://docs.deno.com/runtime/getting_started/installation/) or use "
                "--writer predict."
            )
        self.generate = dspy.Flex(
            SchemaWriterSignature,
            tools=[
                dspy.Tool(
                    validate_specification,
                    name="validate_specification",
                    desc=_TOOL_DESCRIPTION,
                    arg_desc={
                        "specification_json": "draft comparison specification as a JSON string",
                        "reference_json": 'authored reference dataset as a JSON string, or ""',
                        "document_path": "the document_path input field (repo-relative path)",
                    },
                )
            ],
        )

    def forward(
        self,
        task: str,
        grammar_docs: str,
        document_text: str,
        reference_text: str = "",
        document_path: str = "",
    ) -> dspy.Prediction:
        return self.generate(
            task=task,
            grammar_docs=grammar_docs,
            document_text=document_text,
            reference_text=reference_text,
            document_path=document_path,
        )
