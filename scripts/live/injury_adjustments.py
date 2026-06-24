"""
injury_adjustments.py — Stream B.3 pure helpers.

Tier table + classification helpers shared by fetch_injuries.py (writes the
canonical injuries_2026.json) and apply_matchday_adjustments.py (consumes it).

Separated from the fetcher so the math is unit-testable without hitting
API-Football.

Tiers come from the locked spec in data/live/team_adjustments.json's
tier_guide block (kept stable across the manual-overlay → API migration so
existing operator notes still apply):

  tier_1_star    — Mbappé / Bellingham / Vinícius / Haaland / Rodri-level   -30
  tier_1_keeper  — starting GK at a top-10 team                              -25
  tier_2_starter — regular outfield starter, not headline player             -12
  tier_3_squad   — rotation player                                            -4
  doubtful       — 0.5× the tier amount
  suspended      — full tier penalty (for that match only — caller handles)

API-Football's /injuries endpoint exposes `player.type`:
  "Missing Fixture"  → confirmed out      → status = "confirmed_out"
  "Questionable"     → doubtful           → status = "doubtful"

It does NOT expose per-player importance. For v2 we cross-reference each
injured player against data/raw/key_players_2026.json — a small hand-curated
whitelist of obvious tier_1_star + tier_1_keeper names per WC26 squad — and
upgrade matching entries from the conservative tier_2_starter default.
Manual overrides in team_adjustments.json still take priority at the
apply_matchday_adjustments layer.
"""
from __future__ import annotations

import json
import math
import unicodedata
from pathlib import Path

TIER_TO_ELO = {
    "tier_1_star":    -30.0,
    "tier_1_keeper":  -25.0,
    "tier_2_starter": -12.0,
    "tier_3_squad":    -4.0,
}

# API-Football `player.type` → our status taxonomy
APIFOOTBALL_TYPE_MAP = {
    "Missing Fixture": "confirmed_out",
    "Questionable":    "doubtful",
    # Defensive fallthrough: anything else (e.g. "Suspended") treated as out.
    "Suspended":       "confirmed_out",
    "Coach Decision":  "doubtful",
}

DOUBTFUL_DISCOUNT = 0.5
DEFAULT_TIER = "tier_2_starter"  # conservative v1 default for API-sourced players

# Phase 6 rollout flag (CORRECTIONS.md §7). When False, fetch_injuries still
# computes the auto-tier suggestion and attaches it per-player for the
# disagreement-diff CLI, BUT the active `tier` used to penalise Elo remains
# the override / DEFAULT_TIER answer. Flip to True only after the operator
# has reviewed scripts/live/auto_tier_diff.py output against a fresh
# data/live/player_stats_2026.json snapshot.
AUTO_TIER_ACTIVE = False

# Path to the hand-curated tier_1 whitelist. Resolved here so both the
# fetcher and the consumer share one source of truth; the loader is cached
# so the disk read happens once per process.
ROOT = Path(__file__).resolve().parents[2]
KEY_PLAYERS_PATH = ROOT / "data" / "raw" / "key_players_2026.json"


def classify_api_type(player_type: str | None) -> str:
    """Map API-Football `player.type` string → our `status` taxonomy."""
    # Fix #8 (Wave-B R4): bytes is truthy AND has a .strip() method that
    # returns bytes; dict.get(bytes-key) then returns None and the
    # fallback silently routes the record to confirmed_out. Reject any
    # non-str/non-None input loudly so callers can't pretend a bytes
    # payload was a clean classification.
    if player_type is not None and not isinstance(player_type, str):
        raise TypeError(
            f"classify_api_type expected str or None, got "
            f"{type(player_type).__name__}"
        )
    if not player_type:
        return "confirmed_out"
    return APIFOOTBALL_TYPE_MAP.get(player_type.strip(), "confirmed_out")


def tier_elo(tier: str) -> float:
    """Look up tier penalty (signed; negative)."""
    return TIER_TO_ELO.get(tier, 0.0)


