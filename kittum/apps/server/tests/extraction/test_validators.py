"""Validators on the Vitamin C trial's real Janus values, with planted errors."""

import pytest

from kittum.core import new_id
from kittum.extraction import extract, validate
from tests.extraction.support import schema


@pytest.fixture
def vitamin_c(env):
    """The Janus proposals for Table 3, with each arm's N as the trial reports it (355 per arm,
    710 in total), which Janus cannot locate because Table 3 prints N only in a header row."""
    s = schema("vitamin_c")
    fields = s.fields(new_id(), new_id())
    stored = env.stored["vitamin_c"]
    janus = list(extract(stored.layer, "0" * 64, s, fields).proposals.proposals)
    for i, p in enumerate(janus):
        if p.key.quantity == "n_randomized":
            value = "710" if p.key.arm == "total" else "355"
            anchor = next(q for q in janus if q.anchor_ids).anchor_ids  # any anchor will do here
            janus[i] = p.model_copy(
                update={
                    "status": "extracted",
                    "value": value,
                    "unit": "participants",
                    "anchor_ids": anchor,
                }
            )
    return s, fields, [p.model_copy(update={"validators": ()}) for p in janus]


def _results(proposals):
    """{(endpoint, arm, quantity): {validator: (passed, message)}} for the checked proposals. A
    proposal checked twice by one validator (an arm's N, against two endpoints) shows a failure
    if either check failed."""
    out: dict = {}
    for p in proposals:
        for v in p.validators:
            checks = out.setdefault((p.key.endpoint, p.key.arm, p.key.quantity), {})
            if checks.get(v.validator, (True, None))[0]:
                checks[v.validator] = (v.passed, v.message)
    return out


def _plant(proposals, endpoint, arm, quantity, **update):
    return [
        p.model_copy(update=update)
        if (p.key.endpoint, p.key.arm, p.key.quantity) == (endpoint, arm, quantity)
        else p
        for p in proposals
    ]


HEMATOMA_RR = ("any_hematoma", "vitamin_c_vs_control", "risk_ratio")


def test_the_reported_values_pass_every_check(vitamin_c):
    s, fields, proposals = vitamin_c
    results = _results(validate(proposals, fields, s))
    assert results[("any_hematoma", "vitamin_c", "events")] == {
        "exact_value": (True, None),
        "events_at_most_n": (True, None),
        "percent_recomputes": (True, None),  # 200 / 355 = 56.34%, printed 56.3
    }
    assert results[("randomization", "total", "n_randomized")] == {
        "exact_value": (True, None),
        "arms_sum_to_total": (True, None),
    }
    assert results[HEMATOMA_RR] == {
        "exact_value": (True, None),
        "ci_brackets_estimate": (True, None),
        "ratio_log_symmetric": (True, None),  # 1.124 (0.978, 1.290)
    }
    assert all(passed for checks in results.values() for passed, _ in checks.values())


def test_planted_errors_are_caught(vitamin_c):
    s, fields, proposals = vitamin_c

    def failed(planted):
        return {
            (key, name): message
            for key, checks in _results(validate(planted, fields, s)).items()
            for name, (passed, message) in checks.items()
            if not passed
        }

    # More events than participants randomized to the arm.
    assert failed(_plant(proposals, "any_hematoma", "control", "events", value="378")) == {
        (("any_hematoma", "control", "events"), "events_at_most_n"): "378 events exceed N = 355",
        (
            ("randomization", "control", "n_randomized"),
            "events_at_most_n",
        ): "378 events exceed N = 355",
        (("any_hematoma", "control", "events"), "percent_recomputes"): (
            "378 / 355 is 106.48%, not 50.1%: check whether N and the events come from the same "
            "population"
        ),
        (("any_hematoma", "control", "percent"), "percent_recomputes"): (
            "378 / 355 is 106.48%, not 50.1%: check whether N and the events come from the same "
            "population"
        ),
        (("randomization", "control", "n_randomized"), "percent_recomputes"): (
            "378 / 355 is 106.48%, not 50.1%: check whether N and the events come from the same "
            "population"
        ),
    }

    # The percentage of another population: 200 of 352 analysed is 56.8%, not 56.3%.
    swapped = failed(_plant(proposals, "randomization", "vitamin_c", "n_randomized", value="352"))
    assert set(swapped) == {
        (("any_hematoma", "vitamin_c", "percent"), "percent_recomputes"),
        (("any_hematoma", "vitamin_c", "events"), "percent_recomputes"),
        (("randomization", "vitamin_c", "n_randomized"), "percent_recomputes"),
        (("randomization", "total", "n_randomized"), "arms_sum_to_total"),
        (("randomization", "vitamin_c", "n_randomized"), "arms_sum_to_total"),
        (("randomization", "control", "n_randomized"), "arms_sum_to_total"),
    }
    assert swapped[(("randomization", "total", "n_randomized"), "arms_sum_to_total")] == (
        "the arms add up to 707, not 710"
    )

    # A CI that does not contain its estimate.
    assert failed(
        _plant(proposals, "any_hematoma", "vitamin_c_vs_control", "ci_lower", value="1.178")
    ) == {
        (key, "ci_brackets_estimate"): "the CI (1.178, 1.290) does not contain the estimate 1.124"
        for key in (HEMATOMA_RR, (*HEMATOMA_RR[:2], "ci_lower"), (*HEMATOMA_RR[:2], "ci_upper"))
    }

    # A typo in a bound that still brackets the estimate shows on the log scale.
    lopsided = failed(
        _plant(proposals, "any_hematoma", "vitamin_c_vs_control", "ci_upper", value="1.590")
    )
    assert {name for _, name in lopsided} == {"ratio_log_symmetric"}
    assert lopsided[(HEMATOMA_RR, "ratio_log_symmetric")] == (
        "the ratio 1.124 is off the centre of its CI on the log scale by 0.1039 (half-width "
        "0.2430): check the estimate and both bounds"
    )

    # Decimals stay exact strings with the field's unit, and empty is not zero.
    for planted, message in (
        ({"value": "56,3"}, "'56,3' is not an exact decimal"),
        ({"value": ""}, "an empty or padded string is not a value; use null for not reported"),
        ({"value": None}, "an extracted field has no value"),
        ({"unit": "participants"}, "unit 'participants' is not the field's unit '%'"),
        ({"value": "156.3"}, "'156.3' is not a percentage"),
    ):
        checks = failed(_plant(proposals, "any_hematoma", "vitamin_c", "percent", **planted))
        assert checks[(("any_hematoma", "vitamin_c", "percent"), "exact_value")] == message
    count = failed(_plant(proposals, "any_hematoma", "vitamin_c", "events", value="200.0"))
    assert (
        count[(("any_hematoma", "vitamin_c", "events"), "exact_value")] == "'200.0' is not a count"
    )
