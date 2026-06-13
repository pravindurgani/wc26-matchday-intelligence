"""
weather_adjustments.py — Stream B.2 (weather pure-math helpers).

Pure functions for converting raw weather observations into:
  - heat_index_c (Rothfusz regression, NWS standard)
  - wet_bulb_proxy_c (Stull 2011 empirical formula)
  - weather_bucket (NWS-anchored: extreme_heat / heavy_rain / windy /
                    hot_humid / hot / cold / light_rain / normal)
  - per-team Elo adjustment based on confederation acclimatisation
    (capped at ±15 per the locked B.1 caps).

Separated from fetch_weather.py so the math is unit-testable without
hitting Open-Meteo. fetch_weather.py composes these to produce
data/live/weather_2026.json which apply_matchday_adjustments.py consumes.

References:
  - Rothfusz heat index: NWS Technical Attachment SR 90-23 (1990).
    https://www.wpc.ncep.noaa.gov/html/heatindex_equation.shtml
  - Stull wet-bulb: J. Appl. Meteor. Climatol., 50, 2267-2269 (2011).
    DOI: 10.1175/JAMC-D-11-0143.1
  - NWS rain intensity (heavy ≥ 7.6 mm/h): NWS Glossary "heavy rain".
"""
from __future__ import annotations

import math

# ── Confederation acclimatisation map ───────────────────────────────────
# Used to decide which teams are penalised by which weather conditions.
# Conservative: only the most clear-cut mismatches get a non-zero penalty.
# Roof-closed venues (handled by fetch_weather.py via venue metadata)
# zero out outdoor weather entirely.
CONFED_BY_TEAM = {
    # CONCACAF + CONMEBOL (Americas) — heat-acclimated, no humidity penalty
    "Argentina": "CONMEBOL", "Brazil": "CONMEBOL", "Colombia": "CONMEBOL",
    "Uruguay": "CONMEBOL", "Ecuador": "CONMEBOL", "Paraguay": "CONMEBOL",
    "United States": "CONCACAF", "Mexico": "CONCACAF", "Canada": "CONCACAF",
    "Panama": "CONCACAF", "Haiti": "CONCACAF", "Curacao": "CONCACAF",
    # UEFA (Europe) — temperate, penalised in hot+humid US/MX venues
    "England": "UEFA", "France": "UEFA", "Spain": "UEFA", "Portugal": "UEFA",
    "Germany": "UEFA", "Netherlands": "UEFA", "Belgium": "UEFA", "Croatia": "UEFA",
    "Switzerland": "UEFA", "Norway": "UEFA", "Sweden": "UEFA", "Austria": "UEFA",
    "Czechia": "UEFA", "Scotland": "UEFA", "Italy": "UEFA", "Turkey": "UEFA",
    "Bosnia and Herzegovina": "UEFA",
    # CAF (Africa) — heat-acclimated, possible cold penalty in Vancouver
    "Morocco": "CAF", "Egypt": "CAF", "Senegal": "CAF", "Ivory Coast": "CAF",
    "Tunisia": "CAF", "Algeria": "CAF", "DR Congo": "CAF", "Cape Verde": "CAF",
    "Ghana": "CAF", "South Africa": "CAF",
    # AFC (Asia) — generally heat-acclimated. Korean/Japanese teams less so
    # but the difference is small enough that we don't single them out.
    "Japan": "AFC", "South Korea": "AFC", "Iran": "AFC", "Australia": "AFC",
    "Saudi Arabia": "AFC", "Qatar": "AFC", "Iraq": "AFC", "Jordan": "AFC",
    "Uzbekistan": "AFC",
    # OFC
    "New Zealand": "OFC",
}

WEATHER_ELO_CAP = 15.0


