"""The AI judge for screening (DESIGN.md, "Screening and the AI judge").

The judge answers each criterion as `met`, `not_met`, or `unclear` with a verbatim quote and a
one-sentence rationale, in one schema-constrained call per record and scope. An exclusion counts
only if a `not_met` quote is found in the record by `kittum.core.find_quote` and passes the
criterion's keyword and negation checks, and the record holds no text that tries to instruct
the model. Anything else becomes `unclear` with `downgraded_from` and `failed_check` set, and
`unclear` at title and abstract means include. A malformed answer is retried with the next seed,
then recorded as `unclear` for every criterion.

`judge` runs one record. `judge_many` runs a batch with bounded concurrency, writes each verdict
with its raw outputs and a `judge.verdict_recorded` event, and skips records whose cache key
(stage version, input, protocol, rendered prompt, weights, decoding, schema) is already recorded.
`build_request` renders the exact request, for replay and for recorded tests.
`VERIFIER_LIMITS` lists what the filter cannot catch, for the AI disclosure.

`judge_metrics`, `pair_metrics`, and `fleiss_kappa` measure judges on gold data: recall,
specificity, work saved, the double-fault rate, the OR rule, and run-to-run agreement.

Owner: WP-08.
"""

from typing import TYPE_CHECKING

from kittum.judge.metrics import (
    JudgeMetrics,
    PairMetrics,
    cohen_kappa,
    fleiss_kappa,
    judge_metrics,
    pair_metrics,
)
from kittum.judge.prompt import PROMPT_VERSION, answer_schema, build_request
from kittum.judge.run import applicable, run_judge
from kittum.judge.store import (
    VERDICT_RECORDED,
    JudgeConfigConflict,
    cache_key,
    judge_many,
    load_verdicts,
    register_config,
)
from kittum.judge.types import (
    CriterionStatus,
    CriterionVerdict,
    FailedCheck,
    JudgeConfig,
    ScreeningItem,
    Verdict,
)
from kittum.judge.verify import LIMITS as VERIFIER_LIMITS

if TYPE_CHECKING:
    from kittum.models import LlmBackend
    from kittum.protocol import Criterion


def judge(
    item: ScreeningItem,
    scope_id: str,
    criteria: tuple[Criterion, ...],
    config: JudgeConfig,
    backend: LlmBackend,
) -> Verdict:
    """Judge one record against the criteria that apply to the scope at its stage."""
    return run_judge(item, scope_id, criteria, config, backend).verdict


__all__ = [
    "PROMPT_VERSION",
    "VERDICT_RECORDED",
    "VERIFIER_LIMITS",
    "CriterionStatus",
    "CriterionVerdict",
    "FailedCheck",
    "JudgeConfig",
    "JudgeConfigConflict",
    "JudgeMetrics",
    "PairMetrics",
    "ScreeningItem",
    "Verdict",
    "answer_schema",
    "applicable",
    "build_request",
    "cache_key",
    "cohen_kappa",
    "fleiss_kappa",
    "judge",
    "judge_many",
    "judge_metrics",
    "load_verdicts",
    "pair_metrics",
    "register_config",
]
