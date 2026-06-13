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
    HYDRATION_BREAK_WBGT_THRESHOLD, HYDRATION_BREAK_DAMPENER,
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


class TestHydrationBreakDampener(unittest.TestCase):
    """WBGT ≥ 32 °C → FIFA cooling-break protocol fires → dampen heat penalty.

    The dampener is bucket-specific: only `extreme_heat` (the only bucket
    triggered by a high WBGT in the first place) is dampened, and only when
    WBGT is actually supplied.
    """

    def test_constants_sane(self):
        # Sanity-check the constants stay consistent with the model design.
        self.assertEqual(HYDRATION_BREAK_WBGT_THRESHOLD, 32.0)
        self.assertGreater(HYDRATION_BREAK_DAMPENER, 0.5)
        self.assertLess(HYDRATION_BREAK_DAMPENER, 1.0)

    def test_no_wbgt_backwards_compatible(self):
        """Callers passing no WBGT see the original penalty."""
        # England UEFA extreme_heat without WBGT → full -12 (was the v1 behaviour).
        self.assertEqual(team_elo_adjustment("England", "extreme_heat"), -12.0)

    def test_wbgt_below_threshold_no_dampener(self):
        """WBGT < 32 → no cooling-break protocol → penalty unchanged."""
        # WBGT=29.5 → bucket-classifier would already pick extreme_heat
        # via the wet_bulb >= 30 threshold (weather_adjustments.py:134),
        # but FIFA cooling breaks only fire at WBGT >= 32. So we keep
        # the full penalty until the breaks actually start.
        adj = team_elo_adjustment("England", "extreme_heat", wet_bulb_c=29.5)
        self.assertEqual(adj, -12.0)

    def test_wbgt_at_threshold_dampens(self):
        """WBGT == 32 → cooling breaks → 0.75× dampener."""
        adj = team_elo_adjustment("England", "extreme_heat", wet_bulb_c=32.0)
        self.assertAlmostEqual(adj, -12.0 * 0.75)  # -9.0

    def test_wbgt_just_below_threshold_no_dampener(self):
        """Off-by-one boundary: WBGT == 31.99 must NOT dampen.

        The threshold check is `wet_bulb_c >= HYDRATION_BREAK_WBGT_THRESHOLD`
        (i.e. inclusive at 32.0 exactly). 31.99 falls one hundredth below
        and should leave the full penalty intact. This test pins the
        boundary so a future > vs >= refactor cannot quietly shift it.
        """
        adj = team_elo_adjustment("England", "extreme_heat", wet_bulb_c=31.99)
        self.assertEqual(adj, -12.0)

    def test_wbgt_just_above_threshold_dampens(self):
        """Off-by-one boundary: WBGT == 32.01 dampens, same as exactly 32.0."""
        adj = team_elo_adjustment("England", "extreme_heat", wet_bulb_c=32.01)
        self.assertAlmostEqual(adj, -12.0 * 0.75)

    def test_wbgt_well_above_threshold_dampens_same_amount(self):
        """Dampener is a flat 0.75× — no further attenuation at higher WBGT.

        Rationale: FIFA breaks are binary (2x 3min). Sustained extreme
        heat above 32 has DIMINISHING returns from the break, but
        modelling that curve adds noise we can't calibrate from public
        data. Flat dampener is the honest choice.
        """
        adj_32 = team_elo_adjustment("England", "extreme_heat", wet_bulb_c=32.0)
        adj_35 = team_elo_adjustment("England", "extreme_heat", wet_bulb_c=35.0)
        self.assertAlmostEqual(adj_32, adj_35)

    def test_dampener_only_fires_for_extreme_heat_bucket(self):
        """A hot_humid bucket with high WBGT should NOT be dampened.

        FIFA cooling breaks are tied to WBGT, but the Elo table only
        encodes heat penalties for the extreme_heat bucket. The
        hot/hot_humid penalties are smaller and shouldn't shrink further
        on borderline WBGT — that would double-attenuate.
        """
        adj = team_elo_adjustment("England", "hot_humid", wet_bulb_c=33.0)
        self.assertEqual(adj, -8.0)   # unchanged from the no-WBGT case

    def test_dampener_skips_zero_penalty_teams(self):
        """CAF/CONMEBOL/CONCACAF have 0 penalty for extreme_heat.

        Multiplying 0 × 0.75 is still 0, so the result is unchanged,
        but verify the `raw < 0.0` guard at the dampener site keeps
        the code path obvious (no silent positive→smaller-positive
        regression if the table ever changes).
        """
        for team in ("Morocco", "Brazil", "Mexico"):
            self.assertEqual(
                team_elo_adjustment(team, "extreme_heat", wet_bulb_c=34.0),
                0.0,
            )

    def test_indoor_zeroes_dampener_path(self):
        """Roof closed → no outdoor weather → also no cooling breaks."""
        self.assertEqual(
            team_elo_adjustment(
                "England", "extreme_heat", indoor=True, wet_bulb_c=35.0),
            0.0,
        )

    def test_unknown_team_unaffected(self):
        """Unknown teams (None / typo) still 0 even with WBGT supplied."""
        self.assertEqual(
            team_elo_adjustment(None, "extreme_heat", wet_bulb_c=35.0), 0.0)
        self.assertEqual(
            team_elo_adjustment("Atlantis", "extreme_heat", wet_bulb_c=35.0), 0.0)