# ── Heat index (Rothfusz regression, NWS standard) ──────────────────────
def heat_index_c(temp_c: float, rh_pct: float) -> float:
    """Heat index in Celsius. Undefined below ~26.7°C / 80°F — for those
    inputs we return the raw temperature (perceived = actual when cool).

    Inputs:
      temp_c: air temperature (°C)
      rh_pct: relative humidity (0-100)
    """
    # NWS guidance: heat index formula only valid when T >= 80°F (26.7°C).
    if temp_c is None or rh_pct is None or temp_c < 26.7:
        return temp_c if temp_c is not None else 0.0
    # Convert to °F for the regression
    tf = temp_c * 9.0 / 5.0 + 32.0
    rh = float(rh_pct)
    hi_f = (
        -42.379
        + 2.04901523 * tf
        + 10.14333127 * rh
        - 0.22475541 * tf * rh
        - 0.00683783 * tf * tf
        - 0.05481717 * rh * rh
        + 0.00122874 * tf * tf * rh
        + 0.00085282 * tf * rh * rh
        - 0.00000199 * tf * tf * rh * rh
    )
    # Adjustment for low-RH high-T (NWS appendix)
    if rh < 13 and 80 <= tf <= 112:
        adj = ((13 - rh) / 4) * math.sqrt((17 - abs(tf - 95)) / 17)
        hi_f -= adj
    # Adjustment for high-RH moderate-T
    elif rh > 85 and 80 <= tf <= 87:
        adj = ((rh - 85) / 10) * ((87 - tf) / 5)
        hi_f += adj
    return (hi_f - 32.0) * 5.0 / 9.0


# ── Wet-bulb proxy (Stull 2011) ────────────────────────────────────────
def wet_bulb_proxy_c(temp_c: float, rh_pct: float) -> float:
    """Stull's empirical wet-bulb. Valid 5-99% RH, -20 to +50°C.
    ±0.3°C typical error.
    """
    if temp_c is None or rh_pct is None:
        return 0.0
    t = float(temp_c)
    rh = max(5.0, min(99.0, float(rh_pct)))  # clamp to formula's valid range
    return (
        t * math.atan(0.151977 * math.sqrt(rh + 8.313659))
        + math.atan(t + rh)
        - math.atan(rh - 1.676331)
        + 0.00391838 * (rh ** 1.5) * math.atan(0.023101 * rh)
        - 4.686035
    )


# ── Weather bucket classifier (NWS-anchored thresholds) ─────────────────
def classify_weather_bucket(
    apparent_temp_c: float | None,
    rh_pct: float | None,
    precip_mm_per_h: float | None,
    wind_gust_kph: float | None,
    temp_c: float | None,
    wet_bulb_c: float | None = None,
) -> str:
    """Return one of: extreme_heat / heavy_rain / windy / hot_humid / hot /
    cold / light_rain / normal.

    Priority order matters — first match wins. Anchored to:
      - NWS heat-advisory threshold: heat index ≥ 32°C (90°F)
      - NWS "heavy rain": ≥ 7.6 mm/h (0.3 in/h)
      - Beaufort 7 onset for "windy": ~50 km/h gust
      - WBGT 30°C as proxy for unsafe-heat (no FIFA cooling-break threshold).
    """
    # extreme_heat: wet-bulb ≥ 30°C OR apparent temp ≥ 40°C
    if wet_bulb_c is not None and wet_bulb_c >= 30.0:
        return "extreme_heat"
    if apparent_temp_c is not None and apparent_temp_c >= 40.0:
        return "extreme_heat"
    # heavy_rain
    if precip_mm_per_h is not None and precip_mm_per_h >= 7.6:
        return "heavy_rain"
    # windy (Beaufort 7+)
    if wind_gust_kph is not None and wind_gust_kph >= 50.0:
        return "windy"
    # hot_humid (≥32°C AND ≥60% RH)
    if (apparent_temp_c is not None and apparent_temp_c >= 32.0
            and rh_pct is not None and rh_pct >= 60.0):
        return "hot_humid"
    # hot
    if apparent_temp_c is not None and apparent_temp_c >= 32.0:
        return "hot"
    # cold (using actual temp, not apparent)
    if temp_c is not None and temp_c <= 10.0:
        return "cold"
    # light_rain
    if precip_mm_per_h is not None and 0.5 <= precip_mm_per_h < 7.6:
        return "light_rain"
    return "normal"


