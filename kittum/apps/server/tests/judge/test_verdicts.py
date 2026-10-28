"""The judge on recorded completions: which exclusions stand and which become unclear."""

import json

import httpx2
import pytest

from kittum.judge import Verdict, applicable, build_request, judge
from kittum.models import (
    Confidentiality,
    ConfidentialityError,
    ModelRegistry,
    OpenAICompatibleBackend,
)
from kittum.models.testing import RecordedBackend
from kittum.storage import MemoryStore
from tests.judge.records import (
    ADOLESCENTS,
    ADOLESCENTS_PARAPHRASED,
    CHEMO_NAIVE,
    CONFIG,
    CRITERIA,
    EXCLUDED_CHEMO_NAIVE,
    INJECTED,
    SCOPE,
    answer,
    recorded,
)


def _by_id(verdict: Verdict) -> dict[str, object]:
    return {c.criterion_id: c for c in verdict.criteria}


def test_a_verified_not_met_quote_excludes() -> None:
    backend = recorded(ADOLESCENTS)
    verdict = judge(ADOLESCENTS, SCOPE, CRITERIA, CONFIG, backend)

    assert verdict.excludes
    pop = _by_id(verdict)["C_POP_ADULT_NSCLC"]
    assert pop.status == "not_met"
    assert pop.quote == "patients aged 12 to 17 years"
    assert pop.quote_verified
    assert pop.downgraded_from is None
    assert verdict.model_identity == "Qwen/Qwen3.8-27B"
    assert verdict.flags == ()
    # The full-text-only criterion is not asked at title and abstract.
    assert [c.criterion_id for c in verdict.criteria] == [
        "C_POP_ADULT_NSCLC",
        "C_PRIOR_CHEMO",
        "C_RCT",
    ]
    (request,) = backend.requests
    assert set(request.json_schema["required"]) == {"C_POP_ADULT_NSCLC", "C_PRIOR_CHEMO", "C_RCT"}
    assert request.temperature == 0.0
    assert request.seed == 0


def test_a_paraphrased_quote_is_downgraded_and_does_not_exclude() -> None:
    verdict = judge(
        ADOLESCENTS_PARAPHRASED, SCOPE, CRITERIA, CONFIG, recorded(ADOLESCENTS_PARAPHRASED)
    )
    pop = _by_id(verdict)["C_POP_ADULT_NSCLC"]
    assert pop.status == "unclear"
    assert pop.downgraded_from == "not_met"
    assert pop.failed_check == "quote_not_found"
    assert not pop.quote_verified
    assert pop.quote == "participants were adolescents aged 12-17"  # kept for the reviewer
    assert not verdict.excludes
    assert _by_id(verdict)["C_RCT"].status == "met"


def test_a_quote_negated_by_its_sentence_is_downgraded() -> None:
    """ "We excluded patients without prior chemotherapy" means everyone had chemotherapy."""
    verdict = judge(EXCLUDED_CHEMO_NAIVE, SCOPE, CRITERIA, CONFIG, recorded(EXCLUDED_CHEMO_NAIVE))
    chemo = _by_id(verdict)["C_PRIOR_CHEMO"]
    assert chemo.quote_verified  # the words are in the record ...
    assert chemo.status == "unclear"  # ... but they do not say what the model claims
    assert chemo.downgraded_from == "not_met"
    assert chemo.failed_check == "negation"
    assert not verdict.excludes


def test_the_same_quote_stating_a_real_absence_still_excludes() -> None:
    verdict = judge(CHEMO_NAIVE, SCOPE, CRITERIA, CONFIG, recorded(CHEMO_NAIVE))
    chemo = _by_id(verdict)["C_PRIOR_CHEMO"]
    assert chemo.status == "not_met"
    assert chemo.failed_check is None
    assert verdict.excludes


