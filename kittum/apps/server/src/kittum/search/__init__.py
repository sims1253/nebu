"""Primary searches, one strategy per database (DESIGN.md, "Search"; docs/sources.md).

A `SearchExecution` records everything PRISMA-S asks for: the strategy as run, the platform's
query translation, limits, dates, counts, and the raw responses by hash.

Owner: WP-05.
"""

from kittum.search.ctgov import CtgovSource
from kittum.search.imports import import_file
from kittum.search.models import SearchExecution, SearchPlan, Source, SourceRecord
from kittum.search.pubmed import PubMedSource
from kittum.search.service import run
from kittum.search.store import get_execution, list_executions

__all__ = [
    "CtgovSource",
    "PubMedSource",
    "SearchExecution",
    "SearchPlan",
    "Source",
    "SourceRecord",
    "get_execution",
    "import_file",
    "list_executions",
    "run",
]
