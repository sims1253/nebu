"""The JSON Schema a model answers in, derived from the protocol's own Pydantic models.

The schema is never written by hand: `strict_schema(ReviewProtocol)` turns the model's JSON Schema
into the form OpenAI-style `strict` decoding accepts, so the answer format cannot drift from the
protocol it must compile to.

- Every property is listed in `required`; one the model may leave out becomes nullable instead.
- Every object has `additionalProperties: false`; no `default`, `title`, or sibling of `$ref`.
- A mapping (`LocalizedText`, `{en: ..., de: ...}`) has no fixed keys, which strict decoding
  cannot express, so it becomes a list of `{key, value}` pairs.
- `extensions` are left out: a profile's typed fields are the lead's to write.

`from_strict` turns an answer back into the protocol's own shape: pairs into mappings, and a null
for a property the model may leave out into no property, so its default applies. A key given twice
in one list of pairs is a problem with its path, which goes back to the model; it is never
resolved by keeping one of them. Then the answer
goes through `kittum.protocol.compile` like any hand-written source.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic import BaseModel

_DROP = frozenset({"default", "title"})
OMITTED = frozenset({"extensions"})


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    raw = model.model_json_schema(mode="validation")
    defs = raw.pop("$defs", {})
    root = _strict(raw)
    if defs:
        root["$defs"] = {name: _strict(d) for name, d in defs.items()}
    return root


def wrap(properties: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """A strict object of `properties`, each a strict schema; nested `$defs` move to the root."""
    defs: dict[str, Any] = {}
    props = {}
    for name, schema in properties.items():
        schema = dict(schema)
        defs |= schema.pop("$defs", {})
        props[name] = schema
    out: dict[str, Any] = {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }
    if defs:
        out["$defs"] = defs
    return out


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    if any(branch == {"type": "null"} for branch in schema.get("anyOf", ())):
        return schema
    return {"anyOf": [schema, {"type": "null"}]}


def _strict(node: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in node:
        return {"$ref": node["$ref"]}
    out = {k: v for k, v in node.items() if k not in _DROP}
    if "anyOf" in out:
        out["anyOf"] = [_strict(branch) for branch in out["anyOf"]]
    if out.get("type") == "array" and isinstance(out.get("items"), dict):
        out["items"] = _strict(out["items"])
    if out.get("type") == "object":
        if "properties" not in out:
            return _pairs(out)
        required = set(out.get("required", ()))
        props = {
            name: _strict(sub) if name in required else _nullable(_strict(sub))
            for name, sub in out["properties"].items()
            if name not in OMITTED
        }
        out |= {"properties": props, "required": list(props), "additionalProperties": False}
    return out


def _pairs(mapping: dict[str, Any]) -> dict[str, Any]:
    key = {"type": "string", **mapping.get("propertyNames", {})}
    value = mapping.get("additionalProperties")
    pair = {
        "type": "object",
        "properties": {"key": key, "value": _strict(value) if isinstance(value, dict) else {}},
        "required": ["key", "value"],
        "additionalProperties": False,
    }
    out: dict[str, Any] = {"type": "array", "items": pair}
    if "minProperties" in mapping:
        out["minItems"] = mapping["minProperties"]
    return out


def from_strict(value: Any, model: type[BaseModel]) -> tuple[Any, list[tuple[str, str]]]:
    """An answer in `strict_schema(model)`'s shape, back in the model's own shape, and the problems
    the conversion found, by path into the answer: a key given twice in one mapping, which the
    model's own shape cannot hold."""
    raw = model.model_json_schema(mode="validation")
    problems: list[tuple[str, str]] = []
    return _back(value, raw, raw.get("$defs", {}), "", problems), problems


def _resolve(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        schema = defs[schema["$ref"].rsplit("/", 1)[-1]]
    return schema


def _at(path: str, step: str | int) -> str:
    return f"{path}[{step}]" if isinstance(step, int) else f"{path}.{step}" if path else step


def _back(
    value: Any, schema: dict[str, Any], defs: dict[str, Any], path: str, problems: list[Any]
) -> Any:
    schema = _resolve(schema, defs)
    if "anyOf" in schema:
        if value is None:
            return None
        branches = [_resolve(b, defs) for b in schema["anyOf"] if b != {"type": "null"}]
        for branch in branches:
            if _fits(value, branch):
                return _back(value, branch, defs, path, problems)
        return value  # validation names the mismatch
    if schema.get("type") == "object" and isinstance(value, dict):
        props = schema.get("properties", {})
        required = set(schema.get("required", ()))
        return {
            k: _back(v, props[k], defs, _at(path, k), problems) if k in props else v
            for k, v in value.items()
            if not (v is None and k in props and k not in required)
        }
    if schema.get("type") == "object" and "properties" not in schema and isinstance(value, list):
        if not all(isinstance(p, dict) and isinstance(p.get("key"), str) for p in value):
            return value
        inner = schema.get("additionalProperties")
        out: dict[str, Any] = {}
        for i, pair in enumerate(value):
            key = pair["key"]
            if key in out:
                first = next(n for n, p in enumerate(value) if p["key"] == key)
                problems.append(
                    (
                        _at(_at(path, i), "key"),
                        f"{key!r} is given twice; it is already at {_at(path, first)}",
                    )
                )
                continue
            here = _at(_at(path, i), "value")
            out[key] = (
                _back(pair.get("value"), inner, defs, here, problems)
                if isinstance(inner, dict)
                else pair.get("value")
            )
        return out
    if schema.get("type") == "array" and isinstance(value, list):
        items = schema.get("items")
        if not isinstance(items, dict):
            return value
        return [_back(v, items, defs, _at(path, i), problems) for i, v in enumerate(value)]
    return value


def _fits(value: Any, schema: dict[str, Any]) -> bool:
    kind = schema.get("type")
    if kind == "object":
        return isinstance(value, dict | list)
    if kind == "array":
        return isinstance(value, list)
    return not isinstance(value, dict | list)
