"""
S5 fix — validator for data/raw/key_players_2026.json replacement.elo_equiv
invariant.

The validator lives at scripts/pre_flight.py:validate_key_players_replacements.
It enforces: for every player record carrying a `replacement` block,
`replacement.elo_equiv ∈ [TIER_TO_ELO[player.tier], 0]` (inclusive).

Tests:
  * test_validator_passes_on_real_config — the committed JSON is clean.
  * test_validator_catches_replacement_below_elo — replacement worse than
    the out-player flips net_injury_elo positive.
  * test_validator_catches_replacement_above_zero — replacement above 0
    drives net_injury_elo deeper than `elo`.
  * test_cli_exits_1_on_error — subprocess invocation surfaces the error
    via stderr and returns a non-zero exit code (so CI catches it).

Run:
    python3 tests/live/test_key_players_config.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from pre_flight import validate_key_players_replacements  # noqa: E402


REAL_CONFIG = ROOT / "data" / "raw" / "key_players_2026.json"
PRE_FLIGHT = ROOT / "scripts" / "pre_flight.py"


def _write_temp_config(payload: dict) -> Path:
    """Write a synthetic key_players JSON to a tmp file and return its path.

    Caller is responsible for cleanup via `path.unlink(missing_ok=True)`.
    """
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(payload, tmp)
    tmp.close()
    return Path(tmp.name)


class TestKeyPlayersReplacementValidator(unittest.TestCase):
    def test_validator_passes_on_real_config(self):
        """The committed config must satisfy the invariant — if this fails,
        a curator slipped a bad replacement.elo_equiv into the JSON."""
        errors = validate_key_players_replacements(REAL_CONFIG)
        self.assertEqual(
            errors, [],
            f"real key_players_2026.json has invariant violations: {errors}"
        )

    def test_validator_catches_replacement_below_elo(self):
        """replacement.elo_equiv MORE negative than the tier floor flips
        net_injury_elo positive — the injury would 'improve' the team."""
        bad = {
            "players": [
                {"team": "X", "name": "PlayerA", "tier": "tier_1_star",
                 "replacement": {"name": "Worse", "elo_equiv": -50.0}},
            ],
        }
        path = _write_temp_config(bad)
        try:
            errors = validate_key_players_replacements(path)
            self.assertEqual(len(errors), 1, f"unexpected errors: {errors}")
            msg = errors[0]
            self.assertIn("PlayerA", msg)
            self.assertIn("-50", msg)
            # Floor for tier_1_star is -30; message must reference it so
            # the curator knows what value would be valid.
            self.assertIn("-30", msg)
        finally:
            path.unlink(missing_ok=True)

    def test_validator_catches_replacement_above_zero(self):
        """A positive replacement.elo_equiv (sign-flip typo) drives
        net_injury_elo deeper than `elo`. Validator must flag it."""
        bad = {
            "players": [
                {"team": "X", "name": "PlayerB", "tier": "tier_1_star",
                 "replacement": {"name": "TooGood", "elo_equiv": 10.0}},
            ],
        }
        path = _write_temp_config(bad)
        try:
            errors = validate_key_players_replacements(path)
            self.assertEqual(len(errors), 1, f"unexpected errors: {errors}")
            self.assertIn("PlayerB", errors[0])
            self.assertIn("10", errors[0])
        finally:
            path.unlink(missing_ok=True)

    def test_validator_clean_synthetic_passes(self):
        """A synthetic config that respects the invariant must pass —
        guards against the validator falsely flagging healthy data."""
        clean = {
            "players": [
                {"team": "X", "name": "PlayerC", "tier": "tier_1_star",
                 "replacement": {"name": "Backup", "elo_equiv": -9.6}},
                {"team": "Y", "name": "PlayerD", "tier": "tier_2_starter",
                 "replacement": {"name": "Backup", "elo_equiv": 0.0}},
            ],
        }
        path = _write_temp_config(clean)
        try:
            errors = validate_key_players_replacements(path)
            self.assertEqual(errors, [])
        finally:
            path.unlink(missing_ok=True)

    def test_validator_skips_entries_without_replacement(self):
        """Entries with no `replacement` block are out of scope for this
        validator (other gates handle the schema check)."""
        partial = {
            "players": [
                {"team": "X", "name": "PlayerE", "tier": "tier_1_star"},
            ],
        }
        path = _write_temp_config(partial)
        try:
            errors = validate_key_players_replacements(path)
            self.assertEqual(errors, [])
        finally:
            path.unlink(missing_ok=True)

    def test_validator_flags_unknown_tier(self):
        """An unrecognised tier can't be validated (no Elo floor lookup)
        — validator should flag it rather than silently skip."""
        bad = {
            "players": [
                {"team": "X", "name": "PlayerF", "tier": "tier_bogus",
                 "replacement": {"name": "Backup", "elo_equiv": -9.6}},
            ],
        }
        path = _write_temp_config(bad)
        try:
            errors = validate_key_players_replacements(path)
            self.assertEqual(len(errors), 1)
            self.assertIn("tier_bogus", errors[0])
        finally:
            path.unlink(missing_ok=True)

    def test_cli_exits_1_on_error(self):
        """Subprocess invocation with a bad config must exit 1 and emit
        the error on stderr — the contract CI hooks rely on."""
        bad = {
            "players": [
                {"team": "X", "name": "PlayerG", "tier": "tier_1_star",
                 "replacement": {"name": "Worse", "elo_equiv": -50.0}},
            ],
        }
        path = _write_temp_config(bad)
        try:
            result = subprocess.run(
                [sys.executable, str(PRE_FLIGHT),
                 "validate-key-players", str(path)],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(
                result.returncode, 1,
                f"expected exit 1; stdout={result.stdout!r} "
                f"stderr={result.stderr!r}"
            )
            self.assertIn("INVALID", result.stderr)
            self.assertIn("PlayerG", result.stderr)
        finally:
            path.unlink(missing_ok=True)

    def test_cli_exits_0_on_real_config(self):
        """Mirror of test_validator_passes_on_real_config but via the CLI
        path — confirms the dispatch + exit-code contract is right."""
        result = subprocess.run(
            [sys.executable, str(PRE_FLIGHT),
             "validate-key-players", str(REAL_CONFIG)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(
            result.returncode, 0,
            f"real config must pass via CLI; stderr={result.stderr!r}"
        )
        self.assertIn("OK", result.stdout)


def _summary(result):
    print()
    print(f"  Ran {result.testsRun} tests")
    if result.wasSuccessful():
        print("  all passed")
    else:
        print(f"  {len(result.failures)} failures, {len(result.errors)} errors")


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    _summary(result)
    sys.exit(0 if result.wasSuccessful() else 1)