class TestFetchWeatherWarningEmission(unittest.TestCase):
    """fetch_weather.py previously emitted ZERO warnings — asymmetric with
    the other 3 fetchers (fetch_injuries, fetch_lineups, fetch_match_stats
    all emit missing_key / http_error / fetch_error / no_records_returned).
    A wedged Open-Meteo would silently route every match to climate-bucket
    fallback with no operator signal.

    Round 12 patch K wires warnings_acc through _fetch_open_meteo and
    emits an aggregate `no_records_returned` sentinel if ALL in-horizon
    fetches failed. apply_matchday_adjustments.py's
    _PROPAGATE_WARNING_TYPES already includes these types — they lift
    automatically to the dashboard's matchday-intel detail block."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "scripts" / "live"))

    def test_http_error_emits_sampled_warning(self):
        """A 503 from Open-Meteo populates warnings_acc with an
        http_error entry tagged with match_id + truncated body."""
        from unittest.mock import patch
        import urllib.error
        import fetch_weather
        err = urllib.error.HTTPError(
            url="x", code=503, msg="x", hdrs=None, fp=None)
        # Stub e.read() so the body-decode path works.
        err.read = lambda: b"service unavailable"  # type: ignore[method-assign]
        warnings_acc: list[dict] = []
        with patch.object(fetch_weather, "_http_get_json", side_effect=err):
            result = fetch_weather._fetch_open_meteo(
                25.7, -80.2, "2026-06-15",
                match_id=12, warnings_acc=warnings_acc)
        self.assertIsNone(result)
        self.assertEqual(len(warnings_acc), 1)
        self.assertEqual(warnings_acc[0]["type"], "http_error")
        self.assertEqual(warnings_acc[0]["code"], 503)
        self.assertEqual(warnings_acc[0]["match_id"], 12)

    def test_fetch_error_emits_warning(self):
        """A network-level error (URLError/TimeoutError) emits a
        fetch_error warning with the exception class name."""
        from unittest.mock import patch
        import fetch_weather
        with patch.object(fetch_weather, "_http_get_json",
                          side_effect=TimeoutError("connection timed out")):
            warnings_acc: list[dict] = []
            result = fetch_weather._fetch_open_meteo(
                25.7, -80.2, "2026-06-15",
                match_id=7, warnings_acc=warnings_acc)
        self.assertIsNone(result)
        self.assertEqual(len(warnings_acc), 1)
        self.assertEqual(warnings_acc[0]["type"], "fetch_error")
        self.assertEqual(warnings_acc[0]["match_id"], 7)
        self.assertIn("TimeoutError", warnings_acc[0]["message"])

    def test_warning_sample_cap_prevents_explosion(self):
        """A 100% failure rate across 104 matches must NOT inflate the
        warnings array. Cap is _WARNING_SAMPLE_CAP per type (default 3).
        Aggregate sentinel in main() carries the full count regardless."""
        from unittest.mock import patch
        import urllib.error
        import fetch_weather
        err = urllib.error.HTTPError(
            url="x", code=503, msg="x", hdrs=None, fp=None)
        err.read = lambda: b"down"  # type: ignore[method-assign]
        warnings_acc: list[dict] = []
        with patch.object(fetch_weather, "_http_get_json", side_effect=err):
            for i in range(10):
                fetch_weather._fetch_open_meteo(
                    25.7, -80.2, "2026-06-15",
                    match_id=i, warnings_acc=warnings_acc)
        http_warns = [w for w in warnings_acc if w["type"] == "http_error"]
        self.assertEqual(len(http_warns), fetch_weather._WARNING_SAMPLE_CAP,
                         "sample cap must hold even under 100% failure")

    def test_no_warnings_acc_keeps_legacy_behavior(self):
        """Pre-Round-12 callers (none in this repo, but defensively) can
        still call _fetch_open_meteo without warnings_acc — failure
        path silently returns None as before."""
        from unittest.mock import patch
        import urllib.error
        import fetch_weather
        err = urllib.error.HTTPError(
            url="x", code=429, msg="x", hdrs=None, fp=None)
        err.read = lambda: b"rate limited"  # type: ignore[method-assign]
        with patch.object(fetch_weather, "_http_get_json", side_effect=err):
            result = fetch_weather._fetch_open_meteo(
                25.7, -80.2, "2026-06-15")
        self.assertIsNone(result)  # legacy behavior unchanged


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
