"""Runtime configuration resolved from the environment.

Uploads and artifacts default to a single stable directory (~/.janus) so they
survive restarts. JANUS_DATA_DIR relocates all default roots at once; each
root can also be overridden individually. Review metadata is durable only
under the sqlite store, whose path also defaults under the same root.
"""

from __future__ import annotations

import os
from pathlib import Path


def data_root() -> Path:
    return Path(os.getenv("JANUS_DATA_DIR", str(Path.home() / ".janus")))


def upload_root() -> Path:
    return Path(os.getenv("JANUS_UPLOAD_DIR", str(data_root() / "uploads" / "reviews")))


def artifact_root() -> Path:
    return Path(os.getenv("JANUS_ARTIFACT_DIR", str(data_root() / "artifacts")))