def discounted_elo(tier: str, status: str) -> float:
    """Apply status-based discount to the tier penalty.

    confirmed_out → full tier penalty
    doubtful      → 0.5× tier penalty
    anything else → 0 (defensive; unknown status shouldn't quietly leak Elo)
    """
    base = tier_elo(tier)
    if status == "confirmed_out":
        return base
    if status == "doubtful":
        return base * DOUBTFUL_DISCOUNT
    return 0.0


# ── Name normalisation + tier classification (v2 auto-upgrade) ──────────

# Stroke / ligature characters that NFKD does NOT decompose — the
# character has no base letter + combining mark structure, it's a
# self-contained codepoint, so .encode("ascii", "ignore") drops it
# silently (Ødegaard → degaard, Łewandowski → ewandowski).
# We pre-translate these BEFORE NFKD so the resulting normalised
# string is a stable, comparable key. Map covers every codepoint
# we expect across WC2026 squads (Nordic, Polish, Icelandic, German,
# Croatian, French ligature, OE) plus a few currency-neutral siblings
# in case the API returns oddities.
_NORDIC_SLAVIC_MAP = {
    "\u00d8": "O",  "\u00f8": "o",    # Ø ø — Nordic O-slash (Ødegaard, Højbjerg)
    "\u0141": "L",  "\u0142": "l",    # Ł ł — Polish L-slash (Łewandowski)
    "\u00d0": "D",  "\u00f0": "d",    # Ð ð — Icelandic eth
    "\u00de": "Th", "\u00fe": "th",   # Þ þ — Icelandic thorn
    "\u00c6": "AE", "\u00e6": "ae",   # Æ æ — Nordic/Old-English ligature
    "\u0152": "OE", "\u0153": "oe",   # Œ œ — French ligature
    "\u0110": "D",  "\u0111": "d",    # Đ đ — Croatian/Serbian D-stroke (Đoković, Mihailović)
    "\u00df": "ss",                   # ß — German sharp s
    # Turkish dotted/dotless I — NFKD decomposes neither. Without these,
    # 'Uğurcan Çakır' → 'ugurcan cakr' (ı dropped) and 'İlkay' → 'lkay'
    # (İ dropped). Both miss the whitelist.
    "\u0130": "I",  "\u0131": "i",    # İ ı — Turkish dotted-I / dotless-i
    # Greek omicron-like? (skip — out of scope for football names)
    # Cyrillic transliteration intentionally not handled — API-Football
    # already returns Latin script for WC2026 squads.
}


def normalize_player_name(name: str | None) -> str:
    """Collapse a player name to a comparable key.

    - pre-translate stroke / ligature characters NFKD won't decompose
      (Ø → O, Ł → L, Đ → D, Æ → AE, ß → ss, Þ → Th, etc.)
    - NFKD decompose remaining accented letters (é → e + ́)
    - drop everything outside ASCII (the combining marks above)
    - lowercase
    - replace periods, commas, apostrophes with spaces
    - collapse whitespace

    Designed so 'Kylian Mbappé', 'K. Mbappé', 'mbappe' (last-name only),
    'K Mbappe' all collapse to comparable forms. Also handles
    'Martin Ødegaard' → 'martin odegaard' and 'Robert Łewandowski'
    → 'robert lewandowski' which a naive NFKD-only pipeline silently
    truncates to 'degaard' / 'ewandowski'. Callers use `classify_tier`
    which handles full-name AND multi-token last-name matching.
    """
    if not name:
        return ""
    # Fix #5 (Wave-B R4): `str(name)` previously coerced a list / dict
    # straight through the pipeline (`str(['a','b'])` → `"['a', 'b']"`),
    # leaving brackets and quotes in the normalised key and breaking every
    # downstream whitelist comparison. Reject non-str loudly so the
    # provider-shape regression doesn't sneak through.
    if not isinstance(name, str):
        raise TypeError(
            f"normalize_player_name expected str or None, got "
            f"{type(name).__name__}"
        )
    text = name
    # Step 1: pre-translate stroke/ligature characters that NFKD doesn't
    # decompose. Without this, 'Ødegaard' silently becomes 'degaard' and
    # an injury to Norway's tier_1_star routes to tier_2_starter (-18 Elo
    # undercount).
    for src, dst in _NORDIC_SLAVIC_MAP.items():
        if src in text:
            text = text.replace(src, dst)
    # Step 2: NFKD decompose any remaining accented letters so 'é' → 'e'.
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_only = nfkd.encode("ascii", "ignore").decode("ascii")
    cleaned = ascii_only.lower().replace(".", " ").replace(",", " ").replace("'", "")
    return " ".join(cleaned.split())


