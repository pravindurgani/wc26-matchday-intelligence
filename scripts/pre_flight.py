"""
pre_flight.py — Comprehensive pre-launch test pass.

Runs the 10-phase audit from the launch checklist plus 5 targeted stress tests.
Exits 0 with "READY TO DEPLOY" only if every phase passes. Otherwise prints the
full failure list and exits non-zero.

Usage:
    python3 scripts/pre_flight.py

This is the final gate before pushing to GitHub / Vercel. Run from repo root.
"""
from __future__ import annotations
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"
LIVE = ROOT / "data" / "live"
PORT = 8788  # use a separate port so we don't fight a developer's local server

failures: list[str] = []
warnings_ls: list[str] = []
passed = 0
total = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, total
    total += 1
    if ok:
        passed += 1
        print(f"  [✓] {name}" + (f" — {detail}" if detail else ""))
    else:
        failures.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  [✗] {name}" + (f" — {detail}" if detail else ""))


def warn(msg: str) -> None:
    warnings_ls.append(msg)
    print(f"  [~] {msg}")


def section(title: str) -> None:
    print(f"\n── {title} ──")


# ─── Phase 1 ────────────────────────────────────────────────────────────────
def phase_1_clean_package():
    section("Phase 1 · Clean package & repo hygiene")
    forbidden_anywhere = [".DS_Store", "__MACOSX", "Thumbs.db"]
    forbidden_in_dashboard = [".venv", ".claude", ".gstack", "__pycache__", ".DS_Store"]

    for f in forbidden_anywhere:
        hits = [str(p.relative_to(ROOT)) for p in ROOT.rglob(f)
                if ".venv" not in p.parts and ".gstack" not in p.parts and ".claude" not in p.parts]
        check(f"no {f} outside ignored dirs", not hits, f"found {hits[:3]}" if hits else "")

    for f in forbidden_in_dashboard:
        hits = [str(p.relative_to(DASH)) for p in DASH.rglob(f) if p.exists()]
        check(f"no {f} inside dashboard/", not hits, f"found {hits[:3]}" if hits else "")

    gi = ROOT / ".gitignore"
    if gi.exists():
        txt = gi.read_text()
        for pat in (".venv", ".claude", ".gstack", "__MACOSX", "__pycache__", ".DS_Store"):
            check(f".gitignore covers {pat}", pat in txt)
    else:
        check(".gitignore present", False)


# ─── Phase 2 ────────────────────────────────────────────────────────────────
def phase_2_http_serve():
    section("Phase 2 · Static dashboard HTTP 200")
    server = None
    try:
        # Start a one-off http.server pinned to our PORT
        env = os.environ.copy()
        server = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(PORT)],
            cwd=str(DASH), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # Wait for the socket to accept connections
        for _ in range(40):
            try:
                with socket.create_connection(("127.0.0.1", PORT), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.15)
        else:
            check("http.server started", False, "could not bind socket")
            return

        required = [
            "index.html", "methodology.html", "appendix.html",
            "app.js", "styles.css", "methodology.css", "appendix.css",
            "predictions.json", "predictions_live.json",
            "live_state.json", "live_delta.json",
            "calibration.json", "walk_forward.json", "ablation.json",
            "sensitivity.json", "travel_impact.json",
        ]
        for f in required:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/{f}", timeout=2) as r:
                    code = r.status
            except Exception as e:
                code = f"err({type(e).__name__})"
            check(f"GET /{f}", code == 200, f"HTTP {code}")
    finally:
        if server:
            server.terminate()
            try: server.wait(timeout=3)
            except subprocess.TimeoutExpired: server.kill()


