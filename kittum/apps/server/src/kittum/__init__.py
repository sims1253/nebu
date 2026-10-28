"""Kittum: auditable, AI-assisted systematic literature reviews.

Each subpackage is one module from DESIGN.md. Import another module only through its package
(`from kittum.judge import Verdict`), never through its submodules. `tests/test_architecture.py`
enforces this and the allowed dependencies between modules.

The core is domain-neutral. Agency or jurisdiction rules live in `kittum.profiles.<id>`, which
core modules never import; profiles register at the entry points.
"""
