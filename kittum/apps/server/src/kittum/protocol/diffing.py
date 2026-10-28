"""What changed between two protocol versions.

Lists of objects with an `id` are compared by ID, not by position, so reordering criteria is one
change per moved criterion rather than a cascade, and paths name the object
(`search_topics[RCT].criteria[RCT-E5].text.en`).
"""

from typing import TYPE_CHECKING, Any

from kittum.core import Record
from kittum.protocol.model import ScreeningMode

if TYPE_CHECKING:
    from kittum.protocol.compiler import ProtocolVersion


class FieldChange(Record):
    path: str
    before: Any = None  # None when added
    after: Any = None  # None when removed


class ModeChange(Record):
    topic_id: str
    before: ScreeningMode
    after: ScreeningMode


class ProtocolDiff(Record):
    added_criteria: tuple[str, ...]
    removed_criteria: tuple[str, ...]
    changed_criteria: tuple[str, ...]
    mode_changes: tuple[ModeChange, ...]
    added_scopes: tuple[str, ...] = ()
    removed_scopes: tuple[str, ...] = ()
    changes: tuple[FieldChange, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.changes


def diff(a: ProtocolVersion, b: ProtocolVersion) -> ProtocolDiff:
    """Changes from `a` to `b`."""
    changes: list[FieldChange] = []
    if a.content_hash != b.content_hash:
        _walk(a.protocol.model_dump(mode="json"), b.protocol.model_dump(mode="json"), "", changes)

    before_criteria = {c.id: c for c in a.protocol.criteria}
    after_criteria = {c.id: c for c in b.protocol.criteria}
    before_scopes = [s.id for s in a.protocol.scopes()]
    after_scopes = [s.id for s in b.protocol.scopes()]
    before_modes = {t.id: t.mode for t in a.protocol.topics}
    return ProtocolDiff(
        added_criteria=tuple(i for i in after_criteria if i not in before_criteria),
        removed_criteria=tuple(i for i in before_criteria if i not in after_criteria),
        changed_criteria=tuple(
            i for i, c in after_criteria.items() if i in before_criteria and before_criteria[i] != c
        ),
        mode_changes=tuple(
            ModeChange(topic_id=t.id, before=before_modes[t.id], after=t.mode)
            for t in b.protocol.topics
            if t.id in before_modes and before_modes[t.id] != t.mode
        ),
        added_scopes=tuple(i for i in after_scopes if i not in before_scopes),
        removed_scopes=tuple(i for i in before_scopes if i not in after_scopes),
        changes=tuple(changes),
    )


def _key(item: Any) -> str | None:
    if isinstance(item, dict) and isinstance(item.get("id"), str):
        return item["id"]
    return None


def _walk(before: Any, after: Any, path: str, out: list[FieldChange]) -> None:
    if before == after:
        return
    if isinstance(before, dict) and isinstance(after, dict):
        for key in [*before, *(k for k in after if k not in before)]:
            child = f"{path}.{key}" if path else key
            if key not in after:
                out.append(FieldChange(path=child, before=before[key]))
            elif key not in before:
                out.append(FieldChange(path=child, after=after[key]))
            else:
                _walk(before[key], after[key], child, out)
        return
    if isinstance(before, list) and isinstance(after, list):
        keys_before = [_key(x) for x in before]
        keys_after = [_key(x) for x in after]
        keyed = all(keys_before) and all(keys_after)
        if keyed and len(set(keys_before)) == len(before) and len(set(keys_after)) == len(after):
            old = dict(zip(keys_before, before, strict=True))
            new = dict(zip(keys_after, after, strict=True))
            for k in [*keys_before, *(k for k in keys_after if k not in old)]:
                child = f"{path}[{k}]"
                if k not in new:
                    out.append(FieldChange(path=child, before=old[k]))
                elif k not in old:
                    out.append(FieldChange(path=child, after=new[k]))
                else:
                    _walk(old[k], new[k], child, out)
            if [k for k in keys_before if k in new] != [k for k in keys_after if k in old]:
                out.append(FieldChange(path=f"{path}.order", before=keys_before, after=keys_after))
            return
    out.append(FieldChange(path=path, before=before, after=after))
