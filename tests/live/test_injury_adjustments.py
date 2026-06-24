"""
Unit tests for B.3 — injury_adjustments pure helpers + fetch_injuries
record normalisation. No network calls.

Run:
    python3 tests/live/test_injury_adjustments.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

from injury_adjustments import (  # noqa: E402
    TIER_TO_ELO, DEFAULT_TIER, DOUBTFUL_DISCOUNT,
    classify_api_type, tier_elo, discounted_elo,
    classify_tier, classify_tier_with_replacement, net_injury_elo,
    normalize_player_name,
    _load_key_players_index, reset_key_players_index_for_tests,
)
import fetch_injuries  # noqa: E402


class TestTierTable(unittest.TestCase):
    def test_all_tiers_negative(self):
        for tier, val in TIER_TO_ELO.items():
            self.assertLess(val, 0.0, f"{tier} must be a penalty")

    def test_tier_ordering(self):
        # star ≥ keeper in magnitude > starter > squad
        self.assertLess(TIER_TO_ELO["tier_1_star"], TIER_TO_ELO["tier_1_keeper"])
        self.assertLess(TIER_TO_ELO["tier_1_keeper"], TIER_TO_ELO["tier_2_starter"])
        self.assertLess(TIER_TO_ELO["tier_2_starter"], TIER_TO_ELO["tier_3_squad"])

    def test_default_tier_is_starter(self):
        # Conservative default chosen so depth-chart noise doesn't move model.
        self.assertEqual(DEFAULT_TIER, "tier_2_starter")
        self.assertEqual(tier_elo(DEFAULT_TIER), -12.0)


class TestClassifyApiType(unittest.TestCase):
    def test_missing_fixture_is_out(self):
        self.assertEqual(classify_api_type("Missing Fixture"), "confirmed_out")

    def test_questionable_is_doubtful(self):
        self.assertEqual(classify_api_type("Questionable"), "doubtful")

    def test_suspended_treated_as_out(self):
        self.assertEqual(classify_api_type("Suspended"), "confirmed_out")

    def test_unknown_defaults_out(self):
        """Unknown types fail-closed to confirmed_out — safer than 0."""
        self.assertEqual(classify_api_type("Made-up-status"), "confirmed_out")
        self.assertEqual(classify_api_type(None), "confirmed_out")


class TestDiscountedElo(unittest.TestCase):
    def test_confirmed_full_penalty(self):
        self.assertEqual(discounted_elo("tier_2_starter", "confirmed_out"), -12.0)

    def test_doubtful_half_penalty(self):
        self.assertEqual(discounted_elo("tier_2_starter", "doubtful"),
                         -12.0 * DOUBTFUL_DISCOUNT)

    def test_unknown_status_zero(self):
        """Unknown statuses earn 0 — better to be quiet than leak Elo."""
        self.assertEqual(discounted_elo("tier_2_starter", "anything_else"), 0.0)


class TestNormaliseRecords(unittest.TestCase):
    """fetch_injuries.normalise_records turns raw API records into per-team totals."""

    def test_groups_players_per_team(self):
        records = [
            {"team": {"name": "France"},
             "player": {"name": "Player A", "type": "Missing Fixture",
                        "reason": "Knee"},
             "fixture": {"id": 1001}},
            {"team": {"name": "France"},
             "player": {"name": "Player B", "type": "Questionable",
                        "reason": "Ankle"},
             "fixture": {"id": 1001}},
            {"team": {"name": "Spain"},
             "player": {"name": "Player C", "type": "Missing Fixture",
                        "reason": "Hamstring"},
             "fixture": {"id": 1002}},
        ]
        teams, warnings = fetch_injuries.normalise_records(
            records, wc_teams={"France", "Spain"})
        self.assertEqual(set(teams.keys()), {"France", "Spain"})
        self.assertEqual(len(teams["France"]["players"]), 2)
        # France: -12 (confirmed_out) + -6 (doubtful) = -18
        self.assertAlmostEqual(teams["France"]["total_elo_adjustment"], -18.0)
        self.assertAlmostEqual(teams["Spain"]["total_elo_adjustment"], -12.0)
        self.assertEqual(warnings, [])

    def test_filters_non_wc_teams(self):
        records = [
            {"team": {"name": "France"},
             "player": {"name": "X", "type": "Missing Fixture"},
             "fixture": {"id": 1}},
            {"team": {"name": "Andorra"},  # not in WC
             "player": {"name": "Y", "type": "Missing Fixture"},
             "fixture": {"id": 2}},
        ]
        teams, warnings = fetch_injuries.normalise_records(
            records, wc_teams={"France"})
        self.assertEqual(set(teams.keys()), {"France"})
        # One warning about the filtered non-WC team
        types = {w["type"] for w in warnings}
        self.assertIn("filter_non_wc", types)

    def test_normalises_team_aliases(self):
        """API provider names map to canonical (e.g. Korea Republic → South Korea)."""
        records = [{
            "team": {"name": "Korea Republic"},
            "player": {"name": "X", "type": "Missing Fixture"},
            "fixture": {"id": 1},
        }]
        teams, _ = fetch_injuries.normalise_records(
            records, wc_teams={"South Korea"})
        self.assertIn("South Korea", teams)

    def test_skips_records_missing_team(self):
        records = [{"team": {}, "player": {"name": "X", "type": "Missing Fixture"}}]
        teams, warnings = fetch_injuries.normalise_records(
            records, wc_teams={"France"})
        self.assertEqual(teams, {})
        self.assertTrue(any(w["type"] == "skipped_bad_record" for w in warnings))

    def test_empty_records_clean_snapshot(self):
        teams, warnings = fetch_injuries.normalise_records([], wc_teams={"France"})
        self.assertEqual(teams, {})
        self.assertEqual(warnings, [])


class TestBuildSnapshot(unittest.TestCase):
    def test_snapshot_schema_keys(self):
        snap = fetch_injuries.build_snapshot([], [])
        for key in ("generated_at", "schema_version", "source",
                    "league_id", "season", "teams", "warnings",
                    "teams_with_injuries"):
            self.assertIn(key, snap)
        self.assertEqual(snap["source"], "api_football")
        self.assertEqual(snap["schema_version"], 1)


class TestNoRecordsReturnedSentinel(unittest.TestCase):
    """A successful API call that returns 0 records is an INFO event,
    not a silent success. Without a sentinel, an operator looking at
    `teams_with_injuries: 0, warnings: []` can't tell whether the feed
    is genuinely quiet or wedged on a misconfigured league_id/season.

    The sentinel propagates to apply_matchday_adjustments
    (via _PROPAGATE_WARNING_TYPES) so the dashboard's matchday-intel
    detail block can render it. It is INTENTIONALLY omitted from the
    dashboard's INTEL_TOP_BAR_TYPES allowlist so a quiet day doesn't
    trip a false alarm in the top pill."""

    def test_empty_response_emits_sentinel(self):
        """API responded 200 OK with `response: []` → emit
        no_records_returned with the actual endpoint coords so post-hoc
        forensics has the league/season actually queried."""
        from unittest.mock import patch
        with patch.object(fetch_injuries, "_http_get_json",
                          return_value={"response": [], "errors": {}}):
            records, warnings = fetch_injuries.fetch_apifootball_injuries(
                "fake-key")
        self.assertEqual(records, [])
        types = [w["type"] for w in warnings]
        self.assertIn("no_records_returned", types)
        sentinel = next(w for w in warnings
                        if w["type"] == "no_records_returned")
        # The warning carries the actual endpoint + league/season pair so
        # an operator inspecting the audit log can re-run the exact
        # query that produced 0 records.
        self.assertEqual(sentinel["endpoint"], "/injuries")
        self.assertIn("league", sentinel)
        self.assertIn("season", sentinel)

    def test_non_empty_response_no_sentinel(self):
        """A real injury record must NOT trigger the sentinel — only the
        empty case does."""
        from unittest.mock import patch
        payload = {"response": [
            {"team": {"name": "France"},
             "player": {"name": "K. Mbappe", "type": "Missing Fixture"},
             "fixture": {"id": 1}},
        ], "errors": {}}
        with patch.object(fetch_injuries, "_http_get_json",
                          return_value=payload):
            records, warnings = fetch_injuries.fetch_apifootball_injuries(
                "fake-key")
        self.assertEqual(len(records), 1)
        self.assertEqual(
            [w for w in warnings if w["type"] == "no_records_returned"],
            [])

    def test_http_error_does_not_double_emit_sentinel(self):
        """An http_error already explains the failure — the empty-records
        sentinel must NOT fire on top of it (would noise up the warnings
        list with a redundant cause). The fetch function returns the
        http_error warning and short-circuits before the empty-check."""
        from unittest.mock import patch
        import urllib.error
        err = urllib.error.HTTPError(
            url="x", code=503, msg="x", hdrs=None, fp=None)
        with patch.object(fetch_injuries, "_http_get_json", side_effect=err):
            records, warnings = fetch_injuries.fetch_apifootball_injuries(
                "fake-key")
        types = [w["type"] for w in warnings]
        self.assertIn("http_error", types)
        self.assertNotIn("no_records_returned", types)


# ── v2: classify_tier auto-upgrade against the curated whitelist ────────
class TestNormalizePlayerName(unittest.TestCase):
    def test_accents_stripped(self):
        self.assertEqual(normalize_player_name("Kylian Mbappé"), "kylian mbappe")
        self.assertEqual(normalize_player_name("Vinícius Júnior"),
                         "vinicius junior")

    def test_initials_collapsed(self):
        self.assertEqual(normalize_player_name("K. Mbappé"), "k mbappe")

    def test_whitespace_collapsed(self):
        self.assertEqual(normalize_player_name("  Harry   Kane  "),
                         "harry kane")

    def test_empty_and_none(self):
        self.assertEqual(normalize_player_name(None), "")
        self.assertEqual(normalize_player_name(""), "")
        self.assertEqual(normalize_player_name("   "), "")


class TestNormalizePlayerNameStrokeChars(unittest.TestCase):
    """REGRESSION: NFKD doesn't decompose stroke/ligature characters
    (Ø, Ł, Đ, Æ, Œ, ß, Þ, Ð). The naive .encode('ascii', 'ignore')
    pipeline silently drops them, so 'Ødegaard' became 'degaard' and
    routed Norway's tier_1_star to DEFAULT_TIER. The fix pre-translates
    these characters before NFKD."""

    def test_o_slash_nordic(self):
        """Ø ø — used in Norwegian, Danish names (Ødegaard, Møller, Højbjerg)."""
        self.assertEqual(normalize_player_name("Martin Ødegaard"),
                         "martin odegaard")
        self.assertEqual(normalize_player_name("Pierre-Emile Højbjerg"),
                         "pierre-emile hojbjerg")

    def test_l_slash_polish(self):
        """Ł ł — Polish L-slash (Łewandowski, Glik)."""
        self.assertEqual(normalize_player_name("Robert Łewandowski"),
                         "robert lewandowski")

    def test_d_with_stroke_croatian(self):
        """Đ đ — Croatian/Serbian (Đoković, but also keeper Đaković etc.)."""
        self.assertEqual(normalize_player_name("Mateja Đurđević"),
                         "mateja durdevic")

    def test_eth_icelandic(self):
        """Ð ð — Icelandic eth (Eiður Guðjohnsen-tier names)."""
        self.assertEqual(normalize_player_name("Eiður Guðjohnsen"),
                         "eidur gudjohnsen")

    def test_thorn_icelandic(self):
        """Þ þ — Icelandic thorn."""
        self.assertEqual(normalize_player_name("Þórður Þórðarson"),
                         "thordur thordarson")

    def test_ae_ligature(self):
        """Æ æ — Nordic ligature."""
        self.assertEqual(normalize_player_name("Æron Hawkins"),
                         "aeron hawkins")

    def test_oe_ligature(self):
        """Œ œ — French ligature."""
        self.assertEqual(normalize_player_name("Cœur de Lion"),
                         "coeur de lion")

    def test_german_sharp_s(self):
        """ß — German sharp s (Schweinsteiger spelled Schweinßteiger by some)."""
        self.assertEqual(normalize_player_name("Robert Schäßer"),
                         "robert schasser")

    def test_no_stroke_chars_passthrough(self):
        """ASCII-only inputs unaffected by the pre-translate step."""
        for plain in ("Harry Kane", "lionel messi", "Sadio Mané",
                      "Achraf Hakimi", "Vinícius Junior"):
            # Just verify no crash and idempotency under repeated normalization.
            once = normalize_player_name(plain)
            twice = normalize_player_name(once)
            self.assertEqual(once, twice,
                             f"{plain!r} not idempotent")

    def test_idempotent(self):
        """normalize(normalize(x)) == normalize(x) — required so the
        stored name_normalized in the whitelist is a fixed point."""
        for raw in ("Martin Ødegaard", "Robert Łewandowski",
                    "Kylian Mbappé", "Son Heung-min"):
            once = normalize_player_name(raw)
            twice = normalize_player_name(once)
            self.assertEqual(once, twice)


class TestClassifyTier(unittest.TestCase):
    """Auto-tier resolution: real data/raw/key_players_2026.json must
    promote consensus stars/keepers and leave random names at the
    conservative default."""

    @classmethod
    def setUpClass(cls):
        # Bust the process-level cache so this suite always reads fresh.
        reset_key_players_index_for_tests()

    def test_star_full_name_match(self):
        tier, src = classify_tier("Kylian Mbappé", "France")
        self.assertEqual(tier, "tier_1_star")
        self.assertEqual(src, "whitelist_full")

    def test_star_last_name_match(self):
        # API-Football often returns 'F. Lastname' style strings.
        tier, src = classify_tier("K. Mbappé", "France")
        self.assertEqual(tier, "tier_1_star")
        self.assertEqual(src, "whitelist_last")

    def test_keeper_full_name_match(self):
        tier, src = classify_tier("Jordan Pickford", "England")
        self.assertEqual(tier, "tier_1_keeper")
        self.assertEqual(src, "whitelist_full")

    def test_team_filter_blocks_cross_team_match(self):
        """A 'Mbappé' on Spain's roster must NOT auto-upgrade to tier_1_star
        — the whitelist entry is team-keyed."""
        tier, src = classify_tier("Mbappé", "Spain")
        self.assertEqual(tier, DEFAULT_TIER)
        self.assertEqual(src, "default")

    def test_unknown_player_default(self):
        tier, src = classify_tier("Some Squad Reserve", "France")
        self.assertEqual(tier, DEFAULT_TIER)
        self.assertEqual(src, "default")

    def test_missing_team_default(self):
        # Defensive: callers must pass a team. None/empty → default.
        self.assertEqual(classify_tier("Mbappé", None)[0], DEFAULT_TIER)
        self.assertEqual(classify_tier("Mbappé", "")[0], DEFAULT_TIER)

    def test_missing_name_default(self):
        self.assertEqual(classify_tier(None, "France")[0], DEFAULT_TIER)
        self.assertEqual(classify_tier("", "France")[0], DEFAULT_TIER)

    def test_team_with_no_entries_falls_through(self):
        """A WC team without any whitelist entries (e.g. small federations)
        returns DEFAULT_TIER for every injury — by design."""
        # New Zealand currently has zero entries in key_players_2026.json.
        tier, src = classify_tier("Some Star", "New Zealand")
        self.assertEqual(tier, DEFAULT_TIER)
        self.assertEqual(src, "default")

    def test_explicit_index_overrides_disk(self):
        """Tests can inject a custom index — keeps the helper unit-testable
        without depending on the on-disk file. by_last is a list-of-entries
        per surname so callers can model intra-team collisions."""
        custom = {
            "Wakanda": {
                "by_full": {"tchalla": {"tier": "tier_1_star",
                                        "name_normalized": "tchalla"}},
                "by_last": {"tchalla": [{"tier": "tier_1_star",
                                         "name_normalized": "tchalla"}]},
            }
        }
        tier, src = classify_tier("T'Challa", "Wakanda", index=custom)
        self.assertEqual(tier, "tier_1_star")
        self.assertEqual(src, "whitelist_full")


class TestKeyPlayersIndexLoader(unittest.TestCase):
    def test_real_file_loads_with_expected_teams(self):
        idx = _load_key_players_index()
        # Spot-check a few teams we know are seeded.
        for team in ("France", "Spain", "Argentina", "Brazil",
                     "England", "Portugal", "Germany"):
            self.assertIn(team, idx,
                          f"missing whitelist entries for {team!r}")
            self.assertTrue(idx[team]["by_full"],
                            f"{team!r} has empty by_full index")

    def test_missing_file_returns_empty_index(self):
        from pathlib import Path
        idx = _load_key_players_index(path=Path("/nonexistent/no.json"))
        self.assertEqual(idx, {})


class TestWhitelistSelfConsistency(unittest.TestCase):
    """Each stored `name_normalized` and `last_name_normalized` in the
    JSON must match what `normalize_player_name()` produces. If they
    drift, the index keys won't match what classify_tier computes for
    an incoming injury name → silent miscategorisation. This is the
    test that would have caught the Ø-drop bug at commit time."""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        ROOT_HERE = Path(__file__).resolve().parents[2]
        cls.raw = json.loads(
            (ROOT_HERE / "data" / "raw" / "key_players_2026.json").read_text())

    def test_stored_name_normalized_matches_function(self):
        mismatches = []
        for entry in self.raw["players"]:
            stored = entry.get("name_normalized")
            computed = normalize_player_name(entry.get("name"))
            if stored != computed:
                mismatches.append(
                    (entry["team"], entry["name"], stored, computed))
        self.assertFalse(
            mismatches,
            "Whitelist stored name_normalized drifted from "
            "normalize_player_name() output. First few mismatches:\n"
            + "\n".join(
                f"  {team} | {name!r} | stored={stored!r} | computed={computed!r}"
                for team, name, stored, computed in mismatches[:5]))

    def test_stored_last_name_normalized_is_a_window_of_full(self):
        """Stored last_name_normalized must appear as a trailing OR
        leading window of the full normalized name — anything else
        means the by_last index will never match what classify_tier
        computes.

        R14 MED: extended to accept leading windows too. Korean naming
        convention places the surname FIRST (Son Heung-min: surname
        'son', given name 'heung-min'). Pre-R14 the data carried Son's
        last_name_normalized as 'heung-min' (the trailing window) which
        the by_last index then keyed on the GIVEN NAME, defeating the
        team-aware canonical resolution for "Son" / "H. Son" aliases.
        R14 corrected the data to 'son' (the actual surname); this test
        now accepts both window directions to allow the correction.
        """
        problems = []
        for entry in self.raw["players"]:
            full = normalize_player_name(entry["name"])
            last = entry.get("last_name_normalized", "")
            if not last:
                continue
            tokens = full.split()
            trailing = {
                " ".join(tokens[-n:]) for n in range(1, len(tokens) + 1)
            }
            leading = {
                " ".join(tokens[:n]) for n in range(1, len(tokens) + 1)
            }
            valid_windows = trailing | leading
            if last not in valid_windows:
                problems.append((entry["team"], entry["name"], full, last))
        self.assertFalse(
            problems,
            "Stored last_name_normalized values don't appear as a "
            "trailing or leading window of the full normalized name:\n"
            + "\n".join(
                f"  {team} | {name!r} | full={full!r} | last={last!r}"
                for team, name, full, last in problems[:5]))

    def test_every_entry_has_required_fields(self):
        required = {"team", "name", "name_normalized",
                    "last_name_normalized", "tier"}
        for entry in self.raw["players"]:
            missing = required - set(entry.keys())
            self.assertFalse(missing,
                             f"Entry {entry.get('name')!r} missing keys: {missing}")

    def test_tier_values_are_valid(self):
        valid_tiers = {"tier_1_star", "tier_1_keeper",
                       "tier_2_starter", "tier_3_squad"}
        for entry in self.raw["players"]:
            self.assertIn(
                entry["tier"], valid_tiers,
                f"{entry.get('name')!r} has invalid tier {entry.get('tier')!r}")


class TestClassifyTierCompoundSurnames(unittest.TestCase):
    """REGRESSION: split()[-1] returns only the final token, but the
    by_last index stores compound surnames as multi-word keys ('de
    bruyne', 'van dijk', 'de jong'). The fix walks the trailing-window
    from 3 tokens down to 1."""

    @classmethod
    def setUpClass(cls):
        reset_key_players_index_for_tests()

    def test_de_bruyne_full(self):
        tier, src = classify_tier("Kevin De Bruyne", "Belgium")
        self.assertEqual((tier, src), ("tier_1_star", "whitelist_full"))

    def test_de_bruyne_initial_format(self):
        """API-Football short form 'K. De Bruyne' must hit the by_last
        index via the 2-token trailing window 'de bruyne'."""
        tier, src = classify_tier("K. De Bruyne", "Belgium")
        self.assertEqual((tier, src), ("tier_1_star", "whitelist_last"))

    def test_de_bruyne_bare_surname(self):
        """Just 'De Bruyne' should still match — 2-token window only."""
        tier, src = classify_tier("De Bruyne", "Belgium")
        self.assertEqual((tier, src), ("tier_1_star", "whitelist_last"))

    def test_van_dijk_variants(self):
        for inp in ("Virgil van Dijk", "V. van Dijk", "van Dijk"):
            tier, src = classify_tier(inp, "Netherlands")
            self.assertEqual(tier, "tier_1_star",
                             f"input {inp!r} routed to {tier}")
            self.assertIn(src, ("whitelist_full", "whitelist_last"))

    def test_de_jong_variants(self):
        for inp in ("Frenkie de Jong", "F. de Jong", "de Jong"):
            tier, src = classify_tier(inp, "Netherlands")
            self.assertEqual(tier, "tier_1_star",
                             f"input {inp!r} routed to {tier}")

    def test_compound_does_not_leak_to_other_team(self):
        """'De Bruyne' for a non-Belgium team must not auto-upgrade."""
        tier, src = classify_tier("De Bruyne", "Spain")
        self.assertEqual(tier, DEFAULT_TIER)
        self.assertEqual(src, "default")

    def test_window_capped_at_three(self):
        """A 5-token name should only try trailing windows of size 3/2/1.

        This protects against pathological back-matching where an entire
        bizarre name accidentally matches a short compound key. We
        verify by injecting a synthetic 4-token key (which must NEVER
        match) and a 2-token key (which must match cleanly when the
        input's forename prefix lines up with the candidate's)."""
        custom = {
            "TestTeam": {
                "by_full": {},
                "by_last": {
                    # 4-token surname key — should be UNREACHABLE because
                    # max_window=3. If the cap ever regressed and the
                    # 4-token window fired, this entry's tier_1_star
                    # would steal the result.
                    "abcd efgh ijkl mnop": [
                        {"tier": "tier_1_star",
                         "name_normalized": "qqq abcd efgh ijkl mnop"}
                    ],
                    # 2-token surname key with a forename portion so the
                    # post-HIGH#10 forename-prefix check has a candidate
                    # to compare against. Input's first forename 'xyzzy'
                    # matches this candidate's first forename, so the
                    # 2-token trailing window resolves cleanly.
                    "ijkl mnop": [
                        {"tier": "tier_1_keeper",
                         "name_normalized": "xyzzy ijkl mnop"}
                    ],
                }
            }
        }
        # 3-token window 'efgh ijkl mnop' → no match.
        # 2-token window 'ijkl mnop' → hits the tier_1_keeper entry.
        # 4-token candidate must NEVER be tried (it'd be tier_1_star).
        tier, src = classify_tier(
            "Xyzzy Abcd Efgh Ijkl Mnop", "TestTeam", index=custom)
        self.assertEqual(tier, "tier_1_keeper")
        self.assertEqual(src, "whitelist_last")


class TestClassifyTierEdgeCases(unittest.TestCase):
    """Defensive paths the production pipeline shouldn't crash on."""

    @classmethod
    def setUpClass(cls):
        reset_key_players_index_for_tests()

    def test_whitespace_only_name(self):
        self.assertEqual(classify_tier("   ", "France"),
                         (DEFAULT_TIER, "default"))

    def test_extremely_long_name_no_crash(self):
        very_long = " ".join(["foo"] * 50)
        tier, _ = classify_tier(very_long, "France")
        self.assertEqual(tier, DEFAULT_TIER)

    def test_punctuation_heavy_name(self):
        """API has been seen to return 'K. Mbappé!!' style noise — must
        not crash, and the meaningful tokens must still match."""
        tier, src = classify_tier("K. Mbappé!!", "France")
        # punctuation '!!' is preserved as part of token after normalize
        # (we only strip period/comma/apostrophe). The token 'mbappe!!'
        # won't hit by_last['mbappe'], which is acceptable — exotic
        # input falls through to default. We just verify no crash.
        self.assertIn(tier, ("tier_1_star", DEFAULT_TIER))
        self.assertIn(src, ("whitelist_last", "whitelist_full", "default"))

    def test_unicode_nfc_vs_nfd_input(self):
        """Same player name in NFC vs NFD pre-composed forms must
        normalise to the same key."""
        import unicodedata
        nfc = unicodedata.normalize("NFC", "Mbappé")  # combined é
        nfd = unicodedata.normalize("NFD", "Mbappé")  # decomposed e + ́
        self.assertEqual(
            normalize_player_name(nfc), normalize_player_name(nfd))

    def test_uppercase_input(self):
        self.assertEqual(
            normalize_player_name("KYLIAN MBAPPÉ"),
            normalize_player_name("Kylian Mbappé"))


class TestNormaliseRecordsUsesAutoTier(unittest.TestCase):
    """End-to-end: fetch_injuries.normalise_records must emit the
    auto-upgraded tier + the audit source field per player."""

    @classmethod
    def setUpClass(cls):
        reset_key_players_index_for_tests()

    def test_star_auto_upgraded(self):
        records = [{
            "team": {"name": "France"},
            "player": {"name": "Kylian Mbappé", "type": "Missing Fixture",
                       "reason": "Hamstring"},
            "fixture": {"id": 12345},
        }]
        teams, _ = fetch_injuries.normalise_records(
            records, wc_teams={"France"})
        p = teams["France"]["players"][0]
        self.assertEqual(p["tier"], "tier_1_star")
        self.assertEqual(p["auto_tier_source"], "whitelist_full")
        # tier_1_star confirmed_out → -30 Elo (full penalty)
        self.assertEqual(p["elo"], -30.0)
        # Team total reflects the upgraded penalty
        self.assertEqual(teams["France"]["total_elo_adjustment"], -30.0)

    def test_keeper_auto_upgraded(self):
        records = [{
            "team": {"name": "England"},
            "player": {"name": "J. Pickford", "type": "Missing Fixture"},
            "fixture": {"id": 12346},
        }]
        teams, _ = fetch_injuries.normalise_records(
            records, wc_teams={"England"})
        p = teams["England"]["players"][0]
        self.assertEqual(p["tier"], "tier_1_keeper")
        self.assertEqual(p["auto_tier_source"], "whitelist_last")
        # tier_1_keeper confirmed_out → -25 Elo
        self.assertEqual(p["elo"], -25.0)

    def test_unknown_player_defaults_to_starter(self):
        records = [{
            "team": {"name": "France"},
            "player": {"name": "Some Depth Player", "type": "Missing Fixture"},
            "fixture": {"id": 12347},
        }]
        teams, _ = fetch_injuries.normalise_records(
            records, wc_teams={"France"})
        p = teams["France"]["players"][0]
        self.assertEqual(p["tier"], DEFAULT_TIER)
        self.assertEqual(p["auto_tier_source"], "default")
        self.assertEqual(p["elo"], -12.0)  # tier_2_starter

    def test_doubtful_status_halves_upgraded_elo(self):
        """A tier_1_star marked Questionable should be 0.5× the full penalty."""
        records = [{
            "team": {"name": "France"},
            "player": {"name": "Kylian Mbappé", "type": "Questionable"},
            "fixture": {"id": 12348},
        }]
        teams, _ = fetch_injuries.normalise_records(
            records, wc_teams={"France"})
        p = teams["France"]["players"][0]
        self.assertEqual(p["tier"], "tier_1_star")
        # doubtful → -30 × 0.5 = -15
        self.assertEqual(p["elo"], -15.0)

    def test_regression_odegaard_stroke_char(self):
        """REGRESSION: 'Martin Ødegaard' previously normalised to
        'martin degaard' (NFKD drops the Ø codepoint), so the by_full
        index miss + by_last fallback on 'degaard' (also not in the
        index) silently routed Norway's tier_1_star to tier_2_starter
        (-18 Elo undercount). The fix pre-translates Nordic stroke
        characters before NFKD."""
        records = [{
            "team": {"name": "Norway"},
            "player": {"name": "Martin Ødegaard", "type": "Missing Fixture",
                       "reason": "Knee"},
            "fixture": {"id": 99001},
        }]
        teams, _ = fetch_injuries.normalise_records(
            records, wc_teams={"Norway"})
        p = teams["Norway"]["players"][0]
        self.assertEqual(p["tier"], "tier_1_star")
        self.assertEqual(p["auto_tier_source"], "whitelist_full")
        # tier_1_star confirmed_out → -30 Elo (full penalty), not -12
        self.assertEqual(p["elo"], -30.0)
        self.assertEqual(teams["Norway"]["total_elo_adjustment"], -30.0)

    def test_regression_de_bruyne_compound_short_form(self):
        """REGRESSION: 'K. De Bruyne' previously fell through to
        DEFAULT_TIER because split()[-1]='bruyne' isn't in the by_last
        index (the stored key is 'de bruyne'). The fix tries 3/2/1
        trailing-token windows."""
        records = [{
            "team": {"name": "Belgium"},
            "player": {"name": "K. De Bruyne", "type": "Missing Fixture",
                       "reason": "Hamstring"},
            "fixture": {"id": 99002},
        }]
        teams, _ = fetch_injuries.normalise_records(
            records, wc_teams={"Belgium"})
        p = teams["Belgium"]["players"][0]
        self.assertEqual(p["tier"], "tier_1_star")
        self.assertEqual(p["auto_tier_source"], "whitelist_last")
        self.assertEqual(p["elo"], -30.0)


class TestAutoTierSourceAuditValues(unittest.TestCase):
    """The auto_tier_source field is a controlled vocabulary used by
    downstream dashboards / audit logs. Any change to the set must be
    co-ordinated with consumers; this test pins the contract."""

    VALID_SOURCES = {"whitelist_full", "whitelist_last",
                     "whitelist_ambiguous", "default"}

    @classmethod
    def setUpClass(cls):
        reset_key_players_index_for_tests()

    def test_all_known_paths_emit_valid_source(self):
        cases = [
            # full match
            ("Kylian Mbappé", "France"),
            # last-1-token match
            ("Mbappé", "France"),
            # last-2-token match
            ("K. De Bruyne", "Belgium"),
            # collision-ambiguous (Argentina Martínez x2)
            ("Martinez", "Argentina"),
            # default fall-through
            ("Random Player", "France"),
            # team not in whitelist
            ("Random Player", "New Zealand"),
            # blank / missing
            ("", "France"),
            (None, "France"),
        ]
        for name, team in cases:
            _, src = classify_tier(name, team)
            self.assertIn(src, self.VALID_SOURCES,
                          f"unknown source {src!r} for input ({name!r}, {team!r})")


class TestMartinezCollision(unittest.TestCase):
    """REGRESSION (HIGH-1): Argentina's whitelist has two entries with
    last_name_normalized='martinez' — Lautaro (tier_1_star, -30) and
    Emiliano (tier_1_keeper, -25). The pre-fix `setdefault` made the
    first-loaded entry win, so 'E. Martinez' incorrectly inherited
    tier_1_star (+5 Elo undercount for Argentina, miscredited to wrong
    player). The fix stores by_last as list-of-entries and disambiguates
    at lookup time by forename prefix."""

    @classmethod
    def setUpClass(cls):
        reset_key_players_index_for_tests()

    def test_full_name_unambiguous_lautaro(self):
        tier, src = classify_tier("Lautaro Martínez", "Argentina")
        self.assertEqual((tier, src), ("tier_1_star", "whitelist_full"))

    def test_full_name_unambiguous_emiliano(self):
        tier, src = classify_tier("Emiliano Martínez", "Argentina")
        self.assertEqual((tier, src), ("tier_1_keeper", "whitelist_full"))

    def test_initial_l_resolves_to_lautaro(self):
        """API short-form 'L. Martinez' must resolve to Lautaro (star),
        not to whoever happened to load first."""
        tier, src = classify_tier("L. Martinez", "Argentina")
        self.assertEqual((tier, src), ("tier_1_star", "whitelist_last"))

    def test_initial_e_resolves_to_emiliano(self):
        """The pre-fix bug: 'E. Martinez' wrongly returned tier_1_star
        because Lautaro loaded first under by_last['martinez']. After the
        fix, the forename prefix 'e' matches 'emiliano' → keeper (-25)."""
        tier, src = classify_tier("E. Martinez", "Argentina")
        self.assertEqual((tier, src), ("tier_1_keeper", "whitelist_last"))

    def test_bare_surname_is_ambiguous(self):
        """Just 'Martinez' can't pick between two whitelist entries with
        that last name — fail safe to DEFAULT_TIER. We'd rather
        under-penalise than miscredit a tier_1_star to the wrong player."""
        tier, src = classify_tier("Martinez", "Argentina")
        self.assertEqual(tier, DEFAULT_TIER)
        self.assertEqual(src, "whitelist_ambiguous")

    def test_nickname_dibu_is_ambiguous(self):
        """'Dibu' is Emiliano's well-known nickname, but it isn't a
        prefix of 'emiliano' or 'lautaro' so the prefix disambiguator
        can't pick. Fail safe to DEFAULT_TIER + flag the ambiguity."""
        tier, src = classify_tier("Dibu Martinez", "Argentina")
        self.assertEqual(tier, DEFAULT_TIER)
        self.assertEqual(src, "whitelist_ambiguous")

    def test_cross_team_martinez_not_affected(self):
        """A 'Martinez' on Mexico (not in whitelist) must still default
        — the collision logic is scoped to the per-team bucket."""
        tier, src = classify_tier("L. Martinez", "Mexico")
        self.assertEqual(tier, DEFAULT_TIER)
        self.assertEqual(src, "default")

    def test_normalise_records_emits_correct_tier_for_emiliano(self):
        """End-to-end via fetch_injuries: an Emiliano Martínez injury
        record must emit -25 Elo (keeper), not -30 (star)."""
        records = [{
            "team": {"name": "Argentina"},
            "player": {"name": "E. Martinez", "type": "Missing Fixture",
                       "reason": "Back"},
            "fixture": {"id": 77001},
        }]
        teams, _ = fetch_injuries.normalise_records(
            records, wc_teams={"Argentina"})
        p = teams["Argentina"]["players"][0]
        self.assertEqual(p["tier"], "tier_1_keeper")
        self.assertEqual(p["elo"], -25.0)
        self.assertEqual(p["auto_tier_source"], "whitelist_last")

    def test_middle_initial_does_not_over_promote(self):
        """REGRESSION (latent HIGH): 'Carlos E. Martinez' must NOT silently
        inherit Emiliano's keeper tier just because 'e' is the second
        forename token and prefixes 'emiliano'. The disambiguator was
        previously matching ANY input forename token against ANY
        candidate forename token; the fix locks both sides to their
        FIRST forename only."""
        tier, src = classify_tier("Carlos E. Martinez", "Argentina")
        self.assertEqual(tier, DEFAULT_TIER)
        self.assertEqual(src, "whitelist_ambiguous")

    def test_middle_initial_l_does_not_over_promote(self):
        """Parallel regression: 'Carlos L. Martinez' must not silently
        inherit Lautaro's star tier via the middle initial."""
        tier, src = classify_tier("Carlos L. Martinez", "Argentina")
        self.assertEqual(tier, DEFAULT_TIER)
        self.assertEqual(src, "whitelist_ambiguous")

    def test_three_way_collision_same_initial_is_ambiguous(self):
        """Synthetic: if a third 'Lucas Martinez' joined the whitelist
        with Lautaro + Emiliano + Lucas all on Argentina, an input of
        bare 'L. Martinez' must NOT pick one — two candidates' first
        forename starts with 'l' → ambiguous fail-safe."""
        custom = {
            "Argentina": {
                "by_full": {
                    "lautaro martinez":  {"tier": "tier_1_star",
                                          "name_normalized": "lautaro martinez"},
                    "emiliano martinez": {"tier": "tier_1_keeper",
                                          "name_normalized": "emiliano martinez"},
                    "lucas martinez":    {"tier": "tier_1_star",
                                          "name_normalized": "lucas martinez"},
                },
                "by_last": {
                    "martinez": [
                        {"tier": "tier_1_star",   "name_normalized": "lautaro martinez"},
                        {"tier": "tier_1_keeper", "name_normalized": "emiliano martinez"},
                        {"tier": "tier_1_star",   "name_normalized": "lucas martinez"},
                    ]
                }
            }
        }
        tier, src = classify_tier("L. Martinez", "Argentina", index=custom)
        # Two candidates' forename starts with 'l' (Lautaro, Lucas) →
        # ambiguous. Must NOT silently pick the first one.
        self.assertEqual(tier, DEFAULT_TIER)
        self.assertEqual(src, "whitelist_ambiguous")
        # But the unique 'E. Martinez' still resolves cleanly.
        tier, src = classify_tier("E. Martinez", "Argentina", index=custom)
        self.assertEqual(tier, "tier_1_keeper")
        self.assertEqual(src, "whitelist_last")


class TestSingleTokenFalsePositives(unittest.TestCase):
    """REGRESSION (HIGH-2): Single-token whitelist entries (Pedri,
    Rodri, Rodrygo, Raphinha, Alisson — players known by one name)
    previously inserted themselves into by_last under the same key as
    by_full. Any teammate with that surname would then false-positive
    match — e.g. a fictional 'Carlos Pedri' on Spain would inherit
    Pedri's tier_1_star (-30) without justification. The fix excludes
    mononyms from by_last; they only match via exact by_full."""

    @classmethod
    def setUpClass(cls):
        reset_key_players_index_for_tests()

    def test_pedri_still_matches_via_full(self):
        """The legitimate entry must still resolve."""
        tier, src = classify_tier("Pedri", "Spain")
        self.assertEqual((tier, src), ("tier_1_star", "whitelist_full"))

    def test_mononym_does_not_match_via_compound_last(self):
        """Fictional 'Carlos Pedri' must NOT auto-upgrade — Pedri's
        mononym entry is excluded from by_last so the 1-token window
        search 'pedri' returns no hits."""
        tier, src = classify_tier("Carlos Pedri", "Spain")
        self.assertEqual(tier, DEFAULT_TIER)
        self.assertEqual(src, "default")

    def test_rodri_mononym_no_fp(self):
        # Real
        self.assertEqual(classify_tier("Rodri", "Spain")[0], "tier_1_star")
        # Fake — any 'X Rodri' must default
        tier, src = classify_tier("Bruno Rodri", "Spain")
        self.assertEqual(tier, DEFAULT_TIER)
        self.assertEqual(src, "default")

    def test_alisson_mononym_no_fp(self):
        # Real
        self.assertEqual(classify_tier("Alisson", "Brazil")[0], "tier_1_keeper")
        # Fake
        tier, src = classify_tier("Joao Alisson", "Brazil")
        self.assertEqual(tier, DEFAULT_TIER)
        self.assertEqual(src, "default")

    def test_rodrygo_mononym_no_fp(self):
        self.assertEqual(classify_tier("Rodrygo", "Brazil")[0], "tier_1_star")
        tier, src = classify_tier("Pedro Rodrygo", "Brazil")
        self.assertEqual(tier, DEFAULT_TIER)
        self.assertEqual(src, "default")

    def test_raphinha_mononym_no_fp(self):
        self.assertEqual(classify_tier("Raphinha", "Brazil")[0], "tier_1_star")
        tier, src = classify_tier("Lucas Raphinha", "Brazil")
        self.assertEqual(tier, DEFAULT_TIER)
        self.assertEqual(src, "default")

    def test_mononym_skipped_from_by_last_index(self):
        """Direct loader invariant: any whitelist entry whose
        last_name_normalized == name_normalized (full) MUST NOT appear
        in the by_last index — that is the fix's load-bearing guard."""
        idx = _load_key_players_index()
        spain = idx.get("Spain", {})
        # Pedri and Rodri are mononyms — must NOT be present in by_last.
        self.assertNotIn("pedri", spain.get("by_last", {}))
        self.assertNotIn("rodri", spain.get("by_last", {}))
        # But they MUST be present in by_full (the legitimate match path).
        self.assertIn("pedri", spain.get("by_full", {}))
        self.assertIn("rodri", spain.get("by_full", {}))


class TestTurkishCharacters(unittest.TestCase):
    """MEDIUM-1: Turkish dotted-I (İ U+0130) and dotless-i (ı U+0131) are
    self-contained codepoints. NFKD doesn't decompose them, so naïve
    ASCII-drop produced 'Çakır' → 'cakr' (drops ı) and 'İlkay' → 'lkay'
    (drops İ). The fix pre-translates both via _NORDIC_SLAVIC_MAP."""

    def test_dotless_i_preserved(self):
        # Çakır — common Turkish surname (Uğurcan Çakır, GK)
        # Ç → C via NFKD; ı → i via map; ğ → g via NFKD
        self.assertEqual(normalize_player_name("Uğurcan Çakır"),
                         "ugurcan cakir")

    def test_capital_dotted_i_preserved(self):
        # İlkay Gündoğan — Manchester City midfielder
        self.assertEqual(normalize_player_name("İlkay Gündoğan"),
                         "ilkay gundogan")

    def test_idempotent_turkish(self):
        """normalize(normalize(x)) == normalize(x) for Turkish inputs."""
        for raw in ("Uğurcan Çakır", "İlkay Gündoğan", "Hakan Çalhanoğlu"):
            once = normalize_player_name(raw)
            twice = normalize_player_name(once)
            self.assertEqual(once, twice, f"{raw!r} not idempotent")

    def test_arda_guler_still_works(self):
        """Sanity: Turkey's whitelisted star (Arda Güler) is unaffected
        — ü was already handled by NFKD. We just want to confirm the new
        map entries didn't break anything that was working."""
        tier, src = classify_tier("Arda Güler", "Turkey")
        self.assertEqual((tier, src), ("tier_1_star", "whitelist_full"))


class TestSingleEntryLeakClosure(unittest.TestCase):
    """REGRESSION (HIGH #10): the forename-prefix gate previously fired
    ONLY when by_last returned 2+ entries. Single-entry by_last hits
    (12 of 32 whitelisted teams: Algeria/Mahrez, Brazil/Vinícius,
    Egypt/Salah, Senegal/Mané, USA/Pulisic, Norway/Haaland,
    Croatia/Modric, Spain/Yamal, Switzerland/Xhaka, Sweden/Isak,
    Turkey/Güler, etc.) silently auto-promoted ANY input ending in that
    surname. The fix lifts the forename-prefix check OUT of the
    collision-only branch via the shared _resolve_from_last_match
    helper — runs even with a single match."""

    @classmethod
    def setUpClass(cls):
        reset_key_players_index_for_tests()

    def test_carlos_mbappe_not_promoted(self):
        """'Carlos Mbappe' on France must NOT inherit Mbappé's
        tier_1_star — 'kylian' doesn't start with 'carlos'."""
        tier, src = classify_tier("Carlos Mbappe", "France")
        self.assertEqual(tier, DEFAULT_TIER)
        self.assertEqual(src, "default")

    def test_john_haaland_not_promoted(self):
        tier, src = classify_tier("John Haaland", "Norway")
        self.assertEqual(tier, DEFAULT_TIER)
        self.assertEqual(src, "default")

    def test_random_kane_not_promoted(self):
        tier, src = classify_tier("Random Kane", "England")
        self.assertEqual(tier, DEFAULT_TIER)
        self.assertEqual(src, "default")

    def test_aaa_de_bruyne_not_promoted(self):
        """Compound surname leak: 'Aaa De Bruyne' must not inherit
        Kevin's tier_1_star via the by_last['de bruyne'] key."""
        tier, src = classify_tier("Aaa De Bruyne", "Belgium")
        self.assertEqual(tier, DEFAULT_TIER)
        self.assertEqual(src, "default")

    def test_bare_surname_still_resolves_single_entry(self):
        """'Mbappe' on France (no forename) should still hit — the team
        scope already guarantees this is the whitelisted player."""
        tier, src = classify_tier("Mbappe", "France")
        self.assertEqual(tier, "tier_1_star")
        self.assertEqual(src, "whitelist_last")

    def test_initial_format_still_resolves_single_entry(self):
        """'K. Mbappe' must continue to hit — 'kylian' startswith 'k'."""
        tier, src = classify_tier("K. Mbappe", "France")
        self.assertEqual(tier, "tier_1_star")
        self.assertEqual(src, "whitelist_last")

    def test_all_12_vulnerable_teams_pinned(self):
        """Brute-force sweep: for every single-entry team in the real
        whitelist, an obviously fake forename + the canonical surname
        must default. This is the long-tail regression net for HIGH #10
        — any future code change that re-opens the leak fails here for
        every vulnerable team at once."""
        vulnerable = [
            ("Algeria",       "Mahrez",      "Fake"),
            ("Austria",       "Alaba",       "Fake"),
            ("Brazil",        "Junior",      "Fake"),  # last_name = 'junior'
            ("Canada",        "Davies",      "Fake"),
            ("Egypt",         "Salah",       "Fake"),
            ("Saudi Arabia",  "Al-Dawsari",  "Fake"),
            ("Senegal",       "Mane",        "Fake"),
            ("South Korea",   "Heung-min",   "Fake"),
            ("Sweden",        "Isak",        "Fake"),
            ("Switzerland",   "Xhaka",       "Fake"),
            ("Turkey",        "Guler",       "Fake"),
            ("United States", "Pulisic",     "Fake"),
        ]
        leaks = []
        for team, surname, fake_first in vulnerable:
            tier, src = classify_tier(f"{fake_first} {surname}", team)
            if tier != DEFAULT_TIER or src != "default":
                leaks.append((team, surname, tier, src))
        self.assertFalse(
            leaks,
            "HIGH #10 leaks reopened for single-entry teams:\n"
            + "\n".join(f"  {t}/{s} → ({tier},{src})" for t, s, tier, src in leaks))


class TestSurnameFirstFallback(unittest.TestCase):
    """REGRESSION (HIGH #6): some providers emit surname-first
    ("MARTINEZ Emiliano", "VAN DIJK Virgil", "Pulisic, Christian"),
    which the trailing-window matcher could never resolve because the
    surname token sat at position 0, not -1. classify_tier now falls
    back to a LEADING-window search after the trailing windows exhaust,
    with the same forename-prefix guarantees applied — so surname-first
    fakes ("MBAPPE Carlos") still default."""

    @classmethod
    def setUpClass(cls):
        reset_key_players_index_for_tests()

    def test_uppercase_surname_first_emiliano(self):
        """'MARTINEZ Emiliano' (uppercase surname-first) routes to
        Emiliano keeper via leading window + forename disambiguation."""
        tier, src = classify_tier("MARTINEZ Emiliano", "Argentina")
        self.assertEqual((tier, src), ("tier_1_keeper", "whitelist_last"))

    def test_uppercase_surname_first_lautaro(self):
        tier, src = classify_tier("MARTINEZ Lautaro", "Argentina")
        self.assertEqual((tier, src), ("tier_1_star", "whitelist_last"))

    def test_comma_separated_surname_first(self):
        """Comma is replaced with whitespace during normalization, so
        'Martinez, Emiliano' becomes 'martinez emiliano' and routes
        through the same leading-window path."""
        tier, src = classify_tier("Martinez, Emiliano", "Argentina")
        self.assertEqual((tier, src), ("tier_1_keeper", "whitelist_last"))

    def test_compound_surname_first(self):
        """'VAN DIJK Virgil' — leading 2-token window 'van dijk'."""
        tier, src = classify_tier("VAN DIJK Virgil", "Netherlands")
        self.assertEqual((tier, src), ("tier_1_star", "whitelist_last"))

    def test_single_entry_surname_first(self):
        """'MBAPPE Kylian' — leading 1-token window 'mbappe'."""
        tier, src = classify_tier("MBAPPE Kylian", "France")
        self.assertEqual((tier, src), ("tier_1_star", "whitelist_last"))

    def test_surname_first_fake_does_not_leak(self):
        """'Mbappe Carlos' (surname-first FAKE) must NOT promote —
        leading window finds Mbappé but 'kylian' doesn't startswith
        'carlos', so the same HIGH #10 guard kicks in on this path."""
        tier, src = classify_tier("Mbappe Carlos", "France")
        self.assertEqual(tier, DEFAULT_TIER)
        self.assertEqual(src, "default")

    def test_surname_first_collision_is_ambiguous(self):
        """'Martinez Dibu' — leading 'martinez' hits both Lautaro AND
        Emiliano; 'dibu' isn't a prefix of either forename → ambiguous
        (same as the trailing-window Dibu case)."""
        tier, src = classify_tier("Martinez Dibu", "Argentina")
        self.assertEqual(tier, DEFAULT_TIER)
        self.assertEqual(src, "whitelist_ambiguous")

    def test_trailing_window_wins_when_both_could_fire(self):
        """A canonical 'Kylian Mbappé' input should resolve via trailing
        window (full match in by_full actually, but verifying the
        leading-window fallback doesn't pre-empt the standard path)."""
        tier, src = classify_tier("Kylian Mbappé", "France")
        self.assertEqual((tier, src), ("tier_1_star", "whitelist_full"))


class TestBidirectionalForenamePrefix(unittest.TestCase):
    """The forename-prefix disambiguator accepts a match in EITHER
    direction (input prefixes candidate OR candidate prefixes input).
    Covers benign spelling drift like 'Mohammed Salah' vs whitelist
    canonical 'Mohamed Salah' without per-player alias entries.

    The bidirectional rule is safe ONLY because the whitelist has zero
    intra-team forename prefix overlaps (verified at audit time —
    pre-flight will surface any future overlap that re-enables an
    ambiguity)."""

    @classmethod
    def setUpClass(cls):
        reset_key_players_index_for_tests()

    def test_mohammed_resolves_to_mohamed_salah(self):
        """API 'Mohammed Salah' (English) vs whitelist 'mohamed salah':
        'mohammed'.startswith('mohamed') is True (bidirectional)."""
        tier, src = classify_tier("Mohammed Salah", "Egypt")
        self.assertEqual(tier, "tier_1_star")
        # Both whitelist_full (alias) and whitelist_last (disambiguator)
        # are acceptable — they both reflect a correct resolution.
        self.assertIn(src, ("whitelist_full", "whitelist_last"))

    def test_random_other_spelling_still_defaults(self):
        """'Mohammado Salah' must NOT match: neither 'mohamed'.startswith
        ('mohammado') nor 'mohammado'.startswith('mohamed') is True
        (they diverge at character index 5: 'e' vs 'm')."""
        tier, src = classify_tier("Mohammado Salah", "Egypt")
        self.assertEqual(tier, DEFAULT_TIER)
        self.assertEqual(src, "default")

    def test_initial_form_still_resolves(self):
        """'M. Salah' continues to work — input 'm' is a prefix of
        candidate 'mohamed' (the existing direction)."""
        tier, src = classify_tier("M. Salah", "Egypt")
        self.assertEqual(tier, "tier_1_star")


class TestAliasesField(unittest.TestCase):
    """The optional `aliases` field adds full-form lookups for API
    name variants the canonical name_normalized can't capture (Korean
    name reorder, English spelling drift, common nicknames).

    Aliases register ONLY in by_full, so they cannot widen the surname
    leak surface — a stray 'Bob {Surname}' input still defaults via
    HIGH #10 unless 'Bob {Surname}' itself is whitelisted as an alias."""

    @classmethod
    def setUpClass(cls):
        reset_key_players_index_for_tests()

    def test_son_bare_resolves_via_alias(self):
        """Pre-alias bug: 'Son' bare defaulted because by_last stores
        'heung-min' (the Korean given name) instead of 'son'. Alias
        list now registers 'son' → Son Heung-min in by_full."""
        tier, src = classify_tier("Son", "South Korea")
        self.assertEqual((tier, src), ("tier_1_star", "whitelist_full"))

    def test_son_western_initial(self):
        """'H. Son' (Western initial-first format) resolves via alias."""
        tier, src = classify_tier("H. Son", "South Korea")
        self.assertEqual((tier, src), ("tier_1_star", "whitelist_full"))

    def test_son_reverse_order_alias(self):
        """'Heung-min Son' (forename-first Western reorder of the
        Korean canonical) resolves via alias."""
        tier, src = classify_tier("Heung-min Son", "South Korea")
        self.assertEqual((tier, src), ("tier_1_star", "whitelist_full"))

    def test_vini_jr_alias(self):
        """Common abbreviation 'Vini Jr' must resolve to Vinícius
        Júnior — pre-alias it defaulted because by_last stores 'junior'
        and 'jr' isn't in the by_last index."""
        tier, src = classify_tier("Vini Jr", "Brazil")
        self.assertEqual((tier, src), ("tier_1_star", "whitelist_full"))

    def test_vinicius_bare_alias(self):
        """'Vinicius' bare (no 'Jr' suffix) is common in some feeds."""
        tier, src = classify_tier("Vinicius", "Brazil")
        self.assertEqual((tier, src), ("tier_1_star", "whitelist_full"))

    def test_aliases_do_not_widen_surname_leak(self):
        """A stray 'Bob Son' must still default — aliases register in
        by_full only, so 'bob son' as a full-name lookup misses
        (no by_full['bob son']) and by_last['son'] doesn't exist
        either (aliases skip by_last)."""
        tier, src = classify_tier("Bob Son", "South Korea")
        self.assertEqual(tier, DEFAULT_TIER)
        self.assertEqual(src, "default")

    def test_aldawsari_alias_no_hyphen(self):
        """Saudi Arabia: API may emit 'Salem Aldawsari' (no hyphen).
        Canonical whitelist stores 'salem al-dawsari'."""
        tier, src = classify_tier("Salem Aldawsari", "Saudi Arabia")
        self.assertEqual((tier, src), ("tier_1_star", "whitelist_full"))

    def test_memo_ochoa_nickname_alias(self):
        """Mexico: 'Memo' is Guillermo's universal nickname for 20+
        years; broadcasters and feeds use it."""
        for inp in ("Memo Ochoa", "Memo"):
            tier, src = classify_tier(inp, "Mexico")
            self.assertEqual(tier, "tier_1_keeper",
                             f"input {inp!r} routed to {tier}")
            self.assertEqual(src, "whitelist_full")

    def test_bono_morocco_nickname_alias(self):
        """Morocco: 'Bono' is Yassine Bounou's WC22 hero moniker;
        broadcasters use it almost exclusively."""
        for inp in ("Bono", "Yassine Bono"):
            tier, src = classify_tier(inp, "Morocco")
            self.assertEqual(tier, "tier_1_keeper",
                             f"input {inp!r} routed to {tier}")
            self.assertEqual(src, "whitelist_full")

    def test_every_alias_resolves_to_owner_tier(self):
        """Property test: for every entry that declares aliases, each
        alias must classify back to the owner's declared tier. Pins
        the invariant that aliases stay in sync with name_normalized
        edits. This is the test that would catch a future curator
        renaming Mbappé's name_normalized to 'k mbappe' while leaving
        an alias 'kylian mbappe' pointing to a now-stale entry."""
        import json
        from pathlib import Path
        ROOT_HERE = Path(__file__).resolve().parents[2]
        raw = json.loads(
            (ROOT_HERE / "data" / "raw" / "key_players_2026.json").read_text())
        drift = []
        for entry in raw["players"]:
            aliases = entry.get("aliases") or []
            if not isinstance(aliases, list):
                continue
            for a in aliases:
                if not isinstance(a, str) or not a.strip():
                    continue
                tier, src = classify_tier(a, entry["team"])
                if tier != entry["tier"]:
                    drift.append((entry["team"], entry["name"], a, tier))
        self.assertFalse(
            drift,
            "Whitelist aliases drifted from their owner's tier. "
            "First few:\n" + "\n".join(
                f"  {team}/{name!r}: alias {a!r} → {tier}"
                for team, name, a, tier in drift[:5]))

    def test_aliases_loader_skips_non_list(self):
        """Loader treats a malformed aliases field (string instead of
        list) as 'no aliases' rather than crashing."""
        from injury_adjustments import _load_key_players_index
        import tempfile, json as jj
        from pathlib import Path
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            jj.dump({"players": [{
                "team": "TestTeam",
                "name": "Bad Aliases",
                "name_normalized": "bad aliases",
                "last_name_normalized": "aliases",
                "tier": "tier_1_star",
                "aliases": "not a list",
            }]}, f)
            tmp = Path(f.name)
        try:
            idx = _load_key_players_index(path=tmp)
            # Loader must have produced a bucket without crashing.
            self.assertIn("TestTeam", idx)
            # Canonical name still registered.
            self.assertIn("bad aliases", idx["TestTeam"]["by_full"])
        finally:
            tmp.unlink()


class TestAmbiguityWarningPropagation(unittest.TestCase):
    """When classify_tier returns 'whitelist_ambiguous', fetch_injuries
    must surface an aggregate warning so the operator can manually
    disambiguate via team_adjustments.json. Without this, an Emiliano
    Martínez injury reported as 'Dibu Martinez' defaults to -12 Elo
    instead of -25, and no signal is sent to the operator. The dashboard
    already renders matchday_intelligence.json[`warnings`] via
    renderMatchdayIntelligence; this test pins the contract that fetcher
    populates that path."""

    @classmethod
    def setUpClass(cls):
        reset_key_players_index_for_tests()

    def test_single_ambiguous_emits_aggregate_warning(self):
        records = [{
            "team": {"name": "Argentina"},
            "player": {"name": "Dibu Martinez", "type": "Missing Fixture"},
            "fixture": {"id": 88001},
        }]
        teams, warnings = fetch_injuries.normalise_records(
            records, wc_teams={"Argentina"})
        # Player still records the ambiguous source for the audit trail.
        self.assertEqual(teams["Argentina"]["players"][0]["auto_tier_source"],
                         "whitelist_ambiguous")
        # An aggregate warning of the right type is emitted.
        amb = [w for w in warnings if w["type"] == "ambiguous_classification"]
        self.assertEqual(len(amb), 1)
        self.assertEqual(amb[0]["count"], 1)
        # The original input is preserved so the operator can find it.
        self.assertEqual(amb[0]["cases"][0]["team"], "Argentina")
        self.assertEqual(amb[0]["cases"][0]["input"], "Dibu Martinez")
        # The dashboard-facing message names team and input.
        self.assertIn("Argentina", amb[0]["message"])
        self.assertIn("Dibu Martinez", amb[0]["message"])

    def test_multiple_ambiguous_aggregated_into_one(self):
        records = [
            {"team": {"name": "Argentina"},
             "player": {"name": "Dibu Martinez", "type": "Missing Fixture"},
             "fixture": {"id": 1}},
            {"team": {"name": "Argentina"},
             "player": {"name": "Martinez", "type": "Missing Fixture"},
             "fixture": {"id": 2}},
        ]
        _, warnings = fetch_injuries.normalise_records(
            records, wc_teams={"Argentina"})
        amb = [w for w in warnings if w["type"] == "ambiguous_classification"]
        # ONE aggregate warning carrying both cases — not two separate
        # alerts. Dashboard surfaces a single actionable item per snapshot.
        self.assertEqual(len(amb), 1)
        self.assertEqual(amb[0]["count"], 2)
        self.assertEqual(len(amb[0]["cases"]), 2)

    def test_no_ambiguous_no_warning(self):
        """Clean records (no collisions) must NOT emit the warning —
        otherwise the dashboard alert pill turns on every cycle."""
        records = [
            {"team": {"name": "France"},
             "player": {"name": "Kylian Mbappé", "type": "Missing Fixture"},
             "fixture": {"id": 1}},
        ]
        _, warnings = fetch_injuries.normalise_records(
            records, wc_teams={"France"})
        amb = [w for w in warnings if w["type"] == "ambiguous_classification"]
        self.assertEqual(amb, [])

    def test_unambiguous_collision_resolves_no_warning(self):
        """A collision INPUT that the disambiguator resolves cleanly
        (e.g. 'E. Martinez') must NOT trigger the warning — the
        forename prefix uniquely picks Emiliano."""
        records = [
            {"team": {"name": "Argentina"},
             "player": {"name": "E. Martinez", "type": "Missing Fixture"},
             "fixture": {"id": 1}},
            {"team": {"name": "Argentina"},
             "player": {"name": "L. Martinez", "type": "Missing Fixture"},
             "fixture": {"id": 2}},
        ]
        _, warnings = fetch_injuries.normalise_records(
            records, wc_teams={"Argentina"})
        amb = [w for w in warnings if w["type"] == "ambiguous_classification"]
        self.assertEqual(amb, [])


class TestNetInjuryEloPhase1B(unittest.TestCase):
    """Phase 1B (CORRECTIONS.md §1) — replacement-aware net Elo.

    The fetch layer must surface `replacement_elo` and `net_elo` per
    player, and `net_elo_active: true` at top level. The raw `elo` field
    stays unchanged so existing readers keep working.
    """

    @classmethod
    def setUpClass(cls):
        reset_key_players_index_for_tests()

    def test_net_injury_elo_math(self):
        # elo = -30 (tier_1_star), replacement = -9.6 (tier_2_starter * 0.8)
        # → net = -30 - (-9.6) = -20.4
        self.assertAlmostEqual(net_injury_elo(-30.0, -9.6), -20.4, places=6)

    def test_net_injury_elo_no_replacement_falls_back_to_elo(self):
        self.assertEqual(net_injury_elo(-12.0, None), -12.0)

    def test_classify_with_replacement_returns_elo_equiv(self):
        custom = {
            "Wakanda": {
                "by_full": {"tchalla": {
                    "tier": "tier_1_star",
                    "name_normalized": "tchalla",
                    "replacement": {"name": "M'Baku",
                                    "tier": "tier_2_starter",
                                    "elo_equiv": -9.6},
                }},
                "by_last": {},
            }
        }
        tier, src, repl = classify_tier_with_replacement(
            "T'Challa", "Wakanda", index=custom)
        self.assertEqual(tier, "tier_1_star")
        self.assertEqual(src, "whitelist_full")
        self.assertEqual(repl, -9.6)

    def test_classify_with_replacement_missing_block_returns_none(self):
        custom = {
            "Wakanda": {
                "by_full": {"tchalla": {"tier": "tier_1_star",
                                        "name_normalized": "tchalla"}},
                "by_last": {},
            }
        }
        tier, src, repl = classify_tier_with_replacement(
            "T'Challa", "Wakanda", index=custom)
        self.assertEqual(tier, "tier_1_star")
        self.assertIsNone(repl)

    def test_classify_with_replacement_unknown_player_returns_none(self):
        tier, src, repl = classify_tier_with_replacement(
            "Some Random Reserve", "France")
        self.assertEqual(tier, DEFAULT_TIER)
        self.assertEqual(src, "default")
        self.assertIsNone(repl)

    def test_fetch_injuries_emits_replacement_and_net_elo(self):
        records = [{
            "team": {"name": "France"},
            "player": {"name": "Kylian Mbappé", "type": "Missing Fixture"},
            "fixture": {"id": 1},
        }]
        teams, _ = fetch_injuries.normalise_records(
            records, wc_teams={"France"})
        self.assertIn("France", teams)
        player = teams["France"]["players"][0]
        self.assertIn("replacement_elo", player)
        self.assertIn("net_elo", player)
        self.assertEqual(player["elo"], -30.0)
        # Mbappé's whitelist entry must have a replacement with elo_equiv
        # for this test to be meaningful — if the data file ever loses it,
        # this assertion will surface the regression immediately.
        if player["replacement_elo"] is not None:
            expected_net = round(
                player["elo"] - player["replacement_elo"], 3)
            self.assertEqual(player["net_elo"], expected_net)
            self.assertEqual(
                teams["France"]["total_net_elo_adjustment"], expected_net)
        else:
            self.assertEqual(player["net_elo"], player["elo"])

    def test_fetch_injuries_unknown_player_net_equals_elo(self):
        records = [{
            "team": {"name": "France"},
            "player": {"name": "Some Reserve", "type": "Missing Fixture"},
            "fixture": {"id": 1},
        }]
        teams, _ = fetch_injuries.normalise_records(
            records, wc_teams={"France"})
        player = teams["France"]["players"][0]
        self.assertIsNone(player["replacement_elo"])
        self.assertEqual(player["net_elo"], player["elo"])

    def test_snapshot_carries_net_elo_active_flag(self):
        snap = fetch_injuries.build_snapshot([], fetch_warnings=[])
        self.assertTrue(snap.get("net_elo_active"))


class TestNetInjuryEloClamp(unittest.TestCase):
    """S5 fix — net_injury_elo() must clamp the result into [elo, 0].

    Two failure modes the clamp catches:
      1. replacement.elo_equiv MORE negative than elo (data slip in
         key_players_2026.json): raw `elo - replacement_elo` flips
         POSITIVE — an injury that improves the team. Cap at 0.
      2. replacement.elo_equiv ABOVE 0 (positive — replacement is "better
         than baseline"): raw `elo - replacement_elo` becomes more negative
         than `elo`. Floor at `elo` so we never penalise harder than the
         zero-quality-replacement case.

    Also re-asserts the Round-4 finite-guard so a future refactor doesn't
    silently drop NaN/inf handling.
    """

    def test_clamp_caps_positive_at_zero(self):
        # tier_3_squad-style elo (-30) but a worse-than-baseline replacement
        # at -50 would emit raw = -30 - (-50) = +20. Clamp must cap at 0.
        self.assertEqual(net_injury_elo(-30.0, -50.0), 0.0)

    def test_clamp_floors_at_elo(self):
        # Positive replacement_elo (e.g. a typo'd entry where replacement
        # was supposed to be a penalty but got a sign flip) would emit
        # raw = -30 - (+20) = -50, deeper than elo. Floor must hold at elo.
        self.assertEqual(net_injury_elo(-30.0, 20.0), -30.0)

    def test_clamp_preserves_in_range(self):
        # Healthy case: elo=-30, replacement=-10, raw=-20 ∈ [-30, 0] →
        # pass through unchanged.
        self.assertAlmostEqual(net_injury_elo(-30.0, -10.0), -20.0, places=6)

    def test_clamp_propagates_finite_guard(self):
        # NaN/inf must still raise ValueError (Round-4 hardening preserved).
        with self.assertRaises(ValueError):
            net_injury_elo(float("nan"), -9.6)
        with self.assertRaises(ValueError):
            net_injury_elo(-30.0, float("nan"))
        with self.assertRaises(ValueError):
            net_injury_elo(float("inf"), -9.6)
        with self.assertRaises(ValueError):
            net_injury_elo(-30.0, float("-inf"))


def _summary(result):
    print()
    print(f"  Ran {result.testsRun} tests")
    if result.wasSuccessful():
        print("  ✓ all passed")
    else:
        print(f"  ✗ {len(result.failures)} failures, {len(result.errors)} errors")


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    _summary(result)
    sys.exit(0 if result.wasSuccessful() else 1)