def test_a_prompt_injection_in_the_abstract_cannot_exclude() -> None:
    backend = recorded(INJECTED)
    verdict = judge(INJECTED, SCOPE, CRITERIA, CONFIG, backend)

    assert not verdict.excludes
    assert verdict.flags == ("injection_suspected",)
    for cid in ("C_POP_ADULT_NSCLC", "C_RCT"):
        c = _by_id(verdict)[cid]
        assert c.status == "unclear"
        assert c.downgraded_from == "not_met"
        assert c.failed_check == "injection_suspected"
    assert _by_id(verdict)["C_POP_ADULT_NSCLC"].quote_verified  # verbatim, yet not trusted

    # The record sits in the user message only, inside a tag the record cannot close.
    system, user = backend.requests[0].messages
    assert "ignore all previous instructions" not in system.content
    tag = user.content.split("The record follows.\n<", 1)[1].split(">", 1)[0]
    assert tag.startswith("record-")
    assert user.content.count(f"</{tag}>") == 1
    assert user.content.rstrip().endswith(f"</{tag}>")


def test_malformed_output_is_retried_with_a_new_seed_then_recorded_unclear() -> None:
    chosen = applicable(CRITERIA, SCOPE, ADOLESCENTS)
    first, second = (build_request(ADOLESCENTS, chosen, CONFIG, attempt=n) for n in (0, 1))
    truncated = '{"C_POP_ADULT_NSCLC": {"quote": "patients aged 12 to 17 years", "rationale": "Th'
    missing = answer(C_POP_ADULT_NSCLC=("not_met", "patients aged 12 to 17 years", "Teens."))
    backend = RecordedBackend(
        {RecordedBackend.key(first): truncated, RecordedBackend.key(second): missing}
    )

    verdict = judge(ADOLESCENTS, SCOPE, CRITERIA, CONFIG, backend)

    assert [r.seed for r in backend.requests] == [0, 1]
    assert not verdict.excludes
    assert verdict.flags == ("invalid_output",)
    assert {c.status for c in verdict.criteria} == {"unclear"}
    assert {c.failed_check for c in verdict.criteria} == {"invalid_output"}
    assert len(verdict.criteria) == 3


def test_a_retry_that_returns_valid_json_is_used() -> None:
    chosen = applicable(CRITERIA, SCOPE, ADOLESCENTS)
    first, second = (build_request(ADOLESCENTS, chosen, CONFIG, attempt=n) for n in (0, 1))
    valid = json.loads(recorded(ADOLESCENTS).complete(first).text)
    backend = RecordedBackend(
        {
            RecordedBackend.key(first): "I need more information to decide.",
            RecordedBackend.key(second): "<think>Adolescents only.</think>\n" + json.dumps(valid),
        }
    )
    verdict = judge(ADOLESCENTS, SCOPE, CRITERIA, CONFIG, backend)
    assert verdict.excludes
    assert len(backend.requests) == 2


@pytest.mark.parametrize(
    ("status", "quote", "check"),
    [
        ("not_met", "", "no_quote"),
        ("not_met", "Median progression-free survival was 5.1 months", "off_criterion"),
    ],
)
def test_unsupported_exclusions_are_downgraded(status: str, quote: str, check: str) -> None:
    text = answer(
        C_POP_ADULT_NSCLC=(status, quote, "Not adults."),
        C_PRIOR_CHEMO=("unclear", "", "Not stated."),
        C_RCT=("met", "randomised phase 2 trial", "Randomised."),
    )
    backend = recorded(ADOLESCENTS, answers={ADOLESCENTS.item_ref: text})
    pop = _by_id(judge(ADOLESCENTS, SCOPE, CRITERIA, CONFIG, backend))["C_POP_ADULT_NSCLC"]
    assert (pop.status, pop.downgraded_from, pop.failed_check) == ("unclear", "not_met", check)


def test_a_local_only_record_is_never_sent_to_a_hosted_model() -> None:
    hosted = (
        ModelRegistry.load()
        .get(CONFIG.model_id)
        .model_copy(update={"serving": "hosted", "served_name": "qwen3.8-27b"})
    )
    sent: list[httpx2.Request] = []
    client = httpx2.Client(transport=httpx2.MockTransport(lambda r: sent.append(r) or None))
    backend = OpenAICompatibleBackend(hosted, "https://eu.example/v1", MemoryStore(), client=client)
    csr = ADOLESCENTS.model_copy(update={"confidentiality": Confidentiality.LOCAL_ONLY})

    with pytest.raises(ConfidentialityError):
        judge(csr, SCOPE, CRITERIA, CONFIG, backend)
    assert sent == []