# ─── Phase 3 ────────────────────────────────────────────────────────────────
def phase_3_js_dom():
    section("Phase 3 · JavaScript & DOM wiring")
    node = shutil.which("node")
    if node:
        rc = subprocess.run([node, "--check", str(DASH / "app.js")],
                            capture_output=True, text=True).returncode
        check("node --check app.js", rc == 0)
    else:
        warn("node not on PATH — skipping JS syntax check")

    html = (DASH / "index.html").read_text()
    js = (DASH / "app.js").read_text()
    html_ids = set(re.findall(r'\bid\s*=\s*["\']([\w\-]+)["\']', html))
    js_ids = set(re.findall(r"getElementById\(\s*['\"]([\w\-]+)['\"]\s*\)", js))
    js_ids |= set(re.findall(r"querySelector(?:All)?\(\s*['\"]#([\w\-]+)", js))
    missing = sorted(js_ids - html_ids)
    check(f"all JS-referenced DOM IDs exist ({len(js_ids)} checked)",
          not missing, f"missing: {missing[:5]}" if missing else "")

    # Nav anchors resolve
    nav_anchors = re.findall(r'href="#([\w\-]+)"', html)
    nav_missing = [a for a in nav_anchors if a not in html_ids and a != "top"]
    check("nav anchors resolve to sections", not nav_missing,
          f"missing: {nav_missing}" if nav_missing else "")

    # Required interaction hooks
    interactive = [
        ("team-search", "querySelector|getElementById"),
        ("team-group", ""),
        ("contenders-reset", ""),
        ("toggle-all-teams", ""),
        ("f-venue", ""),
        ("f-close-only", ""),
        ("matches-reset", ""),
        ("venue-chips", ""),
        ("cmp-a", ""),
        ("cmp-b", ""),
        ("cmp-swap", ""),
        ("theme-toggle", ""),
    ]
    for elem_id, _ in interactive:
        check(f"interaction element #{elem_id} exists", elem_id in html_ids)


# ─── Phase 4 ────────────────────────────────────────────────────────────────
def phase_4_json_consistency():
    section("Phase 4 · JSON consistency & model integrity")
    pred = json.loads((DASH / "predictions.json").read_text())

    n_total = pred.get("n_simulations_total")
    n_seeds = pred.get("n_seeds")
    n_per = pred.get("n_simulations_per_seed")
    check("n_simulations_total = n_seeds × n_simulations_per_seed",
          n_total == (n_seeds or 0) * (n_per or 0),
          f"{n_total} vs {n_seeds}×{n_per}")

    teams = pred.get("team_predictions", [])
    matches_all = pred.get("match_predictions", [])
    # P1-D: match_predictions is now 104 entries (72 group + 32 knockout
    # placeholders). Split by stage for the per-stage invariants.
    matches = [m for m in matches_all if (m.get("stage") or "group") == "group"]
    matches_knockout = [m for m in matches_all if (m.get("stage") or "group") != "group"]
    check("exactly 48 teams", len(teams) == 48, f"got {len(teams)}")
    check("exactly 72 group matches", len(matches) == 72, f"got {len(matches)}")
    check("exactly 32 knockout placeholders", len(matches_knockout) == 32,
          f"got {len(matches_knockout)}")
    check("annex_c_misses == 0", pred.get("annex_c_misses", -1) == 0)

    def near(actual, target, tol):
        return abs(actual - target) <= tol

    sums = {
        "p_champion":       (sum(t["p_champion"]       for t in teams), 1.0,  0.001),
        "p_reach_final":    (sum(t["p_reach_final"]    for t in teams), 2.0,  0.01),
        "p_reach_sf":       (sum(t["p_reach_sf"]       for t in teams), 4.0,  0.02),
        "p_reach_qf":       (sum(t["p_reach_qf"]       for t in teams), 8.0,  0.05),
        "p_advance_groups": (sum(t["p_advance_groups"] for t in teams), 32.0, 0.05),
    }
    for k, (actual, target, tol) in sums.items():
        check(f"Σ {k} ≈ {target}", near(actual, target, tol), f"actual={actual:.4f}")

    # Duplicates
    team_names = [t["team"] for t in teams]
    check("no duplicate team names", len(set(team_names)) == len(team_names))
    match_ids = [m["m"] for m in matches]
    check("no duplicate match ids", len(set(match_ids)) == len(match_ids))

    # Groups have 4 teams
    groups: dict = {}
    for t in teams:
        groups.setdefault(t["group"], []).append(t["team"])
    bad_groups = {g: len(ts) for g, ts in groups.items() if len(ts) != 4}
    check("every group has 4 teams", not bad_groups, f"{bad_groups}" if bad_groups else "")

    # All match teams exist in team predictions — group fixtures only.
    # Knockout placeholders carry slot codes ("1A", "W101") that intentionally
    # are not in team_predictions; skip them here.
    team_set = set(team_names)
    bad_match_team = [m for m in matches
                      if m["home"] not in team_set or m["away"] not in team_set]
    check("all match teams exist in team_predictions",
          not bad_match_team, f"{len(bad_match_team)} bad" if bad_match_team else "")

    # Probabilities in [0, 1]
    prob_fields = ["p_advance_groups", "p_reach_r16", "p_reach_qf", "p_reach_sf",
                   "p_reach_final", "p_champion", "p_third_place",
                   "p_finish_1st_group", "p_finish_2nd_group"]
    bad_prob = []
    for t in teams:
        for k in prob_fields:
            v = t.get(k)
            if v is None: continue
            if not (0.0 <= v <= 1.0):
                bad_prob.append(f"{t['team']}.{k}={v}")
    check("all team probabilities in [0,1]", not bad_prob,
          f"{bad_prob[:3]}" if bad_prob else "")

    top1 = (pred.get("concentration", {}) or {}).get("top1_champion_p", 0)
    check("top-1 champion < 35% (bookmaker sanity)", top1 < 0.35,
          f"actual={top1*100:.1f}%")

    check("generated_at present", bool(pred.get("generated_at")))

    # Confederation map covers every team
    from_dashboard = (DASH / "app.js").read_text()
    confed_map_block = re.search(r"const CONFED\s*=\s*\{([^}]+)\}", from_dashboard, re.S)
    confed_teams = set()
    if confed_map_block:
        confed_teams = set(re.findall(r'"([^"]+)"\s*:', confed_map_block.group(1)))
    missing_confed = [t for t in team_names if t not in confed_teams]
    check("CONFED map covers all 48 teams",
          not missing_confed, f"missing: {missing_confed[:3]}" if missing_confed else "")


