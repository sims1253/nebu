"""The review protocol: scopes, criteria, and screening modes (DESIGN.md, "Protocol").

The protocol is versioned JSON with a content hash, and it is the source of truth for every later
decision. Exclusion reasons are criterion IDs.

    version = compile(yaml_text)               # validated, cross-checked, hashed
    stored = save_draft(tx, project_id, version)
    publish(tx, project_id, stored.id, approver_id, ledger=ledger)   # frozen, ledger event

Criteria render as the question a reviewer answers (`render_question`) and as the prompt
fragment the AI judge answers with a verbatim quote (`render_prompt`). `diff` compares versions.
`kittum.protocol.testing.InMemoryProtocolStore` stands in for the Postgres store.

The core schema is domain-neutral. A protocol may name a `profile` that adds vocabularies,
labels, typed `extensions`, and validation rules (`register_profile`); this package never imports
one.

Owner: WP-03.
"""

from kittum.protocol.compiler import (
    ProtocolError,
    ProtocolIssue,
    ProtocolVersion,
    compile,
    path_of,
    protocol_hash,
)
from kittum.protocol.diffing import FieldChange, ModeChange, ProtocolDiff, diff
from kittum.protocol.model import (
    AuditPlan,
    Comparator,
    Criterion,
    Deviation,
    EffectModifier,
    FreshnessPolicy,
    KnownInclude,
    LocalizedText,
    Outcome,
    PicoSlot,
    Polarity,
    PressReview,
    ReferenceSet,
    ReviewProtocol,
    ReviewQuestion,
    Scope,
    ScreeningMode,
    SearchTopic,
    Source,
    SourcePlan,
    Stage,
    StoppingPlan,
)
from kittum.protocol.profile import (
    ExtensionPoint,
    Profile,
    Rule,
    get_profile,
    register_profile,
    registered_profiles,
)
from kittum.protocol.render import AnswerKey, CriterionQuestion, render_prompt, render_question
from kittum.protocol.store import (
    PUBLISHED_EVENT,
    PostgresProtocolStore,
    ProtocolStateError,
    ProtocolStore,
    StoredProtocol,
    get_version,
    latest_published,
    list_versions,
    publish,
    save_draft,
)

__all__ = [
    "PUBLISHED_EVENT",
    "AnswerKey",
    "AuditPlan",
    "Comparator",
    "Criterion",
    "CriterionQuestion",
    "Deviation",
    "EffectModifier",
    "ExtensionPoint",
    "FieldChange",
    "FreshnessPolicy",
    "KnownInclude",
    "LocalizedText",
    "ModeChange",
    "Outcome",
    "PicoSlot",
    "Polarity",
    "PostgresProtocolStore",
    "PressReview",
    "Profile",
    "ProtocolDiff",
    "ProtocolError",
    "ProtocolIssue",
    "ProtocolStateError",
    "ProtocolStore",
    "ProtocolVersion",
    "ReferenceSet",
    "ReviewProtocol",
    "ReviewQuestion",
    "Rule",
    "Scope",
    "ScreeningMode",
    "SearchTopic",
    "Source",
    "SourcePlan",
    "Stage",
    "StoppingPlan",
    "StoredProtocol",
    "compile",
    "diff",
    "get_profile",
    "get_version",
    "latest_published",
    "list_versions",
    "path_of",
    "protocol_hash",
    "publish",
    "register_profile",
    "registered_profiles",
    "render_prompt",
    "render_question",
    "save_draft",
]
