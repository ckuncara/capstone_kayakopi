"""Resolve repository-owned data and runtime paths from one interface."""

from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    """Return the mounted project root, overridable for packaged deployments."""
    return Path(os.getenv("KAYA_PROJECT_ROOT", Path.cwd())).resolve()