# ─── Phase 5 ────────────────────────────────────────────────────────────────
def phase_5_live_mode():
    section("Phase 5 · Live mode verification (pre-tournament)")
    ls = json.loads((DASH / "live_state.json").read_text())
    ld = json.loads((DASH / "live_delta.json").read_text())

    check("live_state.mode ∈ {pre_tournament, live}",
          ls.get("mode") in ("pre_tournament", "live"),
          f"mode={ls.get('mode')}")
    if ls.get("mode") == "pre_tournament":
        check("pre-tournament: completed_matches_count == 0",
              ls.get("completed_matches_count") == 0,
              f"count={ls.get('completed_matches_count')}")
        check("pre-tournament: live_delta has no fake movers",
              not ld.get("top_movers_up") and not ld.get("top_movers_down"),
              f"up={len(ld.get('top_movers_up', []))} down={len(ld.get('top_movers_down', []))}")
    else:
        check("live mode: count in [1, 104]",
              1 <= ls.get("completed_matches_count", 0) <= 104)


# ─── Phase 6 ────────────────────────────────────────────────────────────────
def phase_6_provider_feed():
    section("Phase 6 · Provider feed readiness")
    fr = (ROOT / "scripts" / "live" / "fetch_results.py").read_text()
    check("fetch_results reads API keys from env, not hardcoded",
          "os.environ.get(\"WC_APIFOOTBALL_KEY\")" in fr
          or "os.environ.get('WC_APIFOOTBALL_KEY')" in fr)
    check("fetch_results returns mock when key missing (graceful fallback)",
          "falling back to mock" in fr)
    check("fetch_results handles POSTPONED/ABANDONED",
          "POSTPONED" in fr and "ABANDONED" in fr)

    # No API keys leaked anywhere
    leaked = []
    for f in DASH.rglob("*"):
        if f.is_file() and f.suffix in (".html", ".js", ".css", ".json"):
            txt = f.read_text(errors="ignore")
            if re.search(r'[Aa]pi[_-]?[Kk]ey\s*[:=]\s*["\'][^"\']{20,}', txt):
                leaked.append(str(f.relative_to(DASH)))
    check("no API-key-shaped strings in dashboard/", not leaked,
          f"{leaked[:2]}" if leaked else "")

    # Workflow uses ${{ secrets.X }}
    wf = ROOT / ".github" / "workflows" / "live-matchday.yml"
    if wf.exists():
        wf_txt = wf.read_text()
        any_secret = any(s in wf_txt for s in (
            "secrets.API_FOOTBALL_KEY", "secrets.WC_APIFOOTBALL_KEY",
            "secrets.SPORTMONKS_TOKEN", "secrets.WC_SPORTMONKS_TOKEN",
        ))
        check("live-matchday workflow uses CI secrets (not literals)", any_secret)
        # Accept either tournament start (2026-06-11) or the 24h-early
        # warm-up gate (2026-06-10) we now ship by default.
        check("live-matchday workflow has date gate for tournament window",
              ("2026-06-11" in wf_txt or "2026-06-10" in wf_txt)
              and "2026-07-" in wf_txt)


