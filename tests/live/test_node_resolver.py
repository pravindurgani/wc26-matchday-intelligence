"""Positive tests for `_node_resolver` — proves the resolver behaves as
spec'd:

  1. ``WC26_NODE_BIN`` override wins over PATH.
  2. ``shutil.which("node")`` fallback works when no override.
  3. Under ``CI=true`` with no node resolvable, importing the module
     raises ``RuntimeError`` at collection time (loud failure, not
     silent skip).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))


# --------------------------------------------------------------------------
# 1 & 2. In-process unit tests for `_resolve_node`.
# --------------------------------------------------------------------------
def test_resolver_uses_env_override_when_set(tmp_path, monkeypatch):
    """When ``WC26_NODE_BIN`` is set, the resolver returns it verbatim
    (no PATH lookup) — even if the file is fake. CI images pin a
    specific node this way without polluting PATH."""
    fake_node = tmp_path / "node-fake"
    fake_node.write_text("#!/bin/sh\nexit 0\n")
    fake_node.chmod(0o755)
    monkeypatch.setenv("WC26_NODE_BIN", str(fake_node))

    # Reload the resolver module so the env var is picked up fresh.
    import importlib

    import _node_resolver

    importlib.reload(_node_resolver)
    assert _node_resolver._resolve_node() == str(fake_node)


def test_resolver_falls_back_to_which_when_no_override(monkeypatch):
    """No override -> resolver delegates to ``shutil.which("node")``.
    We monkeypatch ``shutil.which`` to assert the call chain rather
    than depending on the host system's actual node install."""
    monkeypatch.delenv("WC26_NODE_BIN", raising=False)

    import importlib
    import shutil

    import _node_resolver

    sentinel = "/sentinel/path/to/node"
    monkeypatch.setattr(shutil, "which", lambda name: sentinel if name == "node" else None)
    importlib.reload(_node_resolver)
    assert _node_resolver._resolve_node() == sentinel


def test_resolver_returns_none_when_unresolvable(monkeypatch):
    """No override + no node on PATH -> None. This is the dev-machine
    case where skipif takes over; only CI escalates this to an error."""
    monkeypatch.delenv("WC26_NODE_BIN", raising=False)
    monkeypatch.delenv("CI", raising=False)

    import importlib
    import shutil

    import _node_resolver

    monkeypatch.setattr(shutil, "which", lambda name: None)
    importlib.reload(_node_resolver)
    assert _node_resolver._resolve_node() is None


# --------------------------------------------------------------------------
# 3. CI hard-requirement — must FAIL at import time when node missing.
# --------------------------------------------------------------------------
def test_ci_with_no_node_raises_at_import():
    """Under CI=true, importing `_node_resolver` with no node resolvable
    MUST raise RuntimeError. Run in a subprocess with PATH cleared so
    `shutil.which` cannot find node, and `WC26_NODE_BIN` unset.

    This is the load-bearing assertion: it proves a misconfigured CI
    image cannot silently skip the .gs ground-truth tests."""
    # Build an isolated env: CI=true, empty PATH, no override.
    env = {
        "CI": "true",
        "PATH": "",
        # Keep PYTHONPATH so the subprocess can import _node_resolver
        # from tests/live/ (sys.path injection below also handles it).
        "PYTHONPATH": str(THIS_DIR),
    }
    # On macOS/Linux, preserve a couple of harmless vars so Python can
    # start (HOME isn't strictly required but doesn't help node lookup).
    for k in ("LANG", "LC_ALL"):
        if k in os.environ:
            env[k] = os.environ[k]

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; "
            f"sys.path.insert(0, {str(THIS_DIR)!r}); "
            "import _node_resolver",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode != 0, (
        f"expected non-zero exit, got {proc.returncode}\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    # The error message must surface in stderr so CI logs make the
    # cause obvious (not just "RuntimeError" with no context).
    assert "CI=true" in proc.stderr
    assert "no node binary resolvable" in proc.stderr
    assert "ground truth" in proc.stderr
    assert "WC26_NODE_BIN" in proc.stderr


def test_ci_with_env_override_does_not_raise(tmp_path):
    """Counter-test: under CI=true, if WC26_NODE_BIN points to a real
    file, the import must NOT raise. Proves the guard is conditional
    on `NODE_BIN is None`, not on `CI=true` alone."""
    fake_node = tmp_path / "node-fake"
    fake_node.write_text("#!/bin/sh\nexit 0\n")
    fake_node.chmod(0o755)

    env = {
        "CI": "true",
        "PATH": "",
        "WC26_NODE_BIN": str(fake_node),
        "PYTHONPATH": str(THIS_DIR),
    }
    for k in ("LANG", "LC_ALL"):
        if k in os.environ:
            env[k] = os.environ[k]

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; "
            f"sys.path.insert(0, {str(THIS_DIR)!r}); "
            "import _node_resolver; "
            "assert _node_resolver.NODE_BIN is not None, _node_resolver.NODE_BIN",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, (
        f"expected zero exit, got {proc.returncode}\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