# ── Per-team Elo adjustment ─────────────────────────────────────────────
# Conservative table: only the clearest mismatches earn a non-zero penalty.
# Capped at WEATHER_ELO_CAP (±15) by the consumer in apply_matchday_adjustments.
_ELO_BY_BUCKET_AND_CONFED = {
    # bucket -> {confederation: penalty}
    # Negative = the team is HURT (less acclimated to this condition).
    # Both teams may get the same penalty (no relative benefit).
    "extreme_heat": {
        "UEFA":  -12.0,   # Big penalty for European teams
        "OFC":   -8.0,
        "AFC":   -3.0,    # Some AFC teams (cooler regions like Korea/Japan)
        "CAF":    0.0,    # Acclimatised
        "CONMEBOL": 0.0,
        "CONCACAF": 0.0,
    },
    "hot_humid": {
        "UEFA":  -8.0,
        "OFC":   -5.0,
        "AFC":   -2.0,
        "CAF":    0.0,
        "CONMEBOL": 0.0,
        "CONCACAF": 0.0,
    },
    "hot": {
        "UEFA":  -4.0,
        "OFC":   -2.0,
        "AFC":   -1.0,
        "CAF":    0.0,
        "CONMEBOL": 0.0,
        "CONCACAF": 0.0,
    },
    "cold": {
        "CAF":   -4.0,    # African teams in 10°C games (rare but possible)
        "AFC":   -2.0,
        "CONMEBOL": -2.0,  # Tropical Brazil/Colombia struggle in cold
        "OFC":   -2.0,
        "UEFA":   0.0,
        "CONCACAF": 0.0,
    },
    # Rain + wind don't directly hurt one team more than another — handled
    # at the lambda level (future enhancement). Return 0 Elo for both teams.
    "heavy_rain": {},
    "light_rain": {},
    "windy": {},
    "normal": {},
}


# FIFA cooling-break protocol: when WBGT ≥ 32°C (~89.6°F), referees pause
# play at the ~30' and ~75' marks for ~3 min cooling/hydration breaks.
# Empirical evidence (Springer 2025 review on cooling-break effectiveness;
# PMC11829705 on hydration-break performance recovery) shows these breaks
# restore ~20-30% of the heat-affected high-speed running capacity that
# would otherwise be lost. We take the conservative end (25%) of that
# range and apply it as a dampener on the extreme_heat Elo penalty.
HYDRATION_BREAK_WBGT_THRESHOLD = 32.0
HYDRATION_BREAK_DAMPENER = 0.75   # multiply heat penalty by 0.75 → ~25% mitigation


def team_elo_adjustment(
    team: str, weather_bucket: str, indoor: bool = False,
    wet_bulb_c: float | None = None,
) -> float:
    """Return signed Elo adjustment for a team given a weather bucket.

    Returns 0.0 for:
      - indoor matches (roof closed → outdoor conditions don't apply)
      - teams whose confederation has no penalty for this bucket
      - unknown teams (don't penalise blind)

    `wet_bulb_c` (optional): if provided AND the bucket is `extreme_heat` AND
    WBGT ≥ HYDRATION_BREAK_WBGT_THRESHOLD, the penalty is multiplied by
    HYDRATION_BREAK_DAMPENER to reflect FIFA's mandatory cooling-break
    protocol partially restoring heat-affected performance. Callers that
    don't have a WBGT reading (e.g. static_climate fallback) pass None
    and behaviour matches pre-dampener semantics.
    """
    if indoor:
        return 0.0
    confed = CONFED_BY_TEAM.get(team)
    if confed is None:
        return 0.0
    bucket_map = _ELO_BY_BUCKET_AND_CONFED.get(weather_bucket, {})
    raw = bucket_map.get(confed, 0.0)
    # Hydration-break dampener — only fires on the extreme_heat path because
    # FIFA's cooling-break trigger is WBGT-based AND only meaningful when
    # the underlying penalty is heat-related (not rain/wind/cold/etc.).
    if (
        weather_bucket == "extreme_heat"
        and wet_bulb_c is not None
        and wet_bulb_c >= HYDRATION_BREAK_WBGT_THRESHOLD
        and raw < 0.0   # only dampen penalties (negative), not zeros
    ):
        raw = raw * HYDRATION_BREAK_DAMPENER
    # Cap at the layer ceiling — the consumer (apply_matchday_adjustments)
    # also caps, but defence-in-depth.
    return max(-WEATHER_ELO_CAP, min(WEATHER_ELO_CAP, raw))