def player_join_key(name: str | None) -> str:
    """Stronger normalization for cross-feed JOIN keys.

    `normalize_player_name` keeps the first-name tokens — fine for tier
    lookup against a curated index (`Kylian Mbappé` and `K. Mbappé` both
    classify_tier-match by last-name token). But for CROSS-FEED dedup
    (suspension yellow accumulation, cross-subsystem absentee dedup),
    provider drift between `R. Jiménez` and `Raúl Jiménez` keeps them on
    different keys and silently splits the count.

    The join key drops single-character "initial" tokens and keeps the
    remaining tokens joined. If only initials + last-name remain (e.g.
    'R Jimenez'), the key collapses to just the last token. Use this
    when joining card/injury events from independent feeds where the
    operator's source-of-truth is the (team, surname) pair, not full
    forenames — combined with the team scoping already in the tuple key,
    intra-team same-surname collisions remain rare enough to favour
    aggressive deduplication.
    """
    norm = normalize_player_name(name)
    if not norm:
        return ""
    tokens = norm.split()
    # Drop single-character initials so 'r jimenez' and 'raul jimenez'
    # both reduce to a shared key.
    significant = [t for t in tokens if len(t) > 1]
    if not significant:
        return norm
    if len(significant) == 1:
        return significant[0]
    # Two or more significant tokens: keep the last token as the join
    # key (cross-feed convention: surname is the stable identifier;
    # forenames drift more across providers than surnames do).
    return significant[-1]


