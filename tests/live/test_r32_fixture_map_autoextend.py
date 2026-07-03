"""
A.6 regression — knockout fixture-map auto-extension in fetch_results.py.

Grounds: the R32 freeze (2026-06-28 → 2026-07-03). Chain of four:
  1. data/live/provider_fixture_map.json was generated 2026-06-09 by the
     pre-A.1 builder — 72 group ids, no KO ids, no `coverage` block.
  2. live-matchday.yml's rebuild Trigger 2 read `.coverage.knockout_total
     // 0` from that legacy map → `0 -lt 0` → never fired.
  3. fetch_results' fuzzy fallback (date±1 + home + away) can never match a
     KO fixture — bracket rows carry slot codes ("1A", "W74"), not teams.
  4. The drop only printed to stderr — results_2026.json kept warnings=[]
     and live_state.json showed a healthy tick while completed_matches froze
     at 72.

This suite pins the fix:
  * an R32 provider fixture is ingested via the auto-extended map — zero
    extra API calls, no manual rebuild step (T1, T3)
  * PEN sub-scores + winner flow through for KO matches (T2)
  * unmapped tournament-window fixtures emit a loud structured warning
    that reaches results_2026.json (T4, T7b) — never stderr-only
  * pre-draw placeholder fixtures stay quiet (T5)
  * the extended map is persisted and reused on later ticks — O(1) path
    even if bracket resolution is unavailable (T6)
  * main() end-to-end: the KO result lands in results_2026.json's
    completed_matches (T7)

Payload shapes mirror the A.0 probe samples
(tests/live/provider_samples/apifootball_*.json: score.penalty.{home,away},
teams.{home,away}.winner, league.round). Bracket/team ground truth comes
from the committed repo data (wc2026_config + knockout_bracket + the 72
locked group results), resolved through the same export_ko_advance resolver
production uses.

Run:
    python3 tests/live/test_r32_fixture_map_autoextend.py
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

_MODULE_SEQ = 0


def _load_fetch_results():
    """Import a FRESH fetch_results instance so per-test monkeypatching of
    LIVE / http_get_json / _resolved_bracket_rows can't leak across tests."""
    global _MODULE_SEQ
    _MODULE_SEQ += 1
    spec = importlib.util.spec_from_file_location(
        f"fetch_results_autoextend_{_MODULE_SEQ}",
        ROOT / "scripts" / "live" / "fetch_results.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _api_fixture(fid, date_iso, home, away, *, status="FT", gh=None, ga=None,
                 winner=None, pens=(None, None), round_label="Round of 32",
                 elapsed=90):
    """Minimal API-Football /fixtures row — same fields the A.0 probe kept."""
    home_flag = {"home": True, "away": False}.get(winner)
    away_flag = {"home": False, "away": True}.get(winner)
    return {
        "fixture": {
            "id": fid,
            "date": date_iso,
            "referee": "Test Referee",
            "status": {"short": status, "long": "Match Finished",
                       "elapsed": elapsed},
        },
        "league": {"id": 1, "season": 2026, "round": round_label},
        "teams": {
            "home": {"id": 1, "name": home, "winner": home_flag},
            "away": {"id": 2, "name": away, "winner": away_flag},
        },
        "goals": {"home": gh, "away": ga},
        "score": {
            "halftime": {"home": None, "away": None},
            "fulltime": {"home": gh, "away": ga},
            "extratime": {"home": None, "away": None},
            "penalty": {"home": pens[0], "away": pens[1]},
        },
    }


