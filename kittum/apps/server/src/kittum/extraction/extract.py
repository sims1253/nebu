"""Both paths over one layer, validated and reconciled. No I/O except the model calls."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kittum.core import SystemClock
from kittum.extraction import janus_path, llm_path
from kittum.extraction.model import Proposal, ProposalSet
from kittum.extraction.reconcile import reconcile_all
from kittum.extraction.validators import validate

if TYPE_CHECKING:
    from kittum.core import Clock
    from kittum.documents import Anchor, Layer
    from kittum.extraction.llm_path import AgentConfig, Transcript
    from kittum.extraction.model import Reconciliation
    from kittum.extraction.schema import ExtractionSchema, SchemaField
    from kittum.models import LlmBackend


@dataclass(frozen=True)
class Extraction:
    proposals: ProposalSet
    anchors: dict[str, Anchor]  # by ID; not yet saved
    reconciliations: list[Reconciliation]
    transcript: Transcript | None = None
    fields: tuple[SchemaField, ...] = field(default=())


def extract(
    layer: Layer,
    document_hash: str,
    schema: ExtractionSchema,
    fields: tuple[SchemaField, ...],
    *,
    backend: LlmBackend | None = None,
    agent: AgentConfig | None = None,
    clock: Clock | None = None,
) -> Extraction:
    """Run the Janus path and, with a backend and an agent config, the LLM path; validate each
    path on its own; reconcile per semantic key."""
    janus, anchors = janus_path.run(layer, document_hash, fields, schema.name)
    proposals: list[Proposal] = validate(janus, fields, schema)
    transcript = None
    if agent is not None:
        if backend is None:
            llm = [
                Proposal(
                    key=f.key,
                    value=None,
                    unit=None,
                    status="error",
                    path="llm",
                    anchor_ids=(),
                    message=f"no backend serves {agent.model_id}",
                )
                for f in fields
            ]
        else:
            llm, found, transcript = llm_path.run(
                layer, fields, backend, agent, clock or SystemClock()
            )
            anchors = {**anchors, **found}
        proposals += validate(llm, fields, schema)
    return Extraction(
        proposals=ProposalSet(layer_id=layer.id, proposals=tuple(proposals)),
        anchors=anchors,
        reconciliations=reconcile_all(fields, proposals),
        transcript=transcript,
        fields=fields,
    )
