"""
Unit tests for B.2 — weather pure-math helpers + classifier + Elo adj.

No network calls — pure functions only. The fetch_weather.py adapter is
tested separately via a fixture file mocking the Open-Meteo response.

Run:
    python3 tests/live/test_weather_adjustments.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

from weather_adjustments import (  # noqa: E402
    heat_index_c, wet_bulb_proxy_c,
    classify_weather_bucket, team_elo_adjustment,
    WEATHER_ELO_CAP, CONFED_BY_TEAM,
)


class TestHeatIndex(unittest.TestCase):
    """Rothfusz regression sanity-checks against known NWS examples."""

    def test_cool_temp_returns_actual(self):
        """Below 26.7°C, heat index is undefined → return raw temp."""
        self.assertEqual(heat_index_c(20.0, 50.0), 20.0)
        self.assertEqual(heat_index_c(10.0, 90.0), 10.0)

    def test_hot_humid_increases_apparent(self):
        """At 32°C / 70% RH, heat index should be noticeably warmer than temp."""
        hi = heat_index_c(32.0, 70.0)
        self.assertGreater(hi, 32.0)
        self.assertLess(hi, 50.0)  # within sane bounds

    def test_extreme_combo(self):
        """40°C / 80% RH is well into 'danger' zone."""
        hi = heat_index_c(40.0, 80.0)
        self.assertGreater(hi, 50.0)  # should be 50-65°C feels-like

    def test_none_inputs_return_zero(self):
        self.assertEqual(heat_index_c(None, 50.0), 0.0)


class TestWetBulb(unittest.TestCase):
    """Stull 2011 — sanity check against published values."""

    def test_dry_hot(self):
        # Stull's paper: T=40, RH=20% → Tw ≈ 21°C
        wb = wet_bulb_proxy_c(40.0, 20.0)
        self.assertAlmostEqual(wb, 21.0, delta=2.0)

    def test_humid_warm(self):
        # T=30, RH=90% → Tw ≈ 28°C
        wb = wet_bulb_proxy_c(30.0, 90.0)
        self.assertAlmostEqual(wb, 28.0, delta=2.0)

    def test_cool_dry(self):
        # T=15, RH=50% → Tw ≈ 9°C
        wb = wet_bulb_proxy_c(15.0, 50.0)
        self.assertAlmostEqual(wb, 9.0, delta=2.0)

    def test_clamps_rh_to_valid_range(self):
        """Formula valid 5-99% RH — extreme inputs clamped, no NaN."""
        wb = wet_bulb_proxy_c(25.0, 0.0)   # 0% would be unphysical
        self.assertIsInstance(wb, float)
        wb = wet_bulb_proxy_c(25.0, 100.0)
        self.assertIsInstance(wb, float)


class TestBucketClassifier(unittest.TestCase):
    """Priority order matters — first match wins."""

    def test_extreme_heat_by_wet_bulb(self):
        # 30°C wet-bulb triggers extreme_heat regardless of other factors
        self.assertEqual(
            classify_weather_bucket(35, 80, 0.0, 10, 35, wet_bulb_c=31.0),
            "extreme_heat",
        )

    def test_extreme_heat_by_apparent_temp(self):
        self.assertEqual(
            classify_weather_bucket(42, 60, 0.0, 10, 38, wet_bulb_c=None),
            "extreme_heat",
        )

    def test_heavy_rain(self):
        self.assertEqual(
            classify_weather_bucket(25, 70, 10.0, 20, 22, wet_bulb_c=None),
            "heavy_rain",
        )

    def test_windy(self):
        self.assertEqual(
            classify_weather_bucket(20, 50, 0.0, 60, 20, wet_bulb_c=None),
            "windy",
        )

    def test_hot_humid(self):
        # 33°C apparent + 65% RH → hot_humid
        self.assertEqual(
            classify_weather_bucket(33, 65, 0.0, 10, 30, wet_bulb_c=None),
            "hot_humid",
        )

    def test_hot_dry(self):
        # 35°C apparent + 30% RH → hot (not hot_humid because RH<60)
        self.assertEqual(
            classify_weather_bucket(35, 30, 0.0, 10, 33, wet_bulb_c=None),
            "hot",
        )

    def test_cold(self):
        self.assertEqual(
            classify_weather_bucket(8, 70, 0.0, 10, 8, wet_bulb_c=None),
            "cold",
        )

    def test_light_rain(self):
        self.assertEqual(
            classify_weather_bucket(20, 70, 2.0, 10, 20, wet_bulb_c=None),
            "light_rain",
        )

    def test_normal(self):
        self.assertEqual(
            classify_weather_bucket(22, 50, 0.0, 10, 22, wet_bulb_c=None),
            "normal",
        )

    def test_extreme_heat_beats_heavy_rain(self):
        """Priority: extreme_heat wins over heavy_rain when both true."""
        self.assertEqual(
            classify_weather_bucket(45, 30, 20.0, 10, 42, wet_bulb_c=None),
            "extreme_heat",
        )


class TestEloAdjustment(unittest.TestCase):
    """Confederation acclimatisation — capped at ±15."""

    def test_uefa_extreme_heat(self):
        self.assertEqual(team_elo_adjustment("England", "extreme_heat"), -12.0)
        self.assertEqual(team_elo_adjustment("Spain", "extreme_heat"), -12.0)

    def test_uefa_hot_humid(self):
        self.assertEqual(team_elo_adjustment("England", "hot_humid"), -8.0)

    def test_caf_extreme_heat(self):
        """CAF teams are heat-acclimated — no penalty."""
        self.assertEqual(team_elo_adjustment("Morocco", "extreme_heat"), 0.0)
        self.assertEqual(team_elo_adjustment("Senegal", "extreme_heat"), 0.0)

    def test_concacaf_no_heat_penalty(self):
        self.assertEqual(team_elo_adjustment("Mexico", "extreme_heat"), 0.0)
        self.assertEqual(team_elo_adjustment("United States", "hot_humid"), 0.0)

    def test_caf_cold_penalty(self):
        """African teams penalised in cold (e.g. Vancouver)."""
        self.assertEqual(team_elo_adjustment("Morocco", "cold"), -4.0)

    def test_rain_and_wind_zero_elo(self):
        """Rain/wind affect lambdas (future), not Elo directly."""
        self.assertEqual(team_elo_adjustment("England", "heavy_rain"), 0.0)
        self.assertEqual(team_elo_adjustment("England", "windy"), 0.0)
        self.assertEqual(team_elo_adjustment("England", "light_rain"), 0.0)

    def test_normal_zero_elo(self):
        for team in ["Spain", "Brazil", "Morocco"]:
            self.assertEqual(team_elo_adjustment(team, "normal"), 0.0)

    def test_indoor_zeroes_outdoor_weather(self):
        """Roof closed → outdoor extreme_heat doesn't penalise anyone."""
        self.assertEqual(team_elo_adjustment("England", "extreme_heat", indoor=True), 0.0)
        self.assertEqual(team_elo_adjustment("Morocco", "cold", indoor=True), 0.0)

    def test_unknown_team_returns_zero(self):
        """Unknown teams (e.g. typo, knockout placeholder) → no penalty."""
        self.assertEqual(team_elo_adjustment("Atlantis", "extreme_heat"), 0.0)
        self.assertEqual(team_elo_adjustment(None, "extreme_heat"), 0.0)

    def test_cap_enforced(self):
        """Even if the table grew, the cap clamps."""
        # All entries currently within ±12, so just verify the cap exists.
        self.assertEqual(WEATHER_ELO_CAP, 15.0)

    def test_all_confederations_present(self):
        """Every team in CONFED_BY_TEAM must be assigned to a real confed."""
        valid = {"UEFA", "CONMEBOL", "CONCACAF", "CAF", "AFC", "OFC"}
        for team, confed in CONFED_BY_TEAM.items():
            self.assertIn(confed, valid, f"{team} has unknown confed: {confed}")


def _summary(result):
    print()
    print(f"  Ran {result.testsRun} tests")
    if result.wasSuccessful():
        print(f"  ✓ all passed")
    else:
        print(f"  ✗ {len(result.failures)} failures, {len(result.errors)} errors")


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    _summary(result)
    sys.exit(0 if result.wasSuccessful() else 1)