class AutoExtendBase(unittest.TestCase):
    """Sandboxed LIVE dir seeded with the EXACT pre-fix state: legacy
    group-only fixture map (no coverage block) + 72 locked group results."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wc26_autoextend_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        real_map = json.loads(
            (ROOT / "data" / "live" / "provider_fixture_map.json").read_text())
        legacy_fixtures = [
            f for f in real_map.get("fixtures", [])
            if int(f.get("match_id") or f.get("m") or 0) <= 72
        ]
        self.assertEqual(len(legacy_fixtures), 72,
                         "precondition: repo map must contain the 72 group ids")
        self.legacy_map = {
            "provider": "api_football",
            "league_id": "1",
            "season": "2026",
            "generated_at": "2026-06-09T20:33:39+00:00",
            "fixtures": legacy_fixtures,
            "unmapped_internal_count": 0,
            "unmapped_provider_count": 0,
        }
        (self.tmp / "provider_fixture_map.json").write_text(
            json.dumps(self.legacy_map))

        real_results = json.loads(
            (ROOT / "data" / "live" / "results_2026.json").read_text())
        group_completed = [m for m in real_results.get("completed_matches", [])
                           if int(m.get("m", 0)) <= 72]
        self.assertEqual(len(group_completed), 72,
                         "precondition: repo results must hold all 72 group matches")
        (self.tmp / "results_2026.json").write_text(json.dumps({
            "schema": real_results.get("schema", ""),
            "updated_at": "2026-06-27T23:59:00+00:00",
            "source": "api_football",
            "completed_matches": group_completed,
            "in_play": [],
            "warnings": [],
        }))

        self.fr = _load_fetch_results()
        self.fr.LIVE = self.tmp

        rows = self.fr._resolved_bracket_rows()
        self.r32 = {r["m"]: r for r in rows if r.get("stage") == "r32"}
        self.assertEqual(len(self.r32), 16)
        # All groups complete → every R32 pairing must resolve.
        for m, r in self.r32.items():
            self.assertTrue(r.get("resolved_home") and r.get("resolved_away"),
                            f"R32 m={m} failed to resolve from group results")

    def _serve(self, fixtures: list[dict]):
        payload = {"response": fixtures}
        self.fr.http_get_json = (
            lambda url, headers, timeout=20, retries=2: payload)

    def _map_doc(self) -> dict:
        return json.loads((self.tmp / "provider_fixture_map.json").read_text())


class TestR32AutoExtendIngestion(AutoExtendBase):
    def test_r32_result_ingested_via_autoextended_map(self):
        """T1 — the headline regression: an R32 FT result must be ingested on
        the same tick with NO manual map rebuild, via the auto-extended map."""
        m73 = self.r32[73]
        self._serve([_api_fixture(
            "9990073", m73["date"] + "T20:00:00+00:00",
            m73["resolved_home"], m73["resolved_away"],
            status="FT", gh=2, ga=1, winner="home")])
        sink: list = []
        out = self.fr.fetch_api_football("test-key", warnings_sink=sink)

        self.assertEqual(len(out), 1)
        rec = out[0]
        self.assertEqual(rec["m"], 73)
        self.assertEqual(rec["status"], "FT")
        self.assertEqual(rec["home"], m73["resolved_home"])
        self.assertEqual(rec["away"], m73["resolved_away"])
        self.assertEqual(rec["home_score"], 2)
        self.assertEqual(rec["away_score"], 1)
        self.assertEqual(rec["winner"], "home")
        self.assertIsNone(rec["home_pens"])
        self.assertIsNone(rec["away_pens"])
        self.assertEqual(sink, [], "clean mapping must not raise warnings")

        # Map persisted with the new id + truthful builder-shaped coverage
        # (the workflow's bootstrap trigger reads these fields).
        doc = self._map_doc()
        by_id = {str(f["provider_fixture_id"]): f for f in doc["fixtures"]}
        self.assertIn("9990073", by_id)
        self.assertEqual(by_id["9990073"]["match_id"], 73)
        self.assertEqual(by_id["9990073"]["phase"], "r32")
        self.assertEqual(doc["coverage"]["group_mapped"], 72)
        self.assertEqual(doc["coverage"]["knockout_mapped"], 1)
        self.assertEqual(doc["coverage"]["knockout_total"], 32)
        self.assertEqual(doc["extended_by"],
                         "fetch_results.knockout_auto_extension")
        # Provider identity preserved — the workflow's provider-mismatch
        # trigger must not fire after an in-process extension.
        self.assertEqual(doc["provider"], "api_football")

    def test_pen_result_flows_through(self):
        """T2 — PEN sub-scores + winner survive the KO path end-to-end
        (score.penalty.{home,away} + teams.*.winner, per the A.0 probe)."""
        m84 = self.r32[84]
        self._serve([_api_fixture(
            "9990084", m84["date"] + "T20:00:00+00:00",
            m84["resolved_home"], m84["resolved_away"],
            status="PEN", gh=1, ga=1, winner="away", pens=(3, 4),
            elapsed=120)])
        out = self.fr.fetch_api_football("test-key", warnings_sink=[])
        self.assertEqual(len(out), 1)
        rec = out[0]
        self.assertEqual(rec["m"], 84)
        self.assertEqual(rec["status"], "PEN")
        self.assertEqual((rec["home_score"], rec["away_score"]), (1, 1))
        self.assertEqual(rec["home_pens"], 3)
        self.assertEqual(rec["away_pens"], 4)
        self.assertEqual(rec["winner"], "away",
                         "decide_knockout locks on this field — must be exact")

    def test_full_r32_batch_maps_all_sixteen(self):
        """T3 — all 16 R32 fixtures (mixed FT/AET/PEN) map in one tick."""
        fixtures = []
        for i, m in enumerate(sorted(self.r32)):
            row = self.r32[m]
            if i == 3:
                fx = _api_fixture(f"999{m:04d}", row["date"] + "T20:00:00+00:00",
                                  row["resolved_home"], row["resolved_away"],
                                  status="AET", gh=2, ga=1, winner="home",
                                  elapsed=120)
            elif i == 7:
                fx = _api_fixture(f"999{m:04d}", row["date"] + "T20:00:00+00:00",
                                  row["resolved_home"], row["resolved_away"],
                                  status="PEN", gh=0, ga=0, winner="away",
                                  pens=(2, 4), elapsed=120)
            else:
                fx = _api_fixture(f"999{m:04d}", row["date"] + "T20:00:00+00:00",
                                  row["resolved_home"], row["resolved_away"],
                                  status="FT", gh=1, ga=0, winner="home")
            fixtures.append(fx)
        sink: list = []
        self._serve(fixtures)
        out = self.fr.fetch_api_football("test-key", warnings_sink=sink)
        self.assertEqual(sorted(r["m"] for r in out), list(range(73, 89)))
        self.assertEqual(sink, [])
        doc = self._map_doc()
        self.assertEqual(doc["coverage"]["knockout_mapped"], 16)
        self.assertEqual(doc["coverage"]["total_mapped"], 88)


class TestUnmappedLoudWarning(AutoExtendBase):
    def test_unmapped_locked_fixture_emits_loud_warning(self):
        """T4 — a locked tournament-window fixture that cannot be placed must
        surface a typed critical warning (the pre-fix silent-drop mode)."""
        # A pairing that exists nowhere in the bracket: m73's home team vs
        # m88's home team (both real WC2026 teams, never opponents in R32).
        a = self.r32[73]["resolved_home"]
        b = self.r32[88]["resolved_home"]
        self._serve([_api_fixture(
            "9999999", self.r32[88]["date"] + "T20:00:00+00:00", a, b,
            status="FT", gh=1, ga=0, winner="home")])
        sink: list = []
        out = self.fr.fetch_api_football("test-key", warnings_sink=sink)
        self.assertEqual(out, [], "an unplaceable fixture must not be emitted")
        self.assertEqual(len(sink), 1)
        w = sink[0]
        self.assertEqual(w["type"], "unmapped_provider_fixture")
        self.assertEqual(w["severity"], "critical")
        self.assertEqual(w["provider"], "api_football")
        self.assertEqual(w["count"], 1)
        self.assertIn("NOT being ingested", w["message"])
        self.assertIn("build_provider_fixture_map.py", w["message"])
        self.assertEqual(w["fixtures"][0]["fixture_id"], "9999999")
        self.assertEqual(w["fixtures"][0]["home"], a)
        # And the bogus id must NOT have been grafted into the map.
        doc = self._map_doc()
        ids = {str(f["provider_fixture_id"]) for f in doc["fixtures"]}
        self.assertNotIn("9999999", ids)

    def test_predraw_placeholder_fixture_stays_quiet(self):
        """T5 — provider placeholder names ("Winner Group A") on a future KO
        fixture are expected pre-resolution — no warning spam."""
        self._serve([_api_fixture(
            "9990075", "2026-06-29T20:00:00+00:00",
            "Winner Group A", "Runner Up Group B", status="NS")])
        sink: list = []
        out = self.fr.fetch_api_football("test-key", warnings_sink=sink)
        self.assertEqual(out, [])
        self.assertEqual(sink, [], "placeholder KO fixtures must stay quiet")

    def test_friendly_outside_window_stays_quiet(self):
        """Unmapped fixtures outside the tournament window (pre-tournament
        friendlies etc.) never warn."""
        self._serve([_api_fixture(
            "8888888", "2026-06-01T20:00:00+00:00",
            self.r32[73]["resolved_home"], self.r32[88]["resolved_home"],
            status="FT", gh=2, ga=2)])
        sink: list = []
        self.fr.fetch_api_football("test-key", warnings_sink=sink)
        self.assertEqual(sink, [])


class TestMapPersistenceAndReuse(AutoExtendBase):
    def test_persisted_map_reused_without_resolver(self):
        """T6 — once auto-extended, later ticks take the O(1) map path even
        if bracket resolution is unavailable (e.g. results file truncated)."""
        m73 = self.r32[73]
        fx = _api_fixture("9990073", m73["date"] + "T20:00:00+00:00",
                          m73["resolved_home"], m73["resolved_away"],
                          status="FT", gh=2, ga=1, winner="home")
        self._serve([fx])
        out1 = self.fr.fetch_api_football("test-key", warnings_sink=[])
        self.assertEqual(out1[0]["m"], 73)
        # Cripple the resolver — the persisted map must carry the mapping.
        self.fr._resolved_bracket_rows = lambda: []
        sink: list = []
        out2 = self.fr.fetch_api_football("test-key", warnings_sink=sink)
        self.assertEqual(out2[0]["m"], 73)
        self.assertEqual(sink, [])

    def test_dry_run_does_not_persist_map(self):
        """--dry-run must not mutate provider_fixture_map.json (parity with
        the no-write contract of fetch_results --dry-run)."""
        m73 = self.r32[73]
        self._serve([_api_fixture("9990073", m73["date"] + "T20:00:00+00:00",
                                  m73["resolved_home"], m73["resolved_away"],
                                  status="FT", gh=2, ga=1, winner="home")])
        out = self.fr.fetch_api_football("test-key", dry_run=True,
                                         warnings_sink=[])
        # In-memory extension still applies this tick…
        self.assertEqual(out[0]["m"], 73)
        # …but nothing was written.
        doc = self._map_doc()
        ids = {str(f["provider_fixture_id"]) for f in doc["fixtures"]}
        self.assertNotIn("9990073", ids)
        self.assertNotIn("coverage", doc)

    def test_foreign_provider_map_not_grafted(self):
        """A football_data map must never receive api_football ids — the
        workflow's provider-mismatch trigger owns that rebuild."""
        doc = self._map_doc()
        doc["provider"] = "football_data"
        (self.tmp / "provider_fixture_map.json").write_text(json.dumps(doc))
        m73 = self.r32[73]
        self._serve([_api_fixture("9990073", m73["date"] + "T20:00:00+00:00",
                                  m73["resolved_home"], m73["resolved_away"],
                                  status="FT", gh=2, ga=1, winner="home")])
        sink: list = []
        out = self.fr.fetch_api_football("test-key", warnings_sink=sink)
        # Not mapped (no graft) → the locked fixture surfaces as a loud warning.
        self.assertEqual(out, [])
        self.assertEqual(len(sink), 1)
        self.assertEqual(sink[0]["type"], "unmapped_provider_fixture")
        doc2 = self._map_doc()
        self.assertEqual(doc2["provider"], "football_data")
        ids = {str(f["provider_fixture_id"]) for f in doc2["fixtures"]}
        self.assertNotIn("9990073", ids)


