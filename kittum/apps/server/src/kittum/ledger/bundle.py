"""The handover bundle (DESIGN.md, "The handover bundle").

`seal` writes a directory that a client can check without Kittum's database:

    kittum.sqlite     the ledger through `cutoff_seq`, its checkpoints, the artifact index,
                      and every table that a registered exporter adds
    artifacts/<hash>  each artifact the exporters include
    <other paths>     files the exporters add, such as rendered reports under `reports/`
    manifest.json     every file hash, the signed checkpoint, the public key, and a signature
                      over the manifest itself

Modules add their own rows and files by registering an `Exporter`; the ledger code does not
change.
`verify_bundle` is the offline check behind `kittum verify`.
"""

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from kittum.core import Id, Record, Sha256, SystemClock, canonical_json, sha256_hex
from kittum.ledger.keys import VerifyKey, document_message
from kittum.ledger.model import AuditEvent, Checkpoint, EventDraft, LedgerIntegrityError
from kittum.ledger.postgres import PostgresLedger
from kittum.ledger.sqlite import NO_TX, SqliteLedger
from kittum.ledger.verify import verify_chain

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

    from kittum.core import Clock
    from kittum.db import Tx
    from kittum.ledger.keys import SigningKey
    from kittum.ledger.model import Ledger
    from kittum.storage import ArtifactStore

FORMAT = "kittum.bundle.v1"
MANIFEST_DOMAIN = "kittum.manifest.v1"
SQLITE_NAME = "kittum.sqlite"
MANIFEST_NAME = "manifest.json"
ARTIFACT_DIR = "artifacts"
SEALED_EVENT = "ledger.bundle_sealed"

_RESERVED_TABLES = {"audit_event", "checkpoint", "artifact"}
_RESERVED_PATHS = {SQLITE_NAME, MANIFEST_NAME, ARTIFACT_DIR}
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_HASH = re.compile(r"^[0-9a-f]{64}$")

ARTIFACT_SCHEMA = """
create table artifact (
    hash text primary key,
    media_type text,
    bytes integer,
    included integer not null check (included in (0, 1)),
    withheld_reason text,
    exporter text not null,
    check ((included = 1) = (withheld_reason is null))
);
"""


class BundleWriter:
    """What an exporter may write: its own tables and the artifacts its rows reference."""

    def __init__(self, conn: sqlite3.Connection, store: ArtifactStore) -> None:
        self._conn = conn
        self._store = store
        self._tables: set[str] = set()
        self._exporter = ""
        self.artifacts: dict[str, tuple[str | None, str | None, str]] = {}
        self.files: dict[str, bytes] = {}

    def _begin(self, exporter: str) -> None:
        """Attribute the artifacts indexed from now on to `exporter`."""
        self._exporter = exporter

    def table(self, name: str, columns: Sequence[str]) -> None:
        """Create a table. Columns are untyped; values keep the type they were inserted with."""
        for n in (name, *columns):
            if not _NAME.match(n):
                raise ValueError(f"{n!r} is not a valid table or column name")
        if name in _RESERVED_TABLES or name in self._tables:
            raise ValueError(f"table {name!r} already exists in the bundle")
        self._tables.add(name)
        self._conn.execute(f"create table {name} ({', '.join(columns)})")

    def insert(self, name: str, rows: Iterable[Mapping[str, Any]]) -> None:
        """Insert rows as returned by a `Tx`: UUIDs, times, decimals, and JSON become text."""
        if name not in self._tables:
            raise ValueError(f"create table {name!r} first")
        for row in rows:
            columns = list(row)
            for c in columns:
                if not _NAME.match(c):
                    raise ValueError(f"{c!r} is not a valid column name")
            self._conn.execute(
                f"insert into {name} ({', '.join(columns)}) values ({', '.join('?' * len(columns))})",
                [_sqlite_value(row[c]) for c in columns],
            )

    def artifact(
        self, sha256: str, *, media_type: str | None = None, withheld_reason: str | None = None
    ) -> None:
        """Index an artifact. With a reason it is listed by hash only, without the file.

        If any exporter withholds an artifact, it stays withheld: a rights restriction wins.
        """
        if not _HASH.match(sha256):
            raise ValueError(f"{sha256!r} is not a SHA-256 hex digest")
        if withheld_reason is None and not self._store.exists(sha256):
            raise ValueError(f"artifact {sha256} is not in the store")
        known = self.artifacts.get(sha256)
        if known is not None and known[1] is not None:
            return
        self.artifacts[sha256] = (media_type, withheld_reason, self._exporter)

    def read(self, sha256: str) -> bytes | None:
        """An artifact's bytes from the store, or None if the store lacks it: for an exporter
        that indexes what an artifact names, such as the model calls in a transcript."""
        return self._store.get(sha256) if self._store.exists(sha256) else None

    def file(self, path: str, data: bytes) -> None:
        """Add a file at a relative POSIX path, such as `reports/prisma.json`.

        The manifest lists it with its hash like every other file, so `verify` checks it.
        """
        parts = path.split("/")
        if (
            not path
            or path.startswith("/")
            or any(p in ("", ".", "..") or "\\" in p or "\x00" in p for p in parts)
        ):
            raise ValueError(f"{path!r} is not a relative path inside the bundle")
        if parts[0] in _RESERVED_PATHS:
            raise ValueError(f"{path!r} is reserved for the ledger")
        if path in self.files:
            raise ValueError(f"{path!r} is already in the bundle")
        self.files[path] = data


