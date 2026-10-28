"""What the assistant's recordings were made from, shared by the tests and `record_fixtures`."""

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
COUNTS = FIXTURES / "counts"
STACK = Path(__file__).parents[1] / "enrich" / "fixtures" / "stack"
CONTACT_EMAIL = "test@kittum.dev"  # the e2e stack's address; recordings carry it in their URLs

DESCRIPTION = (
    "Does nicotine replacement therapy (any form: patch, gum, lozenge, inhaler, nasal or mouth "
    "spray, sublingual tablet) help adults who smoke tobacco to stop smoking, compared with "
    "placebo or no NRT? A second question compares combination NRT (patch plus a fast-acting "
    "form) with a single form. Primary outcome: abstinence at six months or longer, "
    "biochemically validated where possible; secondary: serious adverse events and withdrawal "
    "due to adverse events. Randomized or quasi-randomized controlled trials only. Any language, "
    "no date limits."
)
# The nicotine replacement example, whose PubMed strategy gets improved.
EXAMPLE = Path(__file__).parents[4] / "examples" / "protocols" / "nicotine-replacement.yaml"
# Where `KITTUM_ASSIST_BACKEND=demo` finds the recorded answers.
ANSWERS = (
    Path(__file__).parents[2] / "src" / "kittum" / "app" / "demo_data" / "assist" / "answers.json"
)
PUBMED = "search_topics[TRIALS].source_plan.sources[medline].strategy"


def drafted_pubmed(document: dict) -> str:
    """The path of the PubMed strategy in a drafted protocol."""
    topic = document["search_topics"][0]
    source = next(s for s in topic["source_plan"]["sources"] if s.get("platform") == "PubMed")
    return f"search_topics[{topic['id']}].source_plan.sources[{source['id']}].strategy"


def accepted_subset(change_ids: list[str]) -> list[str]:
    """The changes the tests and the journey accept: all but the last."""
    return change_ids[:-1]