class TestMainEndToEnd(AutoExtendBase):
    """T7 — fetch_results.main(): the R32 result lands in results_2026.json's
    completed_matches; unmapped warnings land in the file's warnings array."""

    def _group_payload(self) -> list[dict]:
        results = json.loads((self.tmp / "results_2026.json").read_text())
        id_by_m = {f["match_id"]: str(f["provider_fixture_id"])
                   for f in self.legacy_map["fixtures"]}
        return [
            _api_fixture(id_by_m[m["m"]], m["date"] + "T18:00:00+00:00",
                         m["home"], m["away"], status="FT",
                         gh=m["home_score"], ga=m["away_score"])
            for m in results["completed_matches"]
        ]

    def _run_main(self) -> int:
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

    def test_main_locks_r32_pen_result_into_results_file(self):
        m73 = self.r32[73]
        payload = self._group_payload()
        payload.append(_api_fixture(
            "9990073", m73["date"] + "T20:00:00+00:00",
            m73["resolved_home"], m73["resolved_away"],
            status="PEN", gh=1, ga=1, winner="home", pens=(4, 2),
            elapsed=120))
        self._serve(payload)
        self.assertEqual(self._run_main(), 0)

        d = json.loads((self.tmp / "results_2026.json").read_text())
        self.assertEqual(len(d["completed_matches"]), 73,
                         "R32 result must unfreeze completed_matches past 72")
        ko = next(m for m in d["completed_matches"] if m["m"] == 73)
        self.assertEqual(ko["status"], "PEN")
        self.assertEqual(ko["home"], m73["resolved_home"])
        self.assertEqual(ko["away"], m73["resolved_away"])
        self.assertEqual((ko["home_pens"], ko["away_pens"]), (4, 2))
        self.assertEqual(ko["winner"], "home")
        self.assertEqual(d["warnings"], [])
        self.assertEqual(d["source"], "api_football")

    def test_main_writes_unmapped_warning_into_results_file(self):
        """The never-silent guarantee: a locked-but-unplaceable fixture must
        put a critical warning into results_2026.json (→ live_state.json)."""
        payload = self._group_payload()
        payload.append(_api_fixture(
            "9999999", self.r32[88]["date"] + "T20:00:00+00:00",
            self.r32[73]["resolved_home"], self.r32[88]["resolved_home"],
            status="FT", gh=1, ga=0, winner="home"))
        self._serve(payload)
        self.assertEqual(self._run_main(), 0)

        d = json.loads((self.tmp / "results_2026.json").read_text())
        self.assertEqual(len(d["completed_matches"]), 72)
        types = [w.get("type") for w in d["warnings"]]
        self.assertIn("unmapped_provider_fixture", types)
        w = next(x for x in d["warnings"]
                 if x.get("type") == "unmapped_provider_fixture")
        self.assertEqual(w["severity"], "critical")
        self.assertEqual(w["fixtures"][0]["fixture_id"], "9999999")


