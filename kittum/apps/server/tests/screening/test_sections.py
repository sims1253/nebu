"""How a reviewer's abstract is cut into sections, and where the protocol's terms fall in them."""

from kittum.protocol import compile
from kittum.screening.rules import highlights, sections
from tests.screening.support import protocol_source

# A registry summary's shape at its start, then a structured abstract's labels.
TEXT = (
    "  \n This review asks whether nicotine gum helps hospital inpatients quit.  "
    "BACKGROUND: Admission is a chance to stop smoking. "
    "METHODS: A cohort of 300 adult inpatients was followed for a year. "
    "RESULTS: Quit rates were 18% with gum and 9% without."
)


def test_text_before_the_first_label_is_an_unlabelled_section_with_its_terms() -> None:
    criteria = compile(protocol_source()).protocol.search_topics[0].criteria

    found = sections(TEXT)

    assert [(label, TEXT[start:end]) for label, start, end in found] == [
        ("", "This review asks whether nicotine gum helps hospital inpatients quit."),
        ("BACKGROUND", "Admission is a chance to stop smoking."),
        ("METHODS", "A cohort of 300 adult inpatients was followed for a year."),
        ("RESULTS", "Quit rates were 18% with gum and 9% without."),
    ]
    # Each protocol term is marked inside the section a reviewer reads it in.
    terms = [
        (TEXT[start:end], next(lab for lab, s, e in found if s <= start and end <= e), label)
        for start, end, label in highlights(TEXT, criteria)
    ]
    assert terms == [("review", "", "protocol:DESIGN"), ("cohort", "METHODS", "protocol:DESIGN")]