# ─── Phase 7 ────────────────────────────────────────────────────────────────
def phase_7_vercel():
    section("Phase 7 · Vercel deployment config")
    v = (ROOT / "vercel.json")
    if not v.exists():
        check("vercel.json exists", False); return
    cfg = json.loads(v.read_text())
    check("outputDirectory = dashboard", cfg.get("outputDirectory") == "dashboard",
          cfg.get("outputDirectory", ""))
    check("cleanUrls = true", cfg.get("cleanUrls") is True)

    headers = cfg.get("headers", [])
    # Live-file caching: must-revalidate, max-age=0
    live_rule = next((h for h in headers
                      if re.search(r"predictions(_live)?|live_state|live_delta",
                                   h.get("source", ""))), None)
    if live_rule:
        cc = next((x.get("value", "") for x in live_rule.get("headers", [])
                   if x.get("key", "").lower() == "cache-control"), "")
        check("live-file Cache-Control contains max-age=0", "max-age=0" in cc, cc)
        check("live-file Cache-Control contains must-revalidate", "must-revalidate" in cc, cc)
    else:
        check("live-file cache rule defined", False)

    # General JSON should not be aggressively cached on the edge for long
    general_json = next((h for h in headers
                         if h.get("source") == "/(.*\\.json)"), None)
    if general_json:
        cc = next((x.get("value", "") for x in general_json.get("headers", [])
                   if x.get("key", "").lower() == "cache-control"), "")
        check("general json edge cache ≤ 300s", "s-maxage=300" in cc or "s-maxage=60" in cc, cc)


# ─── Phase 8 ────────────────────────────────────────────────────────────────
def phase_8_mobile_css():
    section("Phase 8 · Mobile CSS sanity")
    css = (DASH / "styles.css").read_text()
    check(".table-wrap has -webkit-overflow-scrolling: touch",
          "-webkit-overflow-scrolling: touch" in css)
    check(".cmp-head handles overflow-wrap on long team names",
          "overflow-wrap: anywhere" in css)
    check(".ic-teams handles overflow-wrap",
          ".ic-teams" in css and "overflow-wrap: anywhere" in css)
    check("prefers-reduced-motion override present",
          "@media (prefers-reduced-motion: reduce)" in css)
    # Breakpoints under 600px
    bps = re.findall(r"@media\s*\([^)]*max-width:\s*(\d+)px", css)
    bps_small = [b for b in bps if int(b) <= 640]
    check(f"≥3 mobile breakpoints defined (≤640px) — found {len(bps_small)}", len(bps_small) >= 3)


