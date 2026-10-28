"""Review outputs (DESIGN.md, "Reports").

Core targets: the PRISMA flow per scope with strategies, exclusion lists, and the methods text
(`prisma`), RIS files per search and reference list (`ris`), the handover bundle (`bundle`), and
the AI disclosure (`ai_disclosure`). Profiles add their own targets with `register_target` under
IDs of the form `<profile id>.<name>`; core never imports a profile.

Owner: WP-14. Shipped now: the types and the target registry. Still to build: every core target.
"""

from typing import TYPE_CHECKING, Literal, get_args

from kittum.core import Id, Record, Sha256

if TYPE_CHECKING:
    from collections.abc import Callable

type CoreTarget = Literal["prisma", "ris", "bundle", "ai_disclosure"]
type Target = str  # a CoreTarget, or "<profile id>.<name>" from a profile


class ArtifactFile(Record):
    path: str
    sha256: Sha256
    media_type: str


class Artifact(Record):
    target: Target
    project_id: Id
    cutoff_seq: int
    files: tuple[ArtifactFile, ...]


type Renderer = Callable[[Id, str | None], Artifact]  # (project_id, scope_id)

CORE_TARGETS: tuple[CoreTarget, ...] = get_args(CoreTarget.__value__)
_profile_targets: dict[Target, Renderer] = {}


def register_target(target: Target, renderer: Renderer) -> None:
    """Add a profile's target. Called from the entry points, once per process."""
    profile, dot, name = target.partition(".")
    if not (profile and dot and name):
        raise ValueError(f"profile targets are named '<profile id>.<name>', got {target!r}")
    if target in _profile_targets:
        raise ValueError(f"target {target!r} is already registered")
    _profile_targets[target] = renderer


def targets() -> tuple[Target, ...]:
    return (*CORE_TARGETS, *sorted(_profile_targets))


def render(target: Target, project_id: Id, scope_id: str | None = None) -> Artifact:
    if target in _profile_targets:
        return _profile_targets[target](project_id, scope_id)
    if target in CORE_TARGETS:
        raise NotImplementedError("WP-14")
    raise ValueError(f"unknown report target {target!r}")


__all__ = [
    "CORE_TARGETS",
    "Artifact",
    "ArtifactFile",
    "CoreTarget",
    "Renderer",
    "Target",
    "register_target",
    "render",
    "targets",
]
