"""Resolve the `node` binary for the .gs ground-truth harness tests.

The .gs engine is the Dixon-Coles ground truth. A skipped ground-truth
check is exactly the silent-failure class Round 4 was built to kill —
so we must NOT silently skip when running under CI.

Resolution order:
  1. $WC26_NODE_BIN env var (explicit override for CI / pinned dev).
  2. shutil.which("node") — picks up the current PATH.
  3. None — node is not resolvable.

Outside CI, None is acceptable: test files use
``pytest.mark.skipif(NODE_BIN is None, ...)`` so a dev machine without
node still collects but skips these tests with a clear reason.

Under CI (``CI=true``), None is a HARD failure: importing this module
raises RuntimeError at collection time so misconfigured CI errors
loudly instead of quietly skipping the ground-truth assertions.
"""
from __future__ import annotations

import os
import shutil


def _resolve_node() -> str | None:
    """Env override wins over PATH. None if neither resolves."""
    override = os.environ.get("WC26_NODE_BIN")
    if override:
        return override
    return shutil.which("node")


def _is_ci() -> bool:
    """GitHub Actions / GitLab / CircleCI / Travis / Drone convention."""
    return os.environ.get("CI", "").lower() == "true"


NODE_BIN: str | None = _resolve_node()

if _is_ci() and NODE_BIN is None:
    raise RuntimeError(
        "CI=true but no node binary resolvable. The .gs source is the "
        "Dixon-Coles ground truth - a skipped ground-truth check is a "
        "silent failure of the class Round 4 was built to kill. Set "
        "WC26_NODE_BIN or install node in the CI image."
    )