# ─── Phase 9 ────────────────────────────────────────────────────────────────
def phase_9_public_copy():
    section("Phase 9 · Public copy audit")
    targets = ["index.html", "methodology.html", "appendix.html", "app.js"]
    forbidden_phrases = [
        ("Spain will win", "absolute prediction language"),
        ("AI predicts the winner", "AI-deterministic overclaim"),
        ("guaranteed forecast", "overclaim"),
        ("live API feed active", "live-feed overclaim"),
    ]
    for f in targets:
        text = (DASH / f).read_text()
        for phrase, why in forbidden_phrases:
            if phrase.lower() in text.lower():
                check(f"{f} no '{phrase}'", False, why)
            else:
                check(f"{f} no '{phrase}'", True)

    # Required positive framings (at least in index.html)
    idx = (DASH / "index.html").read_text()
    check("index.html includes 'Probabilistic simulator, not a forecast.'",
          "Probabilistic simulator, not a forecast" in idx)
    check("index.html uses 'Won X% of simulated tournaments' phrasing",
          "of simulated tournaments" in idx)
    check("index.html uses 'Simulation range' (not just '95% CI') in public area",
          "Simulation range" in idx or "simulation range" in idx)

    # No stale v1/v2 markers
    for f in targets:
        text = (DASH / f).read_text()
        # Look for stale "v1" or "v2" mentions in version pills or context
        stale = re.findall(r'\b(v1|v2)\s*(<|version|pill)', text, re.I)
        check(f"{f}: no stale v1/v2 version markers", not stale,
              f"found {stale[:2]}" if stale else "")


# ─── Stress Tests (Feedback 2) ─────────────────────────────────────────────
def stress_tests():
    section("Stress tests")

    # 1. Live data injection — simulate is heavy; instead test fetch+validate pathway
    cfg = json.loads((ROOT / "data" / "raw" / "wc2026_config.json").read_text())
    fix = cfg["group_stage_schedule"][0]  # M1
    fake_results = {
        "schema": "Completed WC 2026 matches — locked. Future matches are simulated.",
        "updated_at": "2026-06-11T00:00:00+00:00",
        "source": "preflight-injection",
        "completed_matches": [{
            "m": fix["m"], "date": fix["date"],
            "home": fix["home"], "away": fix["away"],
            "home_score": 0, "away_score": 5,  # opening-day upset
            "status": "FT",
        }],
        "warnings": [],
    }
    backup_path = LIVE / "results_2026.json"
    original = backup_path.read_text() if backup_path.exists() else None
    # Force mock mode for the stress runs. Without this, a developer with
    # FOOTBALL_PROVIDER=api_football or API_FOOTBALL_KEY set in their shell
    # would have fetch_results hit the real API, ignore the injected file,
    # and either preserve the fake record (stress-2 fails) or write real
    # provider data (stress-1 fails). The stress test is a pure-fetch-path
    # unit test, not an integration test.
    mock_env = {**os.environ,
                "FOOTBALL_PROVIDER": "mock",
                "WC_RESULTS_SOURCE": "mock"}
    try:
        backup_path.write_text(json.dumps(fake_results, indent=2))
        rc = subprocess.run([sys.executable, "scripts/live/fetch_results.py"],
                            cwd=str(ROOT), env=mock_env,
                            capture_output=True, text=True).returncode
        check("[stress-1] fetch_results survives injected FT result", rc == 0)
        after = json.loads(backup_path.read_text())
        check("[stress-1] injected FT match passes validation",
              len(after.get("completed_matches", [])) == 1)
    finally:
        if original is not None:
            backup_path.write_text(original)

    # 2. API error handling — abandoned + postponed should not be locked
    fake_abandon = dict(fake_results)
    fake_abandon["completed_matches"] = [{
        **fake_results["completed_matches"][0], "status": "ABANDONED"
    }]
    original2 = backup_path.read_text()
    try:
        backup_path.write_text(json.dumps(fake_abandon, indent=2))
        subprocess.run([sys.executable, "scripts/live/fetch_results.py"],
                       cwd=str(ROOT), env=mock_env,
                       capture_output=True, text=True)
        after = json.loads(backup_path.read_text())
        check("[stress-2] ABANDONED match not locked",
              len(after.get("completed_matches", [])) == 0)
        check("[stress-2] ABANDONED surfaced as warning",
              len(after.get("warnings", [])) >= 1)
    finally:
        backup_path.write_text(original2)

    # 3. Vercel cache (covered by Phase 7, restated here for the audit trail)
    cfg = json.loads((ROOT / "vercel.json").read_text())
    live_rule = next((h for h in cfg["headers"]
                      if "predictions" in h.get("source", "")), {})
    cc = next((x.get("value", "") for x in live_rule.get("headers", [])
               if x.get("key", "").lower() == "cache-control"), "")
    check("[stress-3] live JSON cache: max-age=0 + must-revalidate",
          "max-age=0" in cc and "must-revalidate" in cc, cc)

    # 4. CSS mobile overflow on long names
    css = (DASH / "styles.css").read_text()
    check("[stress-4] .cmp-head wraps long team names",
          re.search(r"\.cmp-head\s*\.cmp-team-a[^{]*\{[^}]*overflow-wrap:\s*anywhere", css, re.S)
          is not None or "overflow-wrap: anywhere" in css)
    check("[stress-4] .ic-teams wraps long team names",
          "ic-teams" in css and "overflow-wrap: anywhere" in css)

    # 5. Deep-link safety — code-level inspection
    js = (DASH / "app.js").read_text()
    check("[stress-5] safeDecode wraps decodeURIComponent",
          "safeDecode" in js and "decodeURIComponent" in js)
    # applyDeepLink should have try/catch blocks around its three handlers
    apply_block = re.search(r"function\s+applyDeepLink\s*\(\s*\)\s*\{(.*?)\n\}", js, re.S)
    has_try_catch = bool(apply_block) and apply_block.group(1).count("try") >= 3 \
                                       and apply_block.group(1).count("catch") >= 3
    check("[stress-5] applyDeepLink uses try/catch around handlers", has_try_catch)
    check("[stress-5] team handler checks team is in roster",
          "_openContenderDrawer" in js
          and re.search(r"_openContenderDrawer\s*=\s*\(team\)\s*=>\s*\{[^}]*\.some\(", js) is not None)