class TestStaticPins(unittest.TestCase):
    """Source-level pins in the style of test_fetch_results_knockout.py —
    a revert of any A.6 wiring must trip a named assertion."""

    def test_both_adapters_wire_the_extension_and_warning(self):
        src = (ROOT / "scripts" / "live" / "fetch_results.py").read_text()
        self.assertGreaterEqual(src.count("extend_fixture_map_with_knockouts("), 3,
            "A.6: both adapters must call extend_fixture_map_with_knockouts "
            "(plus its def) — a revert re-freezes KO ingestion for that provider")
        self.assertGreaterEqual(src.count("build_unmapped_warnings(unmapped"), 2,
            "A.6: both adapters must emit structured unmapped warnings — "
            "stderr-only was exactly the silent-drop failure mode")
        self.assertIn("warnings_list.extend(adapter_warnings)", src,
            "A.6: main() must fold adapter warnings into results_2026.json")

    def test_workflow_bootstrap_trigger_not_dead_code(self):
        yml = (ROOT / ".github" / "workflows" / "live-matchday.yml").read_text()
        self.assertIn(".coverage.knockout_total // 32", yml,
            "A.6: knockout_total must default to 32 — the `// 0` default made "
            "Trigger 2 dead code against the legacy (pre-A.1) map and froze "
            "the dashboard at 72 matches through R32")
        self.assertIn("unmapped_provider_fixture", yml,
            "A.6: the workflow bootstrap rebuild must key off the in-process "
            "loud warning so it can't refetch /fixtures every tick")

    def test_classify_round_canonical_home_and_reexport(self):
        ko_src = (ROOT / "scripts" / "live" / "_knockout.py").read_text()
        self.assertIn("def classify_round", ko_src,
            "A.6: classify_round's canonical home is _knockout.py (fetch_results "
            "cannot import the builder — circular)")
        builder_src = (ROOT / "scripts" / "live"
                       / "build_provider_fixture_map.py").read_text()
        self.assertIn("from _knockout import classify_round", builder_src,
            "A.6: builder must re-export classify_round for back-compat")


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