def _sqlite_value(value: Any) -> Any:
    match value:
        case None | int() | float() | str() | bytes():
            return value
        case UUID() | Decimal():
            return str(value)
        case datetime():
            return (value.astimezone(UTC) if value.tzinfo else value).isoformat()
        case date():
            return value.isoformat()
        case Enum():
            return _sqlite_value(value.value)
        case memoryview():
            return bytes(value)
        case Record():
            return canonical_json(value).decode()
        case dict() | list() | tuple():
            return canonical_json(value).decode()
    raise TypeError(f"cannot store {type(value).__name__} in a bundle")


class Exporter(Protocol):
    """Adds a module's rows to a bundle. Export the state as of `cutoff_seq`, not newer rows."""

    name: str

    def export(self, tx: Tx, project_id: Id, cutoff_seq: int, bundle: BundleWriter) -> None: ...


@dataclass
class ExporterRegistry:
    _exporters: dict[str, Exporter] = field(default_factory=dict)

    def register(self, exporter: Exporter) -> Exporter:
        if not _NAME.match(exporter.name):
            raise ValueError(f"{exporter.name!r} is not a valid exporter name")
        if exporter.name in self._exporters:
            raise ValueError(f"an exporter named {exporter.name!r} is already registered")
        self._exporters[exporter.name] = exporter
        return exporter

    def unregister(self, name: str) -> None:
        self._exporters.pop(name, None)

    def __iter__(self) -> Iterator[Exporter]:
        return iter(list(self._exporters.values()))


EXPORTERS = ExporterRegistry()
"""The process-wide registry that `seal` uses by default."""


def register_exporter(exporter: Exporter) -> Exporter:
    """Add an exporter to every bundle this process seals. Call it at module import."""
    return EXPORTERS.register(exporter)


type BundleCheck = Callable[[Path, list[AuditEvent], Mapping[str, str], list[str]], None]
"""An offline check of files a module adds: (bundle root, the verified ledger events, the
manifest's files, problems to append to). It runs only once the chain verifies."""

_CHECKS: list[BundleCheck] = []


def register_bundle_check(check: BundleCheck) -> BundleCheck:
    """Add a check that `verify_bundle` runs on every bundle, as `register_exporter` adds rows."""
    if check not in _CHECKS:
        _CHECKS.append(check)
    return check


class Bundle(Record):
    path: Path
    project_id: Id
    cutoff_seq: int
    manifest_hash: Sha256
    checkpoint: Checkpoint


def _manifest_hash(manifest: dict[str, Any]) -> str:
    return sha256_hex(canonical_json(manifest))