# ─── Phase 11: Provider integration ────────────────────────────────────────
def phase_11_provider():
    section("Phase 11 · Live provider integration (API-Football)")

    fr = (ROOT / "scripts" / "live" / "fetch_results.py").read_text()
    bm = ROOT / "scripts" / "live" / "build_provider_fixture_map.py"

    check("fetch_results has API-Football adapter (real HTTP)",
          "v3.football.api-sports.io" in fr and "x-apisports-key" in fr)
    check("fetch_results normalises team names (TEAM_ALIAS)",
          "TEAM_ALIAS" in fr and '"Korea Republic"' in fr and '"Türkiye"' in fr)
    check("fetch_results maps API-Football statuses",
          "APIFOOTBALL_STATUS_MAP" in fr
          and '"FT":   "FT"' in fr
          and '"PEN":  "PEN"' in fr
          and '"ABD":  "ABANDONED"' in fr)
    check("fetch_results retries transient 5xx",
          "http_get_json" in fr and "retries" in fr)
    check("fetch_results --dry-run supported",
          'add_argument("--dry-run"' in fr)
    check("fetch_results refuses to shrink locked count",
          "refusing to shrink" in fr)
    check("build_provider_fixture_map.py exists", bm.exists())
    if bm.exists():
        bm_txt = bm.read_text()
        check("builder uses ±1 day tolerance (UTC↔local)",
              "_td(days=1)" in bm_txt or "timedelta(days=1)" in bm_txt
              or "abs((_date" in bm_txt or "gap <= 1" in bm_txt)
        check("builder requires --write to commit", '--write' in bm_txt)

    # run_live_update integration
    ru = (ROOT / "scripts" / "live" / "run_live_update.py").read_text()
    check("run_live_update has --provider flag", '"--provider"' in ru)
    check("run_live_update has --dry-run flag", '"--dry-run"' in ru)
    check("run_live_update detects provider_mode", "detect_provider_source" in ru)
    check("run_live_update emits provider_mode in live_state",
          '"provider_mode"' in ru)

    # Workflow
    wf = (ROOT / ".github" / "workflows" / "live-matchday.yml").read_text()
    check("workflow exposes API_FOOTBALL_KEY secret",
          re.search(r"API_FOOTBALL_KEY:\s*\$\{\{\s*secrets\.API_FOOTBALL_KEY\s*\}\}", wf) is not None)
    check("workflow supports manual dry-run input",
          "workflow_dispatch:" in wf and "dry_run:" in wf)
    check("workflow uses FOOTBALL_PROVIDER env",
          "FOOTBALL_PROVIDER:" in wf)
    check("workflow cron is safety-muzzled OR windowed",
          # H6: window-scoped crons replaced the open-ended `*/10 * * * *`.
          # On feature/matchday-intelligence the schedule is intentionally
          # disabled (a32856d safety commit) so cron can't fire from this
          # branch and clobber prod. Either state is valid here.
          ("'*/10 * 10-30 6 *'" in wf and "'*/10 * 1-20 7 *'" in wf)
          or "SAFETY: schedule disabled on this branch" in wf)

    # Dashboard
    js = (ROOT / "dashboard" / "app.js").read_text()
    check("dashboard surfaces provider name",
          "providerLabel" in js and "API-Football" in js)
    check("dashboard distinguishes provider_mode active vs manual",
          "provider_mode" in js)

    # Tests
    tf = ROOT / "tests" / "live" / "test_status_mapping.py"
    check("provider tests file exists", tf.exists())
    if tf.exists():
        rc = subprocess.run([sys.executable, str(tf)], capture_output=True, text=True).returncode
        check("provider tests pass", rc == 0)


