"""Primary searches, one strategy per database (DESIGN.md, "Search"; docs/sources.md).

A `SearchExecution` records everything PRISMA-S asks for: the strategy as run, the platform's
query translation, limits, dates, counts, and the raw responses by hash.

Owner: WP-05.
"""

from kittum.search.count import (
    SOURCE_NAMES,
    TRANSPORT_ERRORS,
    QueryCount,
    QueryDialect,
    count_hits,
    failure_message,
    unreachable,
)
from kittum.search.ctgov import CtgovSource
from kittum.search.imports import import_file
from kittum.search.models import SearchExecution, SearchPlan, Source, SourceError, SourceRecord
from kittum.search.pubmed import PubMedSource
from kittum.search.service import run
from kittum.search.store import get_execution, list_executions

__all__ = [
    "SOURCE_NAMES",
    "TRANSPORT_ERRORS",
    "CtgovSource",
    "PubMedSource",
    "QueryCount",
    "QueryDialect",
    "SearchExecution",
    "SearchPlan",
    "Source",
    "SourceError",
    "SourceRecord",
    "count_hits",
    "failure_message",
    "get_execution",
    "import_file",
    "list_executions",
    "run",
    "unreachable",
]
