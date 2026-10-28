"""Comparing the two paths per semantic key, and the gate each field's policy sets.

| Policy | Gate |
| --- | --- |
| `critical` | `human_verify`, always. Disagreeing paths also need a second, independent human. |
| `standard` | `group_accept` when both paths extracted the same value and every validator passed; else `human_verify`. One in ten group-accepted fields go to a re-check queue. |
| `descriptive` | `human_review`: one human reviews the value and its span. |

Values agree when they are equal as exact decimals ("0.50" is "0.5") or, for text, equal up to
case and whitespace. A path that did not extract a value never agrees.
"""

from typing import TYPE_CHECKING

from kittum.core import content_hash
from kittum.extraction.model import Agreement, Gate, Reconciliation
from kittum.extraction.numbers import same_value

if TYPE_CHECKING:
    from collections.abc import Sequence

    from kittum.extraction.model import Proposal, SemanticKey
    from kittum.extraction.schema import SchemaField

RECHECK_ONE_IN = 10


def _validated(p: Proposal) -> bool:
    return all(v.passed for v in p.validators)


def agreement(janus: Proposal | None, llm: Proposal | None) -> Agreement:
    j = janus is not None and janus.status == "extracted"
    m = llm is not None and llm.status == "extracted"
    if j and m:
        assert janus is not None and llm is not None
        return "agree" if same_value(janus.value, llm.value) else "disagree"
    if j:
        return "janus_only"
    if m:
        return "llm_only"
    return "neither"


def recheck(key: SemanticKey) -> bool:
    """Whether a key is drawn for the re-check queue: a fixed one in ten, by the key's hash, so
    the draw cannot be steered and is the same on every run."""
    return (
        int(content_hash(key, domain="kittum.extraction_recheck.v1")[:8], 16) % RECHECK_ONE_IN == 0
    )


def reconcile(field: SchemaField, janus: Proposal | None, llm: Proposal | None) -> Reconciliation:
    agreed = agreement(janus, llm)
    policy = field.spec.policy
    gate: Gate
    if policy == "critical":
        gate = "human_verify"
    elif policy == "descriptive":
        gate = "human_review"
    else:
        validated = janus is not None and llm is not None and _validated(janus) and _validated(llm)
        gate = "group_accept" if agreed == "agree" and validated else "human_verify"
    return Reconciliation(
        key=field.key,
        spec=field.spec,
        agreement=agreed,
        gate=gate,
        second_extractor=policy == "critical" and agreed == "disagree",
        recheck=gate == "group_accept" and recheck(field.key),
    )


def reconcile_all(
    fields: Sequence[SchemaField], proposals: Sequence[Proposal]
) -> list[Reconciliation]:
    by_key: dict[tuple[SemanticKey, str], Proposal] = {(p.key, p.path): p for p in proposals}
    return [reconcile(f, by_key.get((f.key, "janus")), by_key.get((f.key, "llm"))) for f in fields]
