"""Dossier outputs (DESIGN.md, "Reports").

Modul 4 tables and Anhänge 4-A to 4-F, the Anlage II.1 directory tree with RIS files, PRISMA flows
per scope, the generated §4.2.3.5 justification, and the AI disclosure.

Owner: WP-14. Shipped now: the types. Still to build: every target.
"""

from typing import Literal

from kittum.core import Id, Record, Sha256

type Target = Literal["gba_modul4", "gba_anlage_ii_1", "jca", "prisma", "ai_disclosure"]


class ArtifactFile(Record):
    path: str
    sha256: Sha256
    media_type: str


class Artifact(Record):
    target: Target
    project_id: Id
    cutoff_seq: int
    files: tuple[ArtifactFile, ...]


def render(target: Target, project_id: Id, scope_id: str | None = None) -> Artifact:
    raise NotImplementedError("WP-14")


__all__ = ["Artifact", "ArtifactFile", "Target", "render"]
