"""DSPy/GEPA optimization layer for the schema-authoring benchmark.

Optional machinery (install with the ``authoring`` extra) that optimizes
schema writers against the deterministic benchmark scorer:

- ``janus.authoring.writer`` — ``PredictSchemaWriter`` (stable ``dspy.Predict``
  baseline) and ``FlexSchemaWriter`` (``dspy.Flex`` with a host-side
  ``validate_specification`` tool that runs the real comparison pipeline).
- ``janus.authoring.metric`` — ``build_metric`` scoring writer output through
  the same compiler/reader/extractor/engine the harness uses, producing a
  score plus natural-language feedback for GEPA's reflection.
- ``janus.authoring.optimize`` — the CLI
  (``python -m janus.authoring.optimize``) that runs ``dspy.GEPA`` over
  benchmark cases and saves the optimized program.

This package deliberately imports dspy/gepa lazily inside the modules (never
at package import), so ``import janus.authoring`` stays cheap and the server's
default dependency set never needs the extra.
"""

__all__ = ["metric", "optimize", "writer"]
