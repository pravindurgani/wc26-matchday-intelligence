"""
A.7 regression — name-first knockout mapping + fetch-time map self-heal.

Grounds: the 2026-07-03 16:31Z incident. At 16:22Z fetch_results' A.6
auto-extension mapped all 16 R32 provider fixtures correctly BY NAME and 13
results were ingested. At 16:31Z the workflow's bootstrap-rebuild trigger
fired (6 R16 fixtures could not map yet) and ran
build_provider_fixture_map.py --write, whose knockout pairing was POSITIONAL
(round + chronological order). FIFA match numbering within a date is NOT
UTC-kickoff order (venue-local kickoff order ≠ UTC order), so the rebuild
re-keyed the already-correct R32 entries WRONG — m74/75/76 rotated, m77/78,
m81/82, m83/84 swapped (plus the m86/87/88 rotation and an m89/90 R16 swap)
— and the next fetch ingested real scores under the wrong internal ids.
03_simulate's decide_knockout name-verification guard then correctly raised
on every tick (locked pairing ≠ sim bracket resolution) and the circuit
breaker started climbing.

This suite pins the two-part fix:
  * BUILDER (build_provider_fixture_map.assign_knockout_ids): knockout
    fixtures with real team names are assigned NAME-FIRST with the same
    criteria the A.6 extension uses (stage + unordered normalized pair +
    date±1 against the resolver-annotated bracket). Positional pairing is
    reserved for genuinely-TBD fixtures in unambiguous singleton buckets;
    ambiguous buckets are refused. Existing entries are merged, never
    blind-overwritten by positional guesses.
  * FETCH SELF-HEAL (fetch_results.heal_mismapped_knockouts): already-
    mapped KO rows are verified BY NAME before ingestion; mis-keyed entries
    are re-keyed via the extension's matcher, persisted atomically, and a
    typed {"type": "mapped_fixture_rekeyed"} warning lists old→new ids.
    Un-re-keyable rows are dropped into the existing critical unmapped
    warning. Stage gating stops a suspect stage's poisoned resolution from
    re-keying later stages within the same tick.

Run:
    python3 tests/live/test_ko_map_name_first.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "live"))

import build_provider_fixture_map as builder  # noqa: E402

_MODULE_SEQ = 0


def _load_fetch_results():
    """Fresh fetch_results instance per test — monkeypatching LIVE /
    http_get_json / _resolved_bracket_rows can't leak across tests."""
    global _MODULE_SEQ
    _MODULE_SEQ += 1
    spec = importlib.util.spec_from_file_location(
        f"fetch_results_namefirst_{_MODULE_SEQ}",
        ROOT / "scripts" / "live" / "fetch_results.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ─── Synthetic bracket helpers (pure-function builder tests) ────────────────
def _row(m, date, stage, home=None, away=None):
    """A fetch_results._resolved_bracket_rows()-shaped row."""
    return {"m": m, "date": date, "stage": stage,
            "home": "1A", "away": "2B",  # slot codes — opaque to the mapper
            "resolved_home": home, "resolved_away": away}


# The REAL R32 truth of the incident (resolved pairings + FIFA dates).
_INCIDENT_R32 = [
    (73, "2026-06-28", "South Africa", "Canada"),
    (74, "2026-06-29", "Germany", "Paraguay"),
    (75, "2026-06-29", "Netherlands", "Morocco"),
    (76, "2026-06-29", "Brazil", "Japan"),
    (77, "2026-06-30", "France", "Sweden"),
    (78, "2026-06-30", "Ivory Coast", "Norway"),
    (79, "2026-06-30", "Mexico", "Ecuador"),
    (80, "2026-07-01", "England", "DR Congo"),
    (81, "2026-07-01", "United States", "Bosnia and Herzegovina"),
    (82, "2026-07-01", "Belgium", "Senegal"),
    (83, "2026-07-02", "Portugal", "Croatia"),
    (84, "2026-07-02", "Spain", "Austria"),
    (85, "2026-07-02", "Switzerland", "Algeria"),
    (86, "2026-07-03", "Argentina", "Cape Verde"),
    (87, "2026-07-03", "Colombia", "Ghana"),
    (88, "2026-07-03", "Australia", "Egypt"),
]

# The provider fixture ids the incident actually involved (true internal id
# → provider fixture id, from the 16:22Z name-based extension).
_INCIDENT_PFID = {
    73: "1561329", 74: "1565176", 75: "1562345", 76: "1562344",
    77: "1565177", 78: "1564789", 79: "1567306", 80: "1567307",
    81: "1562586", 82: "1567308", 83: "1567309", 84: "1567311",
    85: "1567312", 86: "1565179", 87: "1567310", 88: "1565178",
}

# What the 16:31Z positional rebuild wrote instead (provider fixture id →
# WRONG internal id), verbatim from the ec883c47 commit.
_INCIDENT_BAD_KEYING = {
    "1561329": 73, "1562344": 74, "1565176": 75, "1562345": 76,
    "1564789": 77, "1565177": 78, "1567306": 79, "1567307": 80,
    "1567308": 81, "1562586": 82, "1567311": 83, "1567309": 84,
    "1567312": 85, "1565178": 86, "1565179": 87, "1567310": 88,
}


def _incident_bracket_rows():
    return [_row(m, d, "r32", h, a) for m, d, h, a in _INCIDENT_R32]


def _incident_team_set():
    teams = set()
    for _, _, h, a in _INCIDENT_R32:
        teams.update((h, a))
    return teams


def _incident_provider_fixtures():
    """The 16 R32 provider fixtures in UTC-CHRONOLOGICAL order, which
    within several dates differs from FIFA m-number order (m76 kicked off
    before m74 on 06-29; m78 before m77; m84 before m83; m88 before m86...)
    and includes UTC next-day rollovers for late venue-local kickoffs.
    This is exactly the ordering that scrambled the positional builder."""
    utc_order = [  # (true_m, utc_date, home_raw, away_raw)
        (73, "2026-06-28", "South Africa", "Canada"),
        (76, "2026-06-29", "Brazil", "Japan"),          # 12:00 local
        (74, "2026-06-29", "Germany", "Paraguay"),      # 16:30 local
        (75, "2026-06-30", "Netherlands", "Morocco"),   # 19:00 local → UTC rollover
        (78, "2026-06-30", "Ivory Coast", "Norway"),    # 12:00 local
        (77, "2026-06-30", "France", "Sweden"),         # 17:00 local
        (79, "2026-07-01", "Mexico", "Ecuador"),        # 19:00 local → rollover
        (80, "2026-07-01", "England", "DR Congo"),
        (82, "2026-07-01", "Belgium", "Senegal"),
        (81, "2026-07-02", "United States",
         "Bosnia & Herzegovina"),                       # rollover + alias form
        (84, "2026-07-02", "Spain", "Austria"),
        (83, "2026-07-02", "Portugal", "Croatia"),
        (85, "2026-07-03", "Switzerland", "Algeria"),   # rollover
        (88, "2026-07-03", "Australia", "Egypt"),       # 13:00 local
        (86, "2026-07-03", "Argentina",
         "Cape Verde Islands"),                         # alias form
        (87, "2026-07-04", "Colombia", "Ghana"),        # 20:30 local → rollover
    ]
    return [{"id": _INCIDENT_PFID[m], "date": d, "home_raw": h,
             "away_raw": a, "round": "Round of 32"}
            for m, d, h, a in utc_order]


class TestBuilderNameFirst(unittest.TestCase):
    """assign_knockout_ids — the builder half of the A.7 fix."""

    def test_incident_regression_utc_order_keys_by_name(self):
        """THE regression: provider R32 fixtures arriving in UTC-chronological
        order that differs from FIFA numbering within a date must key by
        NAME. The exact 16:31Z scramble (m74←Brazil-Japan etc.) must be
        impossible."""
        mapped, unmapped = builder.assign_knockout_ids(
            _incident_provider_fixtures(), _incident_bracket_rows(),
            _incident_team_set())
        got = {x["provider_fixture_id"]: x["match_id"] for x in mapped}
        expect = {pfid: m for m, pfid in _INCIDENT_PFID.items()}
        self.assertEqual(got, expect)
        self.assertEqual(unmapped, [])
        self.assertTrue(all(x["matched_by"] == "name" for x in mapped))
        # Name the exact incident scrambles so a regression reads loudly:
        self.assertEqual(got["1562344"], 76,
                         "Brazil-Japan must key to m76 — the positional "
                         "builder wrote it to m74 (16:31Z incident)")
        self.assertEqual(got["1565176"], 74, "Germany-Paraguay is m74")
        self.assertEqual(got["1562345"], 75, "Netherlands-Morocco is m75")
        self.assertEqual(got["1565177"], 77, "France-Sweden is m77 (not 78)")
        self.assertEqual(got["1567311"], 84, "Spain-Austria is m84 (not 83)")
        self.assertEqual(got["1565178"], 88, "Australia-Egypt is m88 (not 86)")
        # And none of the bad keyings can recur:
        for pfid, bad_m in _INCIDENT_BAD_KEYING.items():
            if expect[pfid] != bad_m:
                self.assertNotEqual(got[pfid], bad_m,
                                    f"pfid {pfid} re-acquired its 16:31Z "
                                    f"mis-key m{bad_m}")

    def test_real_named_fixture_refused_when_bracket_unresolved(self):
        """Real names + unresolved bracket slots → REFUSE (unmapped), never
        positional. This is the 16:31Z R16 situation: 6 real-named provider
        fixtures, our slots not yet resolvable — the old builder positionally
        mis-keyed them."""
        rows = [_row(89, "2026-07-04", "r16"), _row(90, "2026-07-04", "r16")]
        fixtures = [
            {"id": "1567824", "date": "2026-07-04", "home_raw": "Canada",
             "away_raw": "Morocco", "round": "Round of 16"},
            {"id": "1569870", "date": "2026-07-04", "home_raw": "Paraguay",
             "away_raw": "France", "round": "Round of 16"},
        ]
        teams = {"Canada", "Morocco", "Paraguay", "France"}
        mapped, unmapped = builder.assign_knockout_ids(fixtures, rows, teams)
        self.assertEqual(mapped, [],
                         "real-named fixtures must NEVER be positionally "
                         "guessed — that is the incident")
        self.assertEqual(len(unmapped), 2)
        for u in unmapped:
            self.assertIn("name_unplaceable", u["reason"])

    def test_tbd_fixture_maps_positionally_when_unambiguous(self):
        """Genuinely-TBD fixtures still map positionally when the (stage,
        date-window) bucket is an unambiguous singleton (final, 3rd place)."""
        rows = [_row(103, "2026-07-18", "3rd"), _row(104, "2026-07-19", "final")]
        fixtures = [
            {"id": "F104", "date": "2026-07-19", "home_raw": "Winner SF1",
             "away_raw": "Winner SF2", "round": "Final"},
            {"id": "F103", "date": "2026-07-18", "home_raw": "Loser SF1",
             "away_raw": "Loser SF2", "round": "3rd Place Final"},
        ]
        mapped, unmapped = builder.assign_knockout_ids(
            fixtures, rows, _incident_team_set())
        got = {x["provider_fixture_id"]: (x["match_id"], x["matched_by"])
               for x in mapped}
        self.assertEqual(got, {"F104": (104, "position"),
                               "F103": (103, "position")})
        self.assertEqual(unmapped, [])

    def test_tbd_ambiguous_bucket_refused_no_guess(self):
        """Multiple TBD fixtures inside one (stage, date-window) bucket —
        the pre-A.7 builder guessed by kickoff order; now it must refuse."""
        rows = [_row(74, "2026-06-29", "r32"), _row(75, "2026-06-29", "r32"),
                _row(76, "2026-06-29", "r32")]
        fixtures = [
            {"id": f"T{i}", "date": "2026-06-29", "home_raw": f"Winner Group {c}",
             "away_raw": f"Runner Up Group {c2}", "round": "Round of 32"}
            for i, (c, c2) in enumerate([("A", "B"), ("C", "D"), ("E", "F")])
        ]
        mapped, unmapped = builder.assign_knockout_ids(
            fixtures, rows, _incident_team_set())
        self.assertEqual(mapped, [], "ambiguous TBD bucket must not be guessed")
        self.assertEqual(len(unmapped), 3)
        for u in unmapped:
            self.assertIn("ambiguous_positional_bucket", u["reason"])
            # Operator debuggability: refusals must carry the provider names.
            self.assertTrue(u["raw_home"] and u["raw_away"],
                            f"unmapped entry lost its provider names: {u}")

    def test_positional_never_claims_a_name_claimed_id(self):
        """A TBD fixture whose only candidate id was claimed by a name match
        must be refused, not double-assigned."""
        rows = [_row(104, "2026-07-19", "final", "Spain", "Argentina")]
        fixtures = [
            {"id": "REAL", "date": "2026-07-19", "home_raw": "Spain",
             "away_raw": "Argentina", "round": "Final"},
            {"id": "TBD1", "date": "2026-07-19", "home_raw": "TBD",
             "away_raw": "TBD", "round": "Final"},
        ]
        mapped, unmapped = builder.assign_knockout_ids(
            fixtures, rows, {"Spain", "Argentina"})
        got = {x["provider_fixture_id"]: x["match_id"] for x in mapped}
        self.assertEqual(got, {"REAL": 104})
        self.assertEqual([u["provider_fixture_id"] for u in unmapped], ["TBD1"])

    def test_merge_name_evidence_supersedes_stale_positional_entry(self):
        """An existing (wrong, positional-era) entry must be overridden by
        fresh name evidence — and the freed id re-claimed by ITS rightful
        fixture. This is the committed ec883c47 map being rebuilt."""
        rows = _incident_bracket_rows()
        existing = [
            {"match_id": 74, "provider_fixture_id": "1562344",
             "home": "Brazil", "away": "Japan", "date": "2026-06-29",
             "phase": "r32"},   # WRONG — Brazil-Japan is m76
            {"match_id": 76, "provider_fixture_id": "1562345",
             "home": "Netherlands", "away": "Morocco", "date": "2026-06-29",
             "phase": "r32"},   # WRONG — Netherlands-Morocco is m75
        ]
        mapped, unmapped = builder.assign_knockout_ids(
            _incident_provider_fixtures(), rows, _incident_team_set(),
            existing_entries=existing)
        got = {x["provider_fixture_id"]: x["match_id"] for x in mapped}
        self.assertEqual(got["1562344"], 76)
        self.assertEqual(got["1562345"], 75)
        self.assertEqual(got["1565176"], 74)
        self.assertEqual(unmapped, [])

    def test_merge_preserves_existing_entry_absent_from_payload(self):
        """An existing entry whose provider fixture is missing from this
        payload must be preserved (merge, don't blind-overwrite)."""
        rows = _incident_bracket_rows()
        fixtures = [f for f in _incident_provider_fixtures()
                    if f["id"] != "1561329"]  # m73's fixture absent this pull
        existing = [{"match_id": 73, "provider_fixture_id": "1561329",
                     "home": "South Africa", "away": "Canada",
                     "date": "2026-06-28", "phase": "r32",
                     "matched_by": "name"}]
        mapped, unmapped = builder.assign_knockout_ids(
            fixtures, rows, _incident_team_set(), existing_entries=existing)
        got = {x["provider_fixture_id"]: (x["match_id"], x["matched_by"])
               for x in mapped}
        self.assertEqual(got["1561329"], (73, "name"))
        self.assertEqual(len(got), 16)
        self.assertEqual(unmapped, [])

    def test_merge_preserves_entry_when_payload_reverts_to_tbd(self):
        """If the provider temporarily reverts a mapped fixture's names to
        placeholders, the existing entry survives and the TBD row is not
        re-guessed elsewhere."""
        rows = _incident_bracket_rows()
        fixtures = [{"id": "1561329", "date": "2026-06-28",
                     "home_raw": "Winner Group A", "away_raw": "Match 40 Winner",
                     "round": "Round of 32"}]
        existing = [{"match_id": 73, "provider_fixture_id": "1561329",
                     "home": "South Africa", "away": "Canada",
                     "date": "2026-06-28", "phase": "r32"}]
        mapped, unmapped = builder.assign_knockout_ids(
            fixtures, rows, _incident_team_set(), existing_entries=existing)
        got = {x["provider_fixture_id"]: (x["match_id"], x["matched_by"])
               for x in mapped}
        self.assertEqual(got, {"1561329": (73, "preserved")})
        self.assertEqual(unmapped, [])

    def test_merge_drops_existing_entry_refuted_by_name_evidence(self):
        """An existing entry whose payload names are real but unplaceable
        (pairing exists nowhere in our bracket) must be dropped, not
        preserved as authoritative."""
        rows = _incident_bracket_rows()
        fixtures = [{"id": "9999999", "date": "2026-06-28",
                     "home_raw": "South Africa", "away_raw": "Egypt",
                     "round": "Round of 32"}]  # pairing exists in no bracket row
        existing = [{"match_id": 73, "provider_fixture_id": "9999999",
                     "home": "South Africa", "away": "Egypt",
                     "date": "2026-06-28", "phase": "r32"}]
        mapped, unmapped = builder.assign_knockout_ids(
            fixtures, rows, _incident_team_set(), existing_entries=existing)
        self.assertEqual(mapped, [])
        self.assertEqual(len(unmapped), 1)
        self.assertEqual(unmapped[0]["provider_fixture_id"], "9999999")

    def test_builder_source_no_longer_pairs_positionally_for_named(self):
        """Static pin: the index-for-index pairing loop is gone; the
        name-first assigner is wired into main()."""
        src = (ROOT / "scripts" / "live"
               / "build_provider_fixture_map.py").read_text()
        self.assertNotIn("provider_sorted[ix]", src,
                         "A.7: the positional index-pairing loop must not "
                         "return — it re-keys named fixtures wrong whenever "
                         "UTC order ≠ FIFA numbering within a date")
        self.assertGreaterEqual(src.count("assign_knockout_ids("), 2,
                                "A.7: main() must route knockouts through "
                                "assign_knockout_ids (def + call)")
        self.assertIn("from _knockout import classify_round", src,
                      "back-compat re-export must survive A.7")


# ─── fetch_results self-heal (sandboxed, like test_r32_fixture_map_autoextend)
def _api_fixture(fid, date_iso, home, away, *, status="FT", gh=None, ga=None,
                 winner=None, pens=(None, None), round_label="Round of 32",
                 elapsed=90):
    home_flag = {"home": True, "away": False}.get(winner)
    away_flag = {"home": False, "away": True}.get(winner)
    return {
        "fixture": {"id": fid, "date": date_iso, "referee": "Test Referee",
                    "status": {"short": status, "long": "Match Finished",
                               "elapsed": elapsed}},
        "league": {"id": 1, "season": 2026, "round": round_label},
        "teams": {"home": {"id": 1, "name": home, "winner": home_flag},
                  "away": {"id": 2, "name": away, "winner": away_flag}},
        "goals": {"home": gh, "away": ga},
        "score": {"halftime": {"home": None, "away": None},
                  "fulltime": {"home": gh, "away": ga},
                  "extratime": {"home": None, "away": None},
                  "penalty": {"home": pens[0], "away": pens[1]}},
    }


class HealBase(unittest.TestCase):
    """Sandboxed LIVE dir: 72 locked group results + a map we control."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wc26_namefirst_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        real_map = json.loads(
            (ROOT / "data" / "live" / "provider_fixture_map.json").read_text())
        self.group_entries = [
            f for f in real_map.get("fixtures", [])
            if int(f.get("match_id") or f.get("m") or 0) <= 72
        ]
        self.assertEqual(len(self.group_entries), 72)

        real_results = json.loads(
            (ROOT / "data" / "live" / "results_2026.json").read_text())
        self.group_completed = [
            m for m in real_results.get("completed_matches", [])
            if int(m.get("m", 0)) <= 72
        ]
        self.assertEqual(len(self.group_completed), 72)
        self._write_results(self.group_completed)

        self.fr = _load_fetch_results()
        self.fr.LIVE = self.tmp

        rows = self.fr._resolved_bracket_rows()
        self.r32 = {r["m"]: r for r in rows if r.get("stage") == "r32"}
        self.assertEqual(len(self.r32), 16)
        for m, r in self.r32.items():
            self.assertTrue(r.get("resolved_home") and r.get("resolved_away"),
                            f"R32 m={m} failed to resolve from group results")

    # helpers ---------------------------------------------------------------
    def _write_results(self, completed):
        (self.tmp / "results_2026.json").write_text(json.dumps({
            "schema": "test", "updated_at": "2026-07-03T16:31:00+00:00",
            "source": "api_football", "completed_matches": completed,
            "in_play": [], "warnings": [],
        }))

    def _write_map(self, ko_entries):
        doc = {
            "provider": "api_football", "league_id": "1", "season": "2026",
            "generated_at": "2026-07-03T16:31:12+00:00",
            "fixtures": self.group_entries + ko_entries,
        }
        (self.tmp / "provider_fixture_map.json").write_text(json.dumps(doc))

    def _serve(self, fixtures):
        payload = {"response": fixtures}
        self.fr.http_get_json = (
            lambda url, headers, timeout=20, retries=2: payload)

    def _map_doc(self):
        return json.loads((self.tmp / "provider_fixture_map.json").read_text())

    def _pfid(self, true_m):
        return f"999{true_m:04d}"

    def _bad_ko_entries(self, scramble: dict[int, int]):
        """Map entries keyed WRONG: the fixture whose TRUE id is `true_m`
        is written under `scramble[true_m]` — the 16:31Z failure shape."""
        out = []
        for true_m, wrong_m in scramble.items():
            row = self.r32[true_m]
            out.append({
                "match_id": wrong_m,
                "provider_fixture_id": self._pfid(true_m),
                "home": row["resolved_home"], "away": row["resolved_away"],
                "date": self.r32[wrong_m]["date"], "phase": "r32",
            })
        return out

    def _r32_payload(self, true_ms):
        fixtures = []
        for true_m in true_ms:
            row = self.r32[true_m]
            fixtures.append(_api_fixture(
                self._pfid(true_m), row["date"] + "T20:00:00+00:00",
                row["resolved_home"], row["resolved_away"],
                status="FT", gh=2, ga=1, winner="home"))
        return fixtures


# The incident permutation, in true_m → wrongly-committed-m form.
_INCIDENT_SCRAMBLE = {76: 74, 74: 75, 75: 76,   # m74/75/76 rotated
                      78: 77, 77: 78,           # m77/78 swapped
                      82: 81, 81: 82,           # m81/82 swapped
                      84: 83, 83: 84,           # m83/84 swapped
                      88: 86, 86: 87, 87: 88}   # m86/87/88 rotated


class TestFetchSelfHeal(HealBase):
    def test_heal_rekeys_wrong_committed_map_within_one_tick(self):
        """THE heal regression: a deliberately mis-keyed (committed) map +
        locked results must be re-keyed to name-correct ids on the SAME
        tick, the corrected map persisted, and the typed rekey warning
        emitted — zero manual action."""
        scramble = dict(_INCIDENT_SCRAMBLE)
        scramble[73] = 73   # one correct entry — must verify, not churn
        scramble[85] = 85
        self._write_map(self._bad_ko_entries(scramble))
        self._serve(self._r32_payload(sorted(scramble)))

        sink: list = []
        out = self.fr.fetch_api_football("test-key", warnings_sink=sink)

        # 1. Every result ingests under its TRUE internal id.
        got = {r["provider_fixture_id"]: r["m"] for r in out}
        expect = {self._pfid(m): m for m in scramble}
        self.assertEqual(got, expect,
                         "results must land under name-correct ids — the "
                         "16:31Z map wrote Brazil-Japan's result to m74")
        for r in out:
            row = self.r32[r["m"]]
            self.assertEqual({r["home"], r["away"]},
                             {row["resolved_home"], row["resolved_away"]})

        # 2. Map persisted corrected (same atomic path as the extension).
        doc = self._map_doc()
        by_pfid = {str(f["provider_fixture_id"]): f for f in doc["fixtures"]}
        for true_m in scramble:
            self.assertEqual(by_pfid[self._pfid(true_m)]["match_id"], true_m)
        self.assertEqual(doc["healed_by"],
                         "fetch_results.knockout_map_self_heal")
        self.assertEqual(doc["coverage"]["knockout_mapped"], len(scramble))
        self.assertEqual(doc["coverage"]["group_mapped"], 72)
        self.assertEqual(doc["provider"], "api_football")

        # 3. Typed warning lists old id → new id per fixture.
        rekey = [w for w in sink if w.get("type") == "mapped_fixture_rekeyed"]
        self.assertEqual(len(rekey), 1)
        w = rekey[0]
        self.assertEqual(w["severity"], "warning")
        self.assertEqual(w["provider"], "api_football")
        self.assertEqual(w["count"], len(_INCIDENT_SCRAMBLE))
        moves = {f["provider_fixture_id"]: (f["old_m"], f["new_m"])
                 for f in w["fixtures"]}
        for true_m, wrong_m in _INCIDENT_SCRAMBLE.items():
            self.assertEqual(moves[self._pfid(true_m)], (wrong_m, true_m))
        # Correct entries must not be reported as re-keyed.
        self.assertNotIn(self._pfid(73), moves)
        self.assertNotIn(self._pfid(85), moves)
        # No unmapped-fixture noise: the heal fully placed everything.
        self.assertEqual([w2 for w2 in sink
                          if w2.get("type") == "unmapped_provider_fixture"],
                         [])

    def test_heal_failure_drops_row_with_critical_unmapped_warning(self):
        """A mapped row that fails name verification AND cannot be re-keyed
        (its true id is verified-claimed by another fixture) must be dropped
        — never ingested under the bad id — and surface the existing
        critical unmapped warning."""
        m73 = self.r32[73]
        ghost = "8888888"
        self._write_map([
            {"match_id": 73, "provider_fixture_id": self._pfid(73),
             "home": m73["resolved_home"], "away": m73["resolved_away"],
             "date": m73["date"], "phase": "r32"},
            # Ghost entry: claims m74 but its payload names are m73's pair.
            {"match_id": 74, "provider_fixture_id": ghost,
             "home": m73["resolved_home"], "away": m73["resolved_away"],
             "date": self.r32[74]["date"], "phase": "r32"},
        ])
        fixtures = self._r32_payload([73])
        fixtures.append(_api_fixture(
            ghost, m73["date"] + "T22:00:00+00:00",
            m73["resolved_home"], m73["resolved_away"],
            status="FT", gh=0, ga=3, winner="away"))
        self._serve(fixtures)

        sink: list = []
        out = self.fr.fetch_api_football("test-key", warnings_sink=sink)

        got = {r["provider_fixture_id"]: r["m"] for r in out}
        self.assertEqual(got, {self._pfid(73): 73},
                         "the ghost row must NOT ingest under m74")
        crit = [w for w in sink
                if w.get("type") == "unmapped_provider_fixture"]
        self.assertEqual(len(crit), 1)
        self.assertEqual(crit[0]["severity"], "critical")
        self.assertIn(ghost, [f.get("fixture_id")
                              for f in crit[0]["fixtures"]])
        # m74 must not have gained a result.
        self.assertNotIn(74, got.values())

    def test_heal_dry_run_corrects_in_memory_but_never_persists(self):
        """--dry-run parity: corrected ids apply to THIS tick's records but
        provider_fixture_map.json is untouched."""
        self._write_map(self._bad_ko_entries({76: 74, 74: 75, 75: 76}))
        self._serve(self._r32_payload([74, 75, 76]))
        out = self.fr.fetch_api_football("test-key", dry_run=True,
                                         warnings_sink=[])
        got = {r["provider_fixture_id"]: r["m"] for r in out}
        self.assertEqual(got, {self._pfid(74): 74, self._pfid(75): 75,
                               self._pfid(76): 76})
        doc = self._map_doc()
        by_pfid = {str(f["provider_fixture_id"]): f for f in doc["fixtures"]}
        self.assertEqual(by_pfid[self._pfid(76)]["match_id"], 74,
                         "dry-run must not rewrite the map file")
        self.assertNotIn("healed_by", doc)

    def test_heal_noop_when_resolver_degraded(self):
        """No bracket resolution → no verification (conservative no-op, the
        pre-A.7 behavior). Pins that heal can't invent evidence."""
        self._write_map(self._bad_ko_entries({76: 74}))
        self.fr._resolved_bracket_rows = lambda: []
        self._serve(self._r32_payload([76]))
        sink: list = []
        out = self.fr.fetch_api_football("test-key", warnings_sink=sink)
        self.assertEqual([r["m"] for r in out], [74],
                         "without resolution the mapped id must be trusted")
        self.assertEqual(
            [w for w in sink if w.get("type") == "mapped_fixture_rekeyed"],
            [])

    def test_heal_stage_gating_protects_r16_from_poisoned_resolution(self):
        """When R32 entries are proven mis-keyed, the R16 slot resolution on
        disk is derived from those very wrong records — verifying/re-keying
        R16 in the same tick would act on poisoned data. It must be skipped
        this tick (it heals next tick, after corrected R32 records land)."""
        # On-disk results: 72 groups + DAMAGED R32 records (the committed
        # incident state): each wrong id holds the TRUE pairing of another id.
        damaged = []
        scramble = {76: 74, 74: 75, 75: 76, 78: 77, 77: 78}
        for true_m, wrong_m in scramble.items():
            row = self.r32[true_m]
            damaged.append({
                "m": wrong_m, "date": row["date"],
                "home": row["resolved_home"], "away": row["resolved_away"],
                "home_score": 2, "away_score": 1, "winner": "home",
                "status": "FT",
            })
        damaged.append({
            "m": 73, "date": self.r32[73]["date"],
            "home": self.r32[73]["resolved_home"],
            "away": self.r32[73]["resolved_away"],
            "home_score": 0, "away_score": 1, "winner": "away", "status": "FT",
        })
        self._write_results(self.group_completed + damaged)

        # Under DAMAGED resolution, m89 (W74 v W77) resolves to
        # {winner(damaged m74), winner(damaged m77)} = {Brazil, Norway*}
        # (*whatever the damaged records yield) — which coincidentally
        # matches the REAL R16 fixture Brazil v Norway whose CORRECT id is
        # m91. Without stage gating the heal would re-key it m91→m89.
        w76 = self.r32[76]["resolved_home"]   # winner of true m76 (home won)
        w78 = self.r32[78]["resolved_home"]   # winner of true m78 (home won)
        r16_pfid = "1568100"
        ko_entries = self._bad_ko_entries(scramble)
        ko_entries.append({
            "match_id": 91, "provider_fixture_id": r16_pfid,
            "home": w76, "away": w78, "date": "2026-07-05", "phase": "r16",
        })
        self._write_map(ko_entries)

        fixtures = self._r32_payload(sorted(scramble))
        fixtures.append(_api_fixture(
            r16_pfid, "2026-07-05T18:00:00+00:00", w76, w78,
            status="NS", round_label="Round of 16"))
        self._serve(fixtures)

        sink: list = []
        self.fr.fetch_api_football("test-key", warnings_sink=sink)

        rekey = [w for w in sink if w.get("type") == "mapped_fixture_rekeyed"]
        self.assertEqual(len(rekey), 1)
        moved = {f["provider_fixture_id"] for f in rekey[0]["fixtures"]}
        self.assertNotIn(r16_pfid, moved,
                         "R16 must not be re-keyed against resolution "
                         "derived from still-damaged R32 records")
        for f in rekey[0]["fixtures"]:
            self.assertEqual(f["stage"], "r32")
        doc = self._map_doc()
        by_pfid = {str(f["provider_fixture_id"]): f for f in doc["fixtures"]}
        self.assertEqual(by_pfid[r16_pfid]["match_id"], 91,
                         "the (actually correct) R16 entry must survive "
                         "the tick untouched")
        # R32 corrections still landed on disk.
        for true_m in scramble:
            self.assertEqual(by_pfid[self._pfid(true_m)]["match_id"], true_m)


class TestMainEndToEndHeal(HealBase):
    """main() against the full bad-committed-map shape: results file ends
    the tick keyed correctly + the rekey warning is in its warnings array."""

    def _group_payload(self):
        id_by_m = {f["match_id"]: str(f["provider_fixture_id"])
                   for f in self.group_entries}
        return [
            _api_fixture(id_by_m[m["m"]], m["date"] + "T18:00:00+00:00",
                         m["home"], m["away"], status="FT",
                         gh=m["home_score"], ga=m["away_score"],
                         round_label="Group Stage")
            for m in self.group_completed
        ]

    def _run_main(self):
        old_argv = sys.argv
        old_key = os.environ.get("API_FOOTBALL_KEY")
        old_ev = os.environ.pop("WC_FETCH_EVENTS", None)
        sys.argv = ["fetch_results.py", "--provider", "api_football"]
        os.environ["API_FOOTBALL_KEY"] = "test-key"
        try:
            return self.fr.main()
        finally:
            sys.argv = old_argv
            if old_key is None:
                os.environ.pop("API_FOOTBALL_KEY", None)
            else:
                os.environ["API_FOOTBALL_KEY"] = old_key
            if old_ev is not None:
                os.environ["WC_FETCH_EVENTS"] = old_ev

    def test_main_heals_bad_map_and_locks_results_under_true_ids(self):
        scramble = dict(_INCIDENT_SCRAMBLE)
        for m in (73, 79, 80, 85):
            scramble[m] = m     # the entries the rebuild happened to get right
        self._write_map(self._bad_ko_entries(scramble))
        payload = self._group_payload() + self._r32_payload(sorted(scramble))
        self._serve(payload)

        self.assertEqual(self._run_main(), 0)

        d = json.loads((self.tmp / "results_2026.json").read_text())
        self.assertEqual(len(d["completed_matches"]), 88)
        for rec in d["completed_matches"]:
            if rec["m"] < 73:
                continue
            row = self.r32[rec["m"]]
            self.assertEqual(
                {rec["home"], rec["away"]},
                {row["resolved_home"], row["resolved_away"]},
                f"m{rec['m']} holds the wrong pairing after heal — "
                f"decide_knockout would raise and trip the breaker")
        types = [w.get("type") for w in d["warnings"]]
        self.assertIn("mapped_fixture_rekeyed", types)
        self.assertNotIn("unmapped_provider_fixture", types)
        w = next(x for x in d["warnings"]
                 if x.get("type") == "mapped_fixture_rekeyed")
        self.assertEqual(w["count"], len(_INCIDENT_SCRAMBLE))


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
