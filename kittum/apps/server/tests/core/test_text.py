from kittum.core import Span, find_quote

ABSTRACT = (
    "Methods: We enrolled 412 patients aged 12 to 17 years with\n"
    "relapsed non-small-cell lung cancer. Treat-\nment lasted 24 weeks."
)


def test_exact_quote_is_found_at_original_offsets() -> None:
    (span,) = find_quote("patients aged 12 to 17 years", ABSTRACT)
    assert ABSTRACT[span.start : span.end] == "patients aged 12 to 17 years"


def test_whitespace_differences_are_tolerated() -> None:
    (span,) = find_quote("17 years with relapsed", ABSTRACT)
    assert ABSTRACT[span.start : span.end] == "17 years with\nrelapsed"


def test_line_break_hyphenation_is_joined() -> None:
    (span,) = find_quote("Treatment lasted 24 weeks", ABSTRACT)
    assert span == Span(ABSTRACT.index("Treat-"), len(ABSTRACT) - 1)


def test_real_hyphens_are_kept() -> None:
    assert find_quote("non-small-cell", ABSTRACT)
    assert not find_quote("nonsmallcell", ABSTRACT)


def test_paraphrase_is_not_found() -> None:
    assert find_quote("adolescent patients", ABSTRACT) == ()


def test_repeated_quote_returns_every_occurrence() -> None:
    assert len(find_quote("aged", "aged 12, aged 70")) == 2


def test_empty_quote_is_never_found() -> None:
    assert find_quote("  ", ABSTRACT) == ()


def test_hyphenated_and_plain_occurrences_are_both_reported() -> None:
    assert len(find_quote("Treatment", "Treatment and Treat-\nment")) == 2