def seal(
    tx: Tx,
    project_id: Id,
    cutoff_seq: int,
    store: ArtifactStore,
    out_dir: Path,
    *,
    signer: SigningKey,
    actor_id: Id,
    ledger: Ledger | None = None,
    exporters: Iterable[Exporter] | None = None,
    clock: Clock | None = None,
) -> Bundle:
    """Write the bundle for the chain through `cutoff_seq` into `out_dir`, which must be empty.

    Verifies the chain first, signs a checkpoint at `cutoff_seq` unless `signer` already has one
    there, and records the seal as a `ledger.bundle_sealed` event in `tx`. If `tx` rolls back,
    the files stay behind but the ledger never mentions them.
    """
    ledger = ledger or PostgresLedger(clock)
    clock = clock or SystemClock()
    exporters = list(EXPORTERS if exporters is None else exporters)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"{out_dir} is not empty")

    events = ledger.events(tx, project_id, through_seq=cutoff_seq)
    report = verify_chain(project_id, events)
    if not report.ok:
        raise LedgerIntegrityError(report)
    if report.checked_through_seq != cutoff_seq:
        raise ValueError(f"the chain ends at seq {report.checked_through_seq}, before {cutoff_seq}")
    checkpoints = [c for c in ledger.checkpoints(tx, project_id) if c.seq <= cutoff_seq]
    checkpoint = next(
        (c for c in checkpoints if c.seq == cutoff_seq and c.key_id == signer.key_id), None
    )
    if checkpoint is None:
        checkpoint = ledger.checkpoint(tx, project_id, signer, seq=cutoff_seq)
        checkpoints.append(checkpoint)

    out_dir.mkdir(parents=True, exist_ok=True)
    db = SqliteLedger.open(out_dir / SQLITE_NAME)
    try:
        db.restore(events, checkpoints)
        conn = db.connection
        conn.execute("begin")
        conn.execute(ARTIFACT_SCHEMA)
        writer = BundleWriter(conn, store)
        for exporter in exporters:
            writer._begin(exporter.name)
            exporter.export(tx, project_id, cutoff_seq, writer)
        files: dict[str, str] = {}
        for sha256, (media_type, reason, source) in sorted(writer.artifacts.items()):
            size: int | None = None
            if reason is None:
                data = store.get(sha256)
                if sha256_hex(data) != sha256:
                    raise ValueError(f"artifact {sha256} is corrupt in the store")
                size = len(data)
                rel = f"{ARTIFACT_DIR}/{sha256}"
                (out_dir / ARTIFACT_DIR).mkdir(exist_ok=True)
                (out_dir / rel).write_bytes(data)
                files[rel] = sha256
            conn.execute(
                "insert into artifact (hash, media_type, bytes, included, withheld_reason,"
                " exporter) values (?, ?, ?, ?, ?, ?)",
                (sha256, media_type, size, int(reason is None), reason, source),
            )
        conn.execute("commit")
    finally:
        db.close()
    for rel, data in sorted(writer.files.items()):
        target = out_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files[rel] = sha256_hex(data)
    files[SQLITE_NAME] = sha256_hex((out_dir / SQLITE_NAME).read_bytes())

    manifest: dict[str, Any] = {
        "format": FORMAT,
        "project_id": str(project_id),
        "cutoff_seq": cutoff_seq,
        "head_hash": checkpoint.head_hash,
        "sealed_at": clock.now().astimezone(UTC).isoformat(),
        "exporters": [e.name for e in exporters],
        "files": dict(sorted(files.items())),
        "checkpoint": checkpoint.model_dump(mode="json"),
        "public_key": signer.verify_key.raw.hex(),
    }
    manifest["signature"] = signer.sign(document_message(manifest, domain=MANIFEST_DOMAIN))
    (out_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n")
    manifest_hash = _manifest_hash(manifest)

    ledger.append(
        tx,
        EventDraft(
            project_id=project_id,
            type=SEALED_EVENT,
            schema_version=1,
            actor_id=actor_id,
            payload={
                "cutoff_seq": cutoff_seq,
                "checkpoint_id": str(checkpoint.id),
                "manifest_hash": manifest_hash,
            },
        ),
    )
    return Bundle(
        path=out_dir,
        project_id=project_id,
        cutoff_seq=cutoff_seq,
        manifest_hash=manifest_hash,
        checkpoint=checkpoint,
    )


class BundleReport(Record):
    path: Path
    ok: bool
    key_id: str | None = None
    key_pinned: bool = False
    """False when the bundle was checked only against the key it carries itself."""
    cutoff_seq: int | None = None
    problems: tuple[str, ...] = ()


def verify_bundle(path: Path, *, trusted_key: VerifyKey | None = None) -> BundleReport:
    """Check a sealed bundle offline: signatures, chain continuity, and artifact hashes.

    Without `trusted_key` the check uses the key in the manifest. That still proves the bundle is
    internally consistent, but anyone could have re-signed it, so the report says the key was
    not pinned. Compare `key_id` with the fingerprint the sender published.
    """
    problems: list[str] = []

    def report(**extra: Any) -> BundleReport:
        return BundleReport(path=path, ok=not problems, problems=tuple(problems), **extra)

    try:
        manifest = json.loads((path / MANIFEST_NAME).read_text())
    except (OSError, ValueError) as e:
        problems.append(f"cannot read {MANIFEST_NAME}: {e}")
        return report()
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        problems.append(f"{MANIFEST_NAME} is not a {FORMAT} manifest")
        return report()

    try:
        embedded = VerifyKey.from_raw_hex(manifest["public_key"])
        checkpoint = Checkpoint.model_validate(manifest["checkpoint"])
        project_id = UUID(manifest["project_id"])
        cutoff_seq = int(manifest["cutoff_seq"])
        files: dict[str, str] = dict(manifest["files"])
        signature = str(manifest["signature"])
    except (KeyError, TypeError, ValueError) as e:
        problems.append(f"{MANIFEST_NAME} is malformed: {e}")
        return report()
    key = trusted_key or embedded
    pinned = trusted_key is not None
    result = {"key_id": key.key_id, "key_pinned": pinned, "cutoff_seq": cutoff_seq}

    if pinned and embedded.key_id != key.key_id:
        problems.append(f"the bundle was signed by key {embedded.key_id}, not the trusted key")
    unsigned = {k: v for k, v in manifest.items() if k != "signature"}
    if not key.verifies(signature, document_message(unsigned, domain=MANIFEST_DOMAIN)):
        problems.append("the manifest signature does not verify")
    if (
        checkpoint.project_id != project_id
        or checkpoint.seq != cutoff_seq
        or checkpoint.head_hash != manifest.get("head_hash")
    ):
        problems.append("the checkpoint does not match the manifest's project, seq, or head")

    # Files: every listed file matches its hash, and nothing unlisted rides along.
    for rel, expected in sorted(files.items()):
        target = path / rel
        if not target.resolve().is_relative_to(path.resolve()):
            problems.append(f"{rel} points outside the bundle")
        elif not target.is_file():
            problems.append(f"{rel} is missing")
        elif sha256_hex(target.read_bytes()) != expected:
            problems.append(f"{rel} does not match its hash")
    listed = {*files, MANIFEST_NAME}
    for rel, is_link in _entries(path):
        if is_link:
            problems.append(f"{rel} is a symlink; a bundle holds only regular files")
        elif rel not in listed:
            problems.append(f"{rel} is not listed in the manifest")
    if SQLITE_NAME not in files:
        problems.append(f"{SQLITE_NAME} is not listed in the manifest")
        return report(**result)

    try:
        db = SqliteLedger.open_readonly(path / SQLITE_NAME)
    except (OSError, sqlite3.Error) as e:
        problems.append(f"cannot open {SQLITE_NAME}: {e}")
        return report(**result)
    try:
        _verify_database(path, db, project_id, cutoff_seq, checkpoint, key, files, problems)
    except (sqlite3.Error, ValueError) as e:
        problems.append(f"{SQLITE_NAME} is malformed: {e}")
    finally:
        db.close()
    return report(**result)


def _entries(root: Path) -> list[tuple[str, bool]]:
    """Every file and symlink under `root`, as (relative path, is a symlink).

    Symlinked directories are reported, not followed, and so are dangling links.
    """
    out: list[tuple[str, bool]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        for name in dirnames + filenames:
            entry = here / name
            if entry.is_symlink() or name in filenames:
                out.append((entry.relative_to(root).as_posix(), entry.is_symlink()))
    return sorted(out)


def _verify_database(
    root: Path,
    db: SqliteLedger,
    project_id: Id,
    cutoff_seq: int,
    checkpoint: Checkpoint,
    key: VerifyKey,
    files: Mapping[str, str],
    problems: list[str],
) -> None:
    others = [p for p in db.project_ids() if p != project_id]
    if others:
        problems.append(f"the ledger holds events of other projects: {others}")
    events = db.events(NO_TX, project_id)
    chain = verify_chain(project_id, events, checkpoint=checkpoint, key=key)
    if not chain.ok:
        problems.append(f"ledger seq {chain.first_bad_seq}: {chain.reason}")
    elif chain.checked_through_seq != cutoff_seq:
        problems.append(
            f"the ledger runs to seq {chain.checked_through_seq}, past the cutoff {cutoff_seq}"
        )
    else:
        for check in _CHECKS:
            check(root, list(events), files, problems)
    stored = db.checkpoints(NO_TX, project_id)
    if checkpoint not in stored:
        problems.append("the manifest's checkpoint is not in the ledger file")
    hashes = {e.seq: e.event_hash for e in events}
    for c in stored:
        if c.key_id == key.key_id and not key.verifies_checkpoint(c):
            problems.append(f"the checkpoint at seq {c.seq} has a bad signature")
        if hashes.get(c.seq) != c.head_hash:
            problems.append(f"the checkpoint at seq {c.seq} does not match the ledger")

    rows = db.connection.execute("select hash, included from artifact order by hash").fetchall()
    for row in rows:
        rel = f"{ARTIFACT_DIR}/{row['hash']}"
        if row["included"] and files.get(rel) != row["hash"]:
            problems.append(f"artifact {row['hash']} is indexed but not in the bundle")
    indexed = {f"{ARTIFACT_DIR}/{r['hash']}" for r in rows if r["included"]}
    for rel in sorted(f for f in files if f.startswith(f"{ARTIFACT_DIR}/")):
        if rel not in indexed:
            problems.append(f"{rel} is in the bundle but not in the artifact index")
