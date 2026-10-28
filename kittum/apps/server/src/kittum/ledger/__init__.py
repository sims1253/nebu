"""The append-only, hash-chained audit ledger (DESIGN.md, "The ledger").

Every module that changes state writes an `EventDraft` in the same transaction as its own rows.
The ledger assigns the sequence number and the chain hash. `chain.event_hash` defines the hash, so
the Postgres store, the SQLite handover bundle, and the offline `verify` tool agree.

- `PostgresLedger` is the live store. Appends serialize per project on an advisory lock held
  until the caller commits.
- `SqliteLedger` is the bundle's store and a test store; `InMemoryLedger` in
  `kittum.ledger.testing` is the fake. All three share one implementation of the chain rules.
- `checkpoint` signs the chain head with an Ed25519 `SigningKey`. A hash chain alone cannot show
  that its end was cut or that it was rewritten from some point on; `verify` against a checkpoint
  can.
- `seal` writes a handover bundle; `verify_bundle` (and `kittum verify`) checks one offline.
  Modules add their tables to bundles with `register_exporter`.
"""

from kittum.ledger.bundle import (
    EXPORTERS,
    Bundle,
    BundleReport,
    BundleWriter,
    Exporter,
    ExporterRegistry,
    register_exporter,
    seal,
    verify_bundle,
)
from kittum.ledger.chain import GENESIS_HASH, event_hash, payload_hash
from kittum.ledger.keys import SigningKey, VerifyKey
from kittum.ledger.model import (
    AuditEvent,
    Checkpoint,
    EventDraft,
    Ledger,
    LedgerIntegrityError,
    Problem,
    VerifyReport,
)
from kittum.ledger.postgres import PostgresLedger
from kittum.ledger.sqlite import SqliteLedger
from kittum.ledger.verify import verify_chain

__all__ = [
    "EXPORTERS",
    "GENESIS_HASH",
    "AuditEvent",
    "Bundle",
    "BundleReport",
    "BundleWriter",
    "Checkpoint",
    "EventDraft",
    "Exporter",
    "ExporterRegistry",
    "Ledger",
    "LedgerIntegrityError",
    "PostgresLedger",
    "Problem",
    "SigningKey",
    "SqliteLedger",
    "VerifyKey",
    "VerifyReport",
    "event_hash",
    "payload_hash",
    "register_exporter",
    "seal",
    "verify_bundle",
    "verify_chain",
]
