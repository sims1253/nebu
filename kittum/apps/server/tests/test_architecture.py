"""Module boundaries.

Each module may import only the modules listed for it below, and only through their package
(`kittum.judge`), never their submodules (`kittum.judge.prompts`). `testing` submodules are the
one exception, and only for tests. Change this table in its own pull request.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "kittum"

ALLOWED: dict[str, set[str]] = {
    "core": set(),
    "db": set(),
    "storage": {"core"},
    "ledger": {"core", "db", "storage"},
    "runs": {"core", "db", "ledger"},
    "protocol": {"core", "db", "ledger"},
    "profiles": {"core", "protocol"},
    "gateway": {"core", "db", "storage"},
    "models": {"core", "gateway"},
    "stats": {"core"},
    "search": {"core", "db", "storage", "ledger", "protocol", "gateway"},
    "enrich": {"core", "db", "storage", "gateway", "search", "models"},
    "corpus": {"core", "db", "ledger", "search", "enrich"},
    "judge": {"core", "db", "storage", "ledger", "protocol", "models"},
    "screening": {"core", "db", "ledger", "runs", "protocol", "corpus", "judge", "stats"},
    "documents": {"core", "db", "storage", "ledger", "gateway"},
    "extraction": {"core", "db", "storage", "ledger", "protocol", "documents", "models"},
    "appraisal": {"core", "db", "ledger", "protocol", "documents", "extraction", "models"},
    "report": {
        "core",
        "db",
        "storage",
        "ledger",
        "protocol",
        "search",
        "corpus",
        "screening",
        "documents",
        "extraction",
        "appraisal",
        "stats",
    },
    # Entry points may use every module's public surface.
    "api": {"*"},
    "cli": {"*"},
    "settings": set(),
}


def _module_of(path: Path) -> str:
    rel = path.relative_to(SRC)
    return rel.parts[0].removesuffix(".py")


def _is_submodule(dotted: str) -> bool:
    """Whether `kittum.a.b` names a module or package on disk, not an attribute."""
    rel = Path(*dotted.split(".")[1:])
    return (SRC / rel).is_dir() or (SRC / rel).with_suffix(".py").exists()


def _imports(path: Path) -> list[tuple[int, str]]:
    """Every imported module, with `from x import y` resolved to `x.y` when y is a module."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module))
            if node.module.split(".")[0] == "kittum":
                for alias in node.names:
                    dotted = f"{node.module}.{alias.name}"
                    if _is_submodule(dotted):
                        found.append((node.lineno, dotted))
    return found


def test_every_module_is_declared() -> None:
    modules = {_module_of(p) for p in SRC.iterdir() if p.name != "__init__.py"}
    modules.discard("py.typed")
    modules.discard("__pycache__")
    assert modules == set(ALLOWED), "add new modules to ALLOWED"


def test_imports_respect_module_boundaries() -> None:
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path == SRC / "__init__.py":
            continue
        own = _module_of(path)
        allowed = ALLOWED[own]
        for lineno, name in _imports(path):
            parts = name.split(".")
            if parts[0] != "kittum" or len(parts) == 1:
                continue
            target = parts[1]
            if target == own:
                continue
            where = f"{path.relative_to(SRC.parent)}:{lineno} imports {name}"
            if "*" not in allowed and target not in allowed:
                violations.append(f"{where}: {own} may not depend on {target}")
            elif len(parts) > 2:
                violations.append(f"{where}: import {'.'.join(parts[:2])} instead")
    assert not violations, "\n".join(violations)


def test_tests_use_public_surfaces_or_testing_helpers() -> None:
    tests = Path(__file__).resolve().parent
    violations: list[str] = []
    for path in sorted(tests.rglob("*.py")):
        for lineno, name in _imports(path):
            parts = name.split(".")
            if parts[0] != "kittum" or len(parts) <= 2:
                continue
            owner_dir = path.relative_to(tests).parts[0]
            if parts[2] == "testing" or owner_dir == parts[1]:
                continue
            violations.append(f"{path.relative_to(tests)}:{lineno} imports {name}")
    assert not violations, "\n".join(violations)
