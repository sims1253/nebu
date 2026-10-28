"""Paths into a protocol document, in the notation `kittum.protocol.diff` uses: lists of objects
with an `id` are addressed by ID (`search_topics[TRIALS].criteria[POP].text.en`).

`leaves` lists every field value of a document under its path, which is what an AI mark names.
A list of plain values (keywords, stages) is one leaf.
"""

import re
from typing import Any

_STEP = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)|\[([^\]]+)\]")


def _keyed(items: list[Any]) -> bool:
    ids = [i.get("id") if isinstance(i, dict) else None for i in items]
    return bool(items) and all(isinstance(i, str) for i in ids) and len(set(ids)) == len(ids)


def leaves(node: Any, path: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(node, dict) and node:
        for key, value in node.items():
            out |= leaves(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list) and _keyed(node):
        for item in node:
            out |= leaves(item, f"{path}[{item['id']}]")
    else:
        out[path] = node
    return out


def steps(path: str) -> list[str | tuple[str]]:
    """`a.b[X].c` as `["a", "b", ("X",), "c"]`: a tuple is an ID in a keyed list."""
    out: list[str | tuple[str]] = []
    pos = 0
    for m in _STEP.finditer(path):
        if m.start() != pos and path[pos : m.start()] != ".":
            raise KeyError(path)
        out.append(m.group(1) if m.group(1) else (m.group(2),))
        pos = m.end()
    if pos != len(path) or not out:
        raise KeyError(path)
    return out


def get(document: Any, path: str) -> Any:
    node = document
    for step in steps(path):
        if isinstance(step, tuple):
            node = next(i for i in node if isinstance(i, dict) and i.get("id") == step[0])
        else:
            node = node[step]
    return node


def replace(document: Any, path: str, value: Any) -> Any:
    """A copy of `document` with the value at `path` replaced; the rest is shared."""
    *head, last = steps(path)

    def go(node: Any, rest: list[str | tuple[str]]) -> Any:
        step = rest[0] if rest else last
        inner = (lambda child: go(child, rest[1:])) if rest else (lambda _child: value)
        if isinstance(step, tuple):
            items = list(node)
            index = next(i for i, x in enumerate(items) if x.get("id") == step[0])
            items[index] = inner(items[index])
            return items
        return {**node, step: inner(node[step])}

    return go(document, head)


# Fields that hold no writing: the format version and the default language, which every
# protocol shares, the profile, which only a lead names, and IDs, which name their object.
_UNWRITTEN = frozenset({"schema_version", "default_language", "profile"})


def written(document: Any) -> dict[str, Any]:
    """The leaves someone wrote: everything but format fields, IDs, and empty values."""
    return {
        p: v
        for p, v in leaves(document).items()
        if p not in _UNWRITTEN and p.rsplit(".", 1)[-1] != "id" and v not in (None, [], {})
    }


def changed_written(before: Any, after: Any) -> set[str]:
    old, new = leaves(before), written(after)
    return {p for p, v in new.items() if p not in old or old[p] != v}
