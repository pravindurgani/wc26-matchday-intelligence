#!/usr/bin/env python3
"""Schema-drift watchdog for upstream API responses.

When api-football changes the shape of /players, /lineups, /injuries,
/fixtures, /referees responses, the fetch_* scripts can silently drop
or mis-attribute data (see CORRECTIONS.md: the `clean_sheets` regression
where `goals.get("conceded") and 0` zeroed a field the provider never
returns).

The watchdog hashes the SHAPE (set of keys at each nesting level, type
of each leaf) — not the values — and compares to a captured baseline.
Drift surfaces loudly via a warning (soft) or a non-zero CLI exit.

Usage (CLI):
    # Compare a fresh response to its baseline.
    python3 scripts/live/_schema_watchdog.py check \\
        path/to/response.json path/to/baseline.shape.json
        # exit 0 — shape matches
        # exit 8 — shape drift detected (diff printed to stdout)
        # exit 2 — usage / IO error

    # Re-capture a baseline (e.g. after confirming a drift is intentional).
    python3 scripts/live/_schema_watchdog.py snapshot \\
        path/to/response.json path/to/baseline.shape.json

Usage (library):
    from scripts.live._schema_watchdog import (
        compute_shape_hash,
        assert_shape,
    )

    payload = http_get_json(url, headers)
    # Soft alert: warn-and-continue. Does not raise on drift.
    assert_shape(payload, Path("data/live/_provider_schemas/injuries.shape.json"))

Design notes:
- Shape, NOT values: `{"id": 1}` and `{"id": 999}` hash identically.
- Lists are assumed homogeneous; only the first element's shape is taken.
  Empty lists hash as a distinct shape "list<empty>".
- Sorted key tuples — dict iteration order is not part of the shape.
- SHA-256 truncated to 16 hex chars: collisions are not a security
  concern here; we want a stable short identifier.

This module DELIBERATELY does not modify any fetch_* production code.
Wiring is left for a follow-up round; this module exposes the hook
(`assert_shape`) so the integration is a one-liner.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

# Exit codes (see CORRECTIONS.md / check_invariants.py for the existing
# 0..7 contract; 8 is new and reserved for schema-drift):
#   0 — shape matches baseline
#   2 — usage / IO error
#   8 — schema drift detected
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_DRIFT = 8

log = logging.getLogger("schema_watchdog")


# ---------------------------------------------------------------------------
# Core: shape extraction + hash
# ---------------------------------------------------------------------------

def _type_name(value: Any) -> str:
    """Canonical type-name string used at scalar leaves."""
    if value is None:
        return "null"
    if isinstance(value, bool):  # MUST precede int — bool is a subclass of int
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return type(value).__name__


def extract_shape(obj: Any) -> Any:
    """Recursively extract a shape tree from `obj`.

    Dicts -> ("dict", sorted tuple of (key, value_shape)).
    Lists -> ("list", element_shape) or ("list<empty>",).
    Scalars -> ("scalar", type_name).
    """
    if isinstance(obj, dict):
        items = tuple(
            (str(k), extract_shape(obj[k])) for k in sorted(obj.keys(), key=str)
        )
        return ("dict", items)
    if isinstance(obj, list):
        if not obj:
            return ("list<empty>",)
        # Homogeneous assumption — sample first element only.
        return ("list", extract_shape(obj[0]))
    return ("scalar", _type_name(obj))


def shape_tree(obj: Any) -> Any:
    """Human-readable nested representation of the shape.

    dicts -> dicts of {key: shape_tree(value)}, lists -> [shape_tree(first)]
    or "list<empty>", scalars -> type name string. This is what gets stored
    in the baseline JSON under "shape_tree" so a human can read the diff
    instead of having to interpret a hash.
    """
    if isinstance(obj, dict):
        return {str(k): shape_tree(obj[k]) for k in sorted(obj.keys(), key=str)}
    if isinstance(obj, list):
        if not obj:
            return "list<empty>"
        return [shape_tree(obj[0])]
    return _type_name(obj)


def compute_shape_hash(obj: Any) -> str:
    """SHA-256(repr(shape))[:16] — stable across runs, value-independent."""
    shape = extract_shape(obj)
    return hashlib.sha256(repr(shape).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Diff: human-readable explanation of a drift
# ---------------------------------------------------------------------------

def _flatten(tree: Any, prefix: str = "") -> dict[str, str]:
    """Flatten a shape_tree() into {dotted.path: type_name_or_marker}."""
    out: dict[str, str] = {}
    if isinstance(tree, dict):
        for k, v in tree.items():
            child = f"{prefix}.{k}" if prefix else k
            out.update(_flatten(v, child))
    elif isinstance(tree, list):
        # Non-empty list — descend into first element with [] suffix.
        child = f"{prefix}[]"
        if tree:
            out.update(_flatten(tree[0], child))
    elif isinstance(tree, str):
        out[prefix or "<root>"] = tree
    return out


def diff_shapes(current: Any, baseline: Any) -> list[str]:
    """Return a list of human-readable diff lines (empty => identical)."""
    cur = _flatten(shape_tree(current))
    base = _flatten(shape_tree(baseline))
    out: list[str] = []
    for path in sorted(set(cur) - set(base)):
        out.append(f"  + ADDED   {path}: {cur[path]}")
    for path in sorted(set(base) - set(cur)):
        out.append(f"  - REMOVED {path}: {base[path]}")
    for path in sorted(set(base) & set(cur)):
        if base[path] != cur[path]:
            out.append(
                f"  ~ CHANGED {path}: {base[path]} -> {cur[path]}"
            )
    return out


# ---------------------------------------------------------------------------
# Baseline I/O
# ---------------------------------------------------------------------------

def load_baseline(baseline_path: Path) -> dict:
    """Load a baseline file. Returns the parsed dict with required keys."""
    data = json.loads(Path(baseline_path).read_text())
    if not isinstance(data, dict):
        raise ValueError(
            f"baseline {baseline_path} root is not an object (got {type(data).__name__})"
        )
    if "hash" not in data or "shape_tree" not in data:
        raise ValueError(
            f"baseline {baseline_path} missing required keys 'hash' / 'shape_tree'"
        )
    return data


def write_baseline(
    baseline_path: Path,
    response: Any,
    captured: str | None = None,
) -> dict:
    """Compute and persist a baseline. Returns the written dict."""
    payload = {
        "hash": compute_shape_hash(response),
        "captured": captured or date.today().isoformat(),
        "shape_tree": shape_tree(response),
    }
    Path(baseline_path).parent.mkdir(parents=True, exist_ok=True)
    Path(baseline_path).write_text(json.dumps(payload, indent=2, sort_keys=True))
    return payload


# ---------------------------------------------------------------------------
# Soft integration hook
# ---------------------------------------------------------------------------

def assert_shape(
    response: Any,
    baseline_path: str | Path,
    *,
    raise_on_drift: bool = False,
    logger: logging.Logger | None = None,
) -> bool:
    """Compare `response` shape to baseline. Return True if matched.

    Soft default: drift logs a WARNING but does NOT raise — the tick keeps
    ticking, but the operator sees the alert. Set `raise_on_drift=True` for
    a stricter mode (e.g. CI).

    Missing baseline file is treated as a warning, not a hard failure, so
    introducing the watchdog to a fetch_* script doesn't break it.
    """
    log_ = logger or log
    bp = Path(baseline_path)
    if not bp.exists():
        log_.warning(
            "[schema_watchdog] baseline missing: %s — skipping shape check", bp
        )
        return True
    try:
        baseline = load_baseline(bp)
    except Exception as e:
        log_.warning(
            "[schema_watchdog] baseline unreadable: %s (%s) — skipping",
            bp, e,
        )
        return True
    current_hash = compute_shape_hash(response)
    if current_hash == baseline["hash"]:
        return True
    diff = diff_shapes(response, _shape_tree_to_obj(baseline["shape_tree"]))
    msg = (
        f"[schema_watchdog] SHAPE DRIFT for {bp.name}: "
        f"current={current_hash} baseline={baseline['hash']}\n"
        + "\n".join(diff)
    )
    if raise_on_drift:
        raise SchemaDriftError(msg)
    log_.warning(msg)
    return False


def _shape_tree_to_obj(tree: Any) -> Any:
    """Inverse of shape_tree() for diff purposes.

    Reconstructs a synthetic Python object whose shape matches `tree` so
    that diff_shapes(current_response, this_synthetic) works symmetrically.
    Scalars become a zero-value of their declared type.
    """
    if isinstance(tree, dict):
        return {k: _shape_tree_to_obj(v) for k, v in tree.items()}
    if isinstance(tree, list):
        return [_shape_tree_to_obj(tree[0])] if tree else []
    if tree == "list<empty>":
        return []
    if tree == "null":
        return None
    if tree == "bool":
        return False
    if tree == "int":
        return 0
    if tree == "float":
        return 0.0
    if tree == "str":
        return ""
    # Unknown — let it be a string of the type-name; diff_shapes will then
    # report it accurately.
    return tree


class SchemaDriftError(RuntimeError):
    """Raised by assert_shape(..., raise_on_drift=True) on hash mismatch."""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_check(args: argparse.Namespace) -> int:
    response_path = Path(args.response)
    baseline_path = Path(args.baseline)
    if not response_path.exists():
        print(f"[schema_watchdog] response file not found: {response_path}",
              file=sys.stderr)
        return EXIT_USAGE
    if not baseline_path.exists():
        print(f"[schema_watchdog] baseline file not found: {baseline_path}",
              file=sys.stderr)
        return EXIT_USAGE
    try:
        response = json.loads(response_path.read_text())
        baseline = load_baseline(baseline_path)
    except Exception as e:
        print(f"[schema_watchdog] failed to load files: {type(e).__name__}: {e}",
              file=sys.stderr)
        return EXIT_USAGE
    current_hash = compute_shape_hash(response)
    if current_hash == baseline["hash"]:
        print(f"OK shape matches: {current_hash}  baseline={baseline_path.name}")
        return EXIT_OK
    diff = diff_shapes(response, _shape_tree_to_obj(baseline["shape_tree"]))
    print(
        f"DRIFT current={current_hash} baseline={baseline['hash']} "
        f"({baseline_path.name})"
    )
    for line in diff:
        print(line)
    return EXIT_DRIFT


def _cmd_snapshot(args: argparse.Namespace) -> int:
    response_path = Path(args.response)
    output_path = Path(args.output)
    if not response_path.exists():
        print(f"[schema_watchdog] response file not found: {response_path}",
              file=sys.stderr)
        return EXIT_USAGE
    try:
        response = json.loads(response_path.read_text())
    except Exception as e:
        print(f"[schema_watchdog] failed to load {response_path}: {e}",
              file=sys.stderr)
        return EXIT_USAGE
    written = write_baseline(output_path, response, captured=args.captured)
    print(
        f"SNAPSHOT hash={written['hash']} captured={written['captured']} "
        f"-> {output_path}"
    )
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="_schema_watchdog",
        description="Compare or snapshot the SHAPE of an upstream API response.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("check", help="Compare a response to a baseline.")
    pc.add_argument("response", help="path to fresh response JSON")
    pc.add_argument("baseline", help="path to baseline .shape.json")
    pc.set_defaults(func=_cmd_check)

    ps = sub.add_parser("snapshot", help="Write a new baseline from a response.")
    ps.add_argument("response", help="path to response JSON")
    ps.add_argument("output", help="path to write baseline .shape.json")
    ps.add_argument(
        "--captured", default=None,
        help="capture date (YYYY-MM-DD); defaults to today",
    )
    ps.set_defaults(func=_cmd_snapshot)

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