def _load_key_players_index(path: Path = KEY_PLAYERS_PATH) -> dict:
    """Build a tiered lookup: {team: {by_full: {full: entry},
                                      by_last: {last: [entry, ...]}}}.

    Indexed per-team to disambiguate same-last-name players on different
    sides (e.g. multiple 'Martínez' across teams). Within a team, the
    by_last index stores a LIST of entries per surname so that intra-team
    collisions (Argentina's Lautaro + Emiliano Martínez) can be
    disambiguated at lookup time by classify_tier() via forename prefix.

    Mononyms (entries where the full normalised name is identical to the
    last-name token, e.g. Pedri, Rodri, Rodrygo, Raphinha, Alisson) are
    deliberately EXCLUDED from the by_last index — they can only match
    via exact `by_full` lookup. Without this guard, any same-team player
    whose surname accidentally collides with the mononym would
    false-positive auto-upgrade (e.g. a fictional 'Carlos Pedri' on Spain
    would inherit Pedri's tier_1_star).

    Falls back to an empty index if the file is missing — fetch_injuries
    then defaults every player to tier_2_starter (the pre-v2 behaviour).
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    index: dict[str, dict[str, dict]] = {}
    for entry in data.get("players", []) or []:
        team = entry.get("team")
        if not team:
            continue
        bucket = index.setdefault(team, {"by_full": {}, "by_last": {}})
        full = entry.get("name_normalized") or normalize_player_name(entry.get("name"))
        last = entry.get("last_name_normalized") or (full.split()[-1] if full else "")
        if full:
            bucket["by_full"][full] = entry
        # Skip mononyms from by_last: the surname alone IS the player's
        # canonical name, so by_full carries the match. Indexing them
        # under by_last would let any same-team teammate with that
        # surname auto-promote incorrectly.
        if last and last != full:
            bucket["by_last"].setdefault(last, []).append(entry)
        # Aliases — per-entry list of additional full-form strings the API
        # might emit that don't match the canonical name_normalized. The
        # most common cases:
        #   - "Son" bare (canonical = "Son Heung-min" but API can drop
        #     given name for Korean players)
        #   - "Vini Jr" / "Vinicius" (canonical = "Vinicius Junior")
        #   - "Mohammed Salah" (English spelling drift — also handled by
        #     the bidirectional prefix in _resolve_from_last_match)
        # Registered ONLY in by_full (not by_last) so they cannot widen
        # the surname leak surface — the team-scoped index already
        # prevents cross-team confusion.
        for alias_raw in (entry.get("aliases") or []):
            alias_norm = normalize_player_name(alias_raw)
            if alias_norm:
                bucket["by_full"].setdefault(alias_norm, entry)
    return index


# Process-level cache so a single fetch_injuries run reads the file once.
_KEY_PLAYERS_INDEX: dict | None = None


def _get_key_players_index() -> dict:
    global _KEY_PLAYERS_INDEX
    if _KEY_PLAYERS_INDEX is None:
        _KEY_PLAYERS_INDEX = _load_key_players_index()
    return _KEY_PLAYERS_INDEX


def reset_key_players_index_for_tests() -> None:
    """Tests that swap KEY_PLAYERS_PATH at runtime must call this to bust
    the process-level cache before the next classify_tier()."""
    global _KEY_PLAYERS_INDEX
    _KEY_PLAYERS_INDEX = None


def _resolve_from_last_match(matches: list[dict],
                              forename_tokens: list[str],
                              n_tokens_in_last: int
                              ) -> tuple[str, str, dict | None]:
    """Given a non-empty by_last hit, decide which whitelist entry (if any)
    the input actually refers to and return (tier, source).

    Invariants enforced here (closes HIGH #10 — single-entry surname leak):
      * If the input HAS forename tokens, the input's first forename token
        MUST be a prefix of the candidate's first forename token. So
        "Carlos Mbappe" / "John Haaland" / "Random Kane" no longer auto-
        promote just because the surname is whitelisted under a single
        entry. The pre-fix branch only enforced this on multi-entry
        collisions; the single-entry path bypassed all forename checks.
      * If the input is bare-surname (no forename tokens) AND there's
        exactly ONE candidate, accept — the team-scoped index already
        prevents cross-team leaks, and most API short-forms fall here.
      * Multi-entry collisions still resolve via forename-prefix
        disambiguator (Round 7 fix) and fall back to "whitelist_ambiguous"
        when forename doesn't uniquely identify a candidate.
    """
    if not forename_tokens:
        # Bare surname. Single match → accept. Collision → ambiguous.
        if len(matches) == 1:
            return matches[0].get("tier", DEFAULT_TIER), "whitelist_last", matches[0]
        return DEFAULT_TIER, "whitelist_ambiguous", None
    input_first = forename_tokens[0]
    disambiguated = []
    for entry in matches:
        entry_full = entry.get("name_normalized") or normalize_player_name(
            entry.get("name"))
        entry_tokens = entry_full.split()
        if len(entry_tokens) <= n_tokens_in_last:
            # Candidate is a mononym / has no forename portion to compare.
            continue
        entry_first_forename = entry_tokens[0]
        # Bidirectional prefix: either side starting with the other
        # counts as a match. Standard direction handles initial-form
        # inputs ('K. Mbappé' → 'kylian'.startswith('k')). The reverse
        # direction handles inputs that extend the canonical forename
        # (e.g. 'Karima Benzema' against canonical 'karim benzema' —
        # 'karima'.startswith('karim') is True). It does NOT rescue
        # spelling variants where the divergence happens mid-string
        # ('mohammed' vs 'mohamed' diverge at index 5 → both directions
        # False); those are handled explicitly by the aliases field.
        # Safe because intra-team forename pairs don't share leading
        # prefixes on the current whitelist (pre-flight gates this
        # property going forward — see the new overlap gate below).
        if (entry_first_forename.startswith(input_first)
                or input_first.startswith(entry_first_forename)):
            disambiguated.append(entry)
    if len(disambiguated) == 1:
        return disambiguated[0].get("tier", DEFAULT_TIER), "whitelist_last", disambiguated[0]
    if len(matches) > 1:
        # Genuine collision with no unique forename resolution —
        # operator visibility path.
        return DEFAULT_TIER, "whitelist_ambiguous", None
    # Single match but the input's first forename didn't prefix the
    # candidate's first forename → the input is a DIFFERENT player who
    # happens to share the surname. Conservative default; no audit
    # ambiguity (we know this isn't the whitelisted player).
    return DEFAULT_TIER, "default", None


def classify_tier(player_name: str | None, team_name: str | None,
                  index: dict | None = None) -> tuple[str, str]:
    """Return (tier, source) for an injured player.

    Matching strategy (per-team):
      1. Full normalized name match → use whitelist tier
         (source = "whitelist_full").
      2. Trailing-window last-name match — try 3-token, 2-token, then
         1-token windows so compound surnames ("De Bruyne", "van Dijk",
         "de Jong") match the by_last keys that store the multi-word
         form. Forename-prefix check ALWAYS runs (even with a single
         candidate) so "Carlos Mbappe" no longer silently inherits
         Mbappé's tier_1_star. Multi-entry collisions (Argentina's
         Lautaro + Emiliano Martínez) resolve via forename or fall back
         to "whitelist_ambiguous".
      3. Leading-window last-name match — surname-FIRST fallback for
         providers that occasionally return "MARTINEZ Emiliano",
         "VAN DIJK Virgil", "Pulisic, Christian" (after normalization
         the comma collapses to whitespace). Same forename-prefix
         guarantees apply. This step only fires if step 2 didn't return.
      4. Otherwise fall through to DEFAULT_TIER (source = "default").

    Same team filtering prevents 'Martínez' (Argentina) auto-upgrading a
    different player called 'Martínez' on Mexico. Caller may pass a
    custom `index` for testing; otherwise the cached on-disk index is used.

    Audit sources (controlled vocabulary, pinned by test contract):
      whitelist_full       — exact full-name match in by_full
      whitelist_last       — unique trailing- or leading-window match
                             in by_last (forename check passed)
      whitelist_ambiguous  — collision present, can't disambiguate →
                             returns DEFAULT_TIER but flags the audit log
      default              — no whitelist signal OR forename rejected
                             a single-candidate surname hit
    """
    tier, source, _entry = _classify_tier_internal(player_name, team_name, index)
    return tier, source


def _classify_tier_internal(player_name: str | None, team_name: str | None,
                            index: dict | None = None
                            ) -> tuple[str, str, dict | None]:
    """Shared lookup: returns (tier, source, matched_entry_or_None).

    Public callers go through classify_tier() (2-tuple, test-pinned) or
    classify_tier_with_replacement() (surfaces replacement.elo_equiv for
    Phase 1B net-injury-elo wiring at fetch_injuries.py:203).
    """
    # Fix #9 (Wave-B R4): the legacy `if not player_name` guard accepted
    # any truthy non-string (int 12345, list ['a','b'], dict {...}) and
    # let it flow into normalize_player_name -> by_last lookup, returning
    # ('tier_2_starter', 'default') with no warning. Reject non-str input
    # explicitly so the upstream provider-shape regression surfaces.
    if player_name is not None and not isinstance(player_name, str):
        raise TypeError(
            f"classify_tier expected player_name str or None, got "
            f"{type(player_name).__name__}"
        )
    if team_name is not None and not isinstance(team_name, str):
        raise TypeError(
            f"classify_tier expected team_name str or None, got "
            f"{type(team_name).__name__}"
        )
    if not player_name or not team_name:
        return DEFAULT_TIER, "default", None
    idx = index if index is not None else _get_key_players_index()
    bucket = idx.get(team_name)
    if not bucket:
        return DEFAULT_TIER, "default", None
    norm = normalize_player_name(player_name)
    if not norm:
        return DEFAULT_TIER, "default", None
    full_hit = bucket.get("by_full", {}).get(norm)
    if full_hit:
        return full_hit.get("tier", DEFAULT_TIER), "whitelist_full", full_hit
    by_last = bucket.get("by_last", {})
    tokens = norm.split()
    max_window = min(3, len(tokens))
    # Step 2: trailing-window search (forename-first, the common API format).
    for n_tokens in range(max_window, 0, -1):
        candidate = " ".join(tokens[-n_tokens:])
        matches = by_last.get(candidate, [])
        if not matches:
            continue
        forename_tokens = tokens[:-n_tokens]
        return _resolve_from_last_match(matches, forename_tokens, n_tokens)
    # Step 3: leading-window fallback (surname-first format). Only fires
    # if step 2 found no by_last hit at all. Requires ≥2 input tokens so
    # at least one forename token remains for the disambiguator.
    if len(tokens) >= 2:
        max_leading = min(3, len(tokens) - 1)
        for n_lead in range(max_leading, 0, -1):
            candidate_last = " ".join(tokens[:n_lead])
            matches = by_last.get(candidate_last, [])
            if not matches:
                continue
            forename_tokens = tokens[n_lead:]
            return _resolve_from_last_match(matches, forename_tokens, n_lead)
    return DEFAULT_TIER, "default", None


def classify_tier_with_replacement(
    player_name: str | None,
    team_name: str | None,
    index: dict | None = None,
) -> tuple[str, str, float | None]:
    """Like classify_tier() but also returns the matched whitelist entry's
    replacement.elo_equiv (or None if no replacement data is present).

    Used by fetch_injuries.py to compute net_elo = elo - replacement_elo
    per CORRECTIONS.md §1 (Phase 1B). Falls back to None when:
      - no whitelist match (default tier / ambiguous)
      - entry has no `replacement` block
      - replacement.elo_equiv is missing or non-numeric
    """
    _tier, _source, entry = _classify_tier_internal(player_name, team_name, index)
    if not entry:
        return _tier, _source, None
    replacement = entry.get("replacement") or {}
    val = replacement.get("elo_equiv")
    if not isinstance(val, (int, float)):
        return _tier, _source, None
    return _tier, _source, float(val)


def net_injury_elo(elo: float, replacement_elo: float | None) -> float:
    """Compute net injury Elo penalty per CORRECTIONS.md §1.

    Definition: net_elo = elo - replacement_elo (if replacement data exists),
    else net_elo = elo. The raw `elo` is preserved by callers for backward
    compat; `net_elo` is the new value the apply layer consumes when
    `net_elo_active` is true in injuries_2026.json.

    Invariant (S5 fix): the result is bounded to `[elo, 0]`. An injury can
    never be beneficial (`net <= 0`) and can never be worse than losing the
    player to a zero-quality replacement (`net >= elo`, the floor when
    replacement.elo_equiv = 0). Equivalently: replacement.elo_equiv must lie
    in `[elo, 0]`. The clamp here guards against author-entered data slips
    in data/raw/key_players_2026.json AND against doubtful-status edge cases
    where the post-discount `elo` shrinks below `replacement_elo` in
    magnitude (e.g. tier_3_squad doubtful = -2 vs fixed replacement = -9.6
    would otherwise emit net = +7.6 — an injury that improves the team).
    The compile-time gate (scripts/pre_flight.py
    `validate_key_players_replacements`) blocks bad config; this runtime
    clamp is the belt-and-braces second line of defence.
    """
    # Fixes #6 + #7 (Wave-B R4): NaN and ±Inf both used to pass through
    # unchanged, silently poisoning the team total whenever an upstream
    # malformed replacement_elo or stale auto_tier component injected a
    # non-finite value. A single math.isfinite() guard covers both. We
    # raise loudly so the bad value is traced back to its source rather
    # than buried in a team total of `nan`.
    elo_f = float(elo)
    if not math.isfinite(elo_f):
        raise ValueError(
            f"net_injury_elo received non-finite elo={elo!r}"
        )
    if replacement_elo is None:
        return elo_f
    replacement_f = float(replacement_elo)
    if not math.isfinite(replacement_f):
        raise ValueError(
            f"net_injury_elo received non-finite "
            f"replacement_elo={replacement_elo!r}"
        )
    raw = elo_f - replacement_f
    # Clamp: an injury is bounded by [elo, 0]. Never beneficial (cap at 0),
    # never worse than a zero-quality replacement (floor at elo).
    return max(min(raw, 0.0), elo_f)


# ── Phase 6 priority chain: override > auto_tier > DEFAULT_TIER ─────────

def classify_tier_with_overrides(
    player_name: str | None,
    team_name: str | None,
    player_stats_payload: dict | None = None,
    auto_tier_active: bool = False,
    index: dict | None = None,
) -> tuple[str, str, dict]:
    """Phase 6 priority chain — `override > auto_tier > DEFAULT_TIER`.

    Behaviour (per CORRECTIONS.md §7 / Phase 6 shadow rollout):

      * If the hand-curated whitelist resolves the player (full or
        unambiguous surname match), the override always wins. We still
        compute the auto-tier suggestion and surface it in `components`
        so the disagreement-diff CLI can show drift.
      * If `auto_tier_active` is True AND the override missed, fall
        through to `auto_classify(to_stats(payload))`. Source tags from
        auto_tier are returned verbatim ("auto_*").
      * If `auto_tier_active` is False (shadow mode — the rollout
        default), the override miss falls all the way through to
        DEFAULT_TIER, matching the pre-Phase-6 behaviour. The auto-tier
        suggestion is STILL computed and attached to `components` for
        diff visibility.
      * `player_stats_payload` is the per-team dict from
        `player_stats_2026.json` (i.e. fetch_player_stats's
        `snapshot["teams"][team]`). It may be None when stats are
        unavailable; auto_tier then degrades to `auto_no_data`.

    Returns (tier, source, components). `components` always contains:
        override_tier      : str | None  — what the hand-curated layer said
        override_source    : str         — whitelist_full / whitelist_last /
                                           whitelist_ambiguous / default
        auto_tier          : str | None  — what auto_classify said (or None
                                           if stats missing entirely)
        auto_source        : str | None
        auto_components    : dict        — minutes_share / ga90 / cs_share
        active             : bool        — was auto_tier_active true
    """
    # Import locally so the module stays importable when auto_tier.py is
    # absent (e.g. partial deploys / older test trees).
    try:
        from auto_tier import auto_classify  # type: ignore
        from fetch_player_stats import to_stats  # type: ignore
    except Exception:
        auto_classify = None  # type: ignore
        to_stats = None  # type: ignore

    override_tier, override_source, entry = _classify_tier_internal(
        player_name, team_name, index)
    override_resolved = entry is not None  # whitelist_full or whitelist_last

    auto_tier_value: str | None = None
    auto_source: str | None = None
    auto_components: dict = {}
    if auto_classify is not None and to_stats is not None:
        stats = to_stats(player_stats_payload or {}, player_name or "")
        a_tier, a_source, a_components = auto_classify(stats)
        auto_tier_value = a_tier
        auto_source = a_source
        auto_components = a_components

    components = {
        "override_tier": override_tier if override_resolved else None,
        "override_source": override_source,
        "auto_tier": auto_tier_value,
        "auto_source": auto_source,
        "auto_components": auto_components,
        "active": bool(auto_tier_active),
    }

    if override_resolved:
        return override_tier, override_source, components

    if auto_tier_active and auto_tier_value is not None:
        return auto_tier_value, auto_source or "auto_no_data", components

    return DEFAULT_TIER, "default", components