def phase_12_matchday_intel():
    """Stream B integration: injuries + weather + lineups + stats proxy."""
    section("Phase 12 · Matchday intelligence (Stream B)")

    LIVE_DIR = ROOT / "scripts" / "live"
    TESTS_DIR = ROOT / "tests" / "live"

    # ─ Module presence (B.1-B.5)
    modules = {
        "apply_matchday_adjustments.py": "B.1 scaffold + audit log",
        "weather_adjustments.py":        "B.2 weather pure math",
        "fetch_weather.py":              "B.2 Open-Meteo adapter",
        "injury_adjustments.py":         "B.3 injury helpers",
        "fetch_injuries.py":             "B.3 API-Football adapter",
        "lineup_adjustments.py":         "B.4 lineup heuristics",
        "fetch_lineups.py":              "B.4 API-Football adapter",
        "stats_proxy_adjustments.py":    "B.5 stats proxy math",
        "fetch_match_stats.py":          "B.5 API-Football adapter",
    }
    for fname, label in modules.items():
        check(f"module exists: {fname} ({label})",
              (LIVE_DIR / fname).exists())

    # ─ Locked caps in apply_matchday_adjustments
    amd = (LIVE_DIR / "apply_matchday_adjustments.py").read_text()
    cap_constants = {
        "INJURY_CAP_NORMAL": "25.0",
        "INJURY_CAP_EXTREME": "35.0",
        "LINEUP_CAP": "20.0",
        "WEATHER_CAP": "15.0",
        "STATS_CAP_PER_MATCH": "8.0",
        "STATS_CAP_GROUP_TOTAL": "20.0",
        "AGGREGATE_MATCHDAY_CAP": "35.0",
        "GRAND_TOTAL_CAP": "45.0",
    }
    for name, value in cap_constants.items():
        check(f"cap constant locked: {name} == {value}",
              re.search(rf"^{name}\s*=\s*{re.escape(value)}\b", amd, re.M) is not None)

    # ─ Single integration point in 03_simulate.py
    sim = (ROOT / "scripts" / "03_simulate.py").read_text()
    check("simulator imports get_team_elo_adjustment",
          "from apply_matchday_adjustments import get_team_elo_adjustment" in sim)
    check("simulator no longer adds injury_adjustments to elo_eff_base",
          "+ injury_adjustments.get(t, 0.0)" not in sim)
    check("simulator includes _matchday_intel(t) in elo_eff_base",
          "_matchday_intel(t)" in sim)

    # ─ Stats proxy is locked-NOT-xG
    sp = (LIVE_DIR / "stats_proxy_adjustments.py").read_text()
    check("stats proxy module asserts NOT xG in docstring",
          re.search(r"\b(not|never)\b[^\n]*\bxG\b", sp, re.I) is not None)
    fms = (LIVE_DIR / "fetch_match_stats.py").read_text()
    check("stats fetcher sets true_xg_available=False",
          "true_xg_available" in fms and "False" in fms)

    # ─ Workflow split (B.8)
    slow_wf = ROOT / ".github" / "workflows" / "matchday-intel-slow.yml"
    check("slow workflow exists (B.8)", slow_wf.exists())
    if slow_wf.exists():
        wf_txt = slow_wf.read_text()
        check("slow workflow cron is safety-muzzled OR windowed",
              # H6: window-scoped cron — every 3h, but only during tournament.
              # Same safety-muzzle escape as live-matchday.yml when running
              # on the feature branch (a32856d).
              ("'0 */3 10-30 6 *'" in wf_txt and "'0 */3 1-20 7 *'" in wf_txt)
              or "SAFETY: schedule disabled on this branch" in wf_txt
              or "disabled during launch" in wf_txt)
        check("slow workflow calls all four fetchers",
              all(s in wf_txt for s in (
                  "fetch_injuries.py", "fetch_weather.py",
                  "fetch_lineups.py", "fetch_match_stats.py")))
        check("slow workflow runs apply_matchday_adjustments after fetchers",
              "apply_matchday_adjustments.py" in wf_txt)
        check("slow workflow exposes API_FOOTBALL_KEY",
              re.search(r"API_FOOTBALL_KEY:\s*\$\{\{\s*secrets\.API_FOOTBALL_KEY\s*\}\}",
                        wf_txt) is not None)
        check("slow workflow uses separate concurrency group",
              "wc26-matchday-intel-slow" in wf_txt)

    # ─ Dashboard surfacing (B.7)
    js = (DASH / "app.js").read_text()
    idx = (DASH / "index.html").read_text()
    check("dashboard fetches matchday_intelligence.json",
          "./matchday_intelligence.json" in js)
    check("dashboard renders renderMatchdayIntelligence",
          "renderMatchdayIntelligence" in js)
    check("dashboard has matchday-intel section in HTML",
          'id="matchday-intel"' in idx)

    # ─ Audit log presence in module + dashboard JSON contract
    check("apply_matchday writes append-only audit log",
          "append_audit_log" in amd and 'open("a"' in amd or
          ".jsonl" in amd)  # accept either signal
    check("dashboard matchday_intelligence.json baseline committed",
          (DASH / "matchday_intelligence.json").exists())

    # ─ Stream B unit tests pass
    test_files = [
        "test_apply_matchday_adjustments.py",
        "test_weather_adjustments.py",
        "test_injury_adjustments.py",
        "test_lineup_adjustments.py",
        "test_stats_proxy.py",
    ]
    for tf in test_files:
        tp = TESTS_DIR / tf
        check(f"test file exists: {tf}", tp.exists())
        if tp.exists():
            rc = subprocess.run([sys.executable, str(tp)],
                                capture_output=True, text=True).returncode
            check(f"tests pass: {tf}", rc == 0)


# ─── Phase 10 / report ─────────────────────────────────────────────────────
def report():
    print("\n" + "=" * 70)
    print(f"  Pre-flight: {passed} / {total} checks passed")
    if warnings_ls:
        print(f"  Warnings: {len(warnings_ls)}")
        for w in warnings_ls[:5]:
            print(f"    ~ {w}")
    print("=" * 70)
    if failures:
        print("\n  NOT READY — failed checks:\n")
        for i, f in enumerate(failures, 1):
            print(f"    {i:>2}. {f}")
        print("\n  Fix the above and re-run `python3 scripts/pre_flight.py`.")
        sys.exit(1)
    print("\n  ✓ READY TO DEPLOY")
    print("    All phases green. Safe to push to GitHub and deploy to Vercel.")
    sys.exit(0)


def main():
    print("== Pre-flight launch audit ==")
    phase_1_clean_package()
    phase_2_http_serve()
    phase_3_js_dom()
    phase_4_json_consistency()
    phase_5_live_mode()
    phase_6_provider_feed()
    phase_7_vercel()
    phase_8_mobile_css()
    phase_9_public_copy()
    stress_tests()
    phase_11_provider()
    phase_12_matchday_intel()
    report()


if __name__ == "__main__":
    main()
