"""
_degrade.py — graceful-degradation helpers for apply_matchday_adjustments.

Round 5 hardening: the math layer in each subsystem (injury, lineup, referee,
suspension, stats_proxy) now raises loudly on bad inputs (ValueError on NaN
xG / non-finite Elo, TypeError on non-str player names, etc.). That's the
right safety posture at the math layer — silence-by-default would mask
provider regressions.

But on matchday morning a single NaN xG record must NOT crash the entire
live-update tick. The orchestrator catches per-record AND per-subsystem,
records a structured warning, and continues.

Two helpers exported:

  degrade_record(...)   — wrap a per-record call. On exception, append a
                          structured warning to the accumulator and return
                          a sentinel `None` so the caller can skip the
                          record. Catches (ValueError, TypeError, KeyError,
                          OverflowError) — anything narrower than that is
                          a bug, anything wider would hide programmer
                          errors (NameError, ImportError).

  degrade_subsystem(...) — wrap a whole _load_*_components call. On
                           exception, append a `subsystem_degraded`
                           warning and return an empty {} so the merge
                           step proceeds with zero contribution from that
                           subsystem. Same exception class allowlist.

Both append to a list passed in by reference so the orchestrator can
surface every warning in the consolidated state's `degradation_warnings`
field.

Schema of a degradation warning record:

  {
    "subsystem":       str,   # "injury" | "lineup" | "referee" | ...
    "scope":           str,   # "record" | "subsystem"
    "record_id":       str,   # caller-supplied; e.g. "team=Spain m=12"
    "exception_class": str,   # type(e).__name__
    "message":         str,   # str(e), truncated to 200 chars
    "ts":              str,   # ISO-8601 UTC
  }
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

_DEGRADABLE = (ValueError, TypeError, KeyError, OverflowError)
_MSG_TRUNCATE = 200

T = TypeVar("T")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_warning(subsystem: str, scope: str, record_id: str, exc: BaseException) -> dict:
    msg = str(exc)
    if len(msg) > _MSG_TRUNCATE:
        msg = msg[:_MSG_TRUNCATE] + "…"
    return {
        "subsystem": subsystem,
        "scope": scope,
        "record_id": record_id,
        "exception_class": type(exc).__name__,
        "message": msg,
        "ts": _now_iso(),
    }


def degrade_record(
    subsystem: str,
    record_id: str,
    fn: Callable[[], T],
    warnings_acc: list,
) -> T | None:
    """Run `fn()` (a per-record subsystem call). On a degradable exception,
    log to stderr, append a structured warning, and return None so the
    caller can `continue` past this record. Re-raise anything outside the
    allowlist — those are bugs, not data issues."""
    try:
        return fn()
    except _DEGRADABLE as e:
        warnings_acc.append(_make_warning(subsystem, "record", record_id, e))
        print(
            f"[matchday] WARN: {subsystem} record skipped "
            f"({record_id}): {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return None


def degrade_subsystem(
    subsystem: str,
    fn: Callable[[], dict],
    warnings_acc: list,
) -> dict:
    """Run `fn()` (the entire _load_*_components call). On a degradable
    exception, log to stderr, append a structured `subsystem_degraded`
    warning, and return {} so the merge step gets zero contribution from
    this subsystem. Other subsystems still produce output."""
    try:
        return fn() or {}
    except _DEGRADABLE as e:
        warnings_acc.append(_make_warning(subsystem, "subsystem", subsystem, e))
        print(
            f"[matchday] WARN: subsystem degraded ({subsystem}): "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return {}
