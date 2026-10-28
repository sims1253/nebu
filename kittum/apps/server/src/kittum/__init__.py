"""Kittum: auditable systematic literature reviews for AMNOG dossiers.

Each subpackage is one module from DESIGN.md. Import another module only through its package
(`from kittum.judge import Verdict`), never through its submodules. `tests/test_architecture.py`
enforces this and the allowed dependencies between modules.
"""
