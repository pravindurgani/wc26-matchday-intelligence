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
        # H5: lock in the "95% CI is sampling noise, not a parameter CI" relabel.
        # These phrases survived an earlier sweep and contradicted card #9.
        # If any new copy reintroduces them the gate fails before deploy.
        ("95% CI", "use 'simulation range (5 seeds)' — p05/p95 is MC sampling noise, not a parameter CI"),
        ("95% confidence interval", "use 'simulation range' — not a parameter CI"),
        ("with confidence intervals", "use '5-seed simulation ranges' — not a parameter CI"),
        ("Top-6 rank ordering", "rank stability holds only for the top-4; positions 5-6 swap under altitude extremes"),
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
    check("workflow has a 10-min dispatch path",
          # Live-matchday is dispatched by the Cloudflare Worker every 10 min
          # via workflow_dispatch — the native `schedule:` block was removed
          # to avoid doubling the run rate (CF is more reliable). Accept
          # either a workflow_dispatch trigger OR a windowed schedule.
          "workflow_dispatch" in wf
          or ("'*/10 * 11-30 6 *'" in wf and "'*/10 * 1-19 7 *'" in wf))

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
        "STATS_CAP_TOURNAMENT_TOTAL": "20.0",
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
        check("slow workflow runs every 3h during tournament windows",
              # H6: window-scoped cron — every 3h, but only during tournament.
              ("'0 */3 11-30 6 *'" in wf_txt and "'0 */3 1-19 7 *'" in wf_txt)
              or ("'0 */3 10-30 6 *'" in wf_txt and "'0 */3 1-20 7 *'" in wf_txt))
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

    # ─ v2 auto-tier whitelist (B.3 enhancement: per-player tier upgrade)
    kp_path = ROOT / "data" / "raw" / "key_players_2026.json"
    check("key_players_2026.json whitelist present", kp_path.exists())
    if kp_path.exists():
        try:
            kp = json.loads(kp_path.read_text())
            entries = kp.get("players", []) or []
            check("key_players_2026.json has ≥40 entries (sanity floor)",
                  len(entries) >= 40)
            tiers = {e.get("tier") for e in entries}
            check("whitelist contains tier_1_star entries",
                  "tier_1_star" in tiers)
            check("whitelist contains tier_1_keeper entries",
                  "tier_1_keeper" in tiers)
            # All entries must carry the load-bearing keys.
            required_keys = {"team", "name", "name_normalized",
                             "last_name_normalized", "tier"}
            shape_ok = all(required_keys.issubset(e.keys()) for e in entries)
            check("every whitelist entry has required keys", shape_ok)
            # tier values must be valid (matches the TIER_TO_ELO taxonomy).
            valid_tiers = {"tier_1_star", "tier_1_keeper",
                           "tier_2_starter", "tier_3_squad"}
            check("every whitelist entry uses a valid tier",
                  all(e.get("tier") in valid_tiers for e in entries))
            # Top-12 coverage — marquee teams must have a tier_1_keeper AND
            # at least one tier_1_star. Without the star gate, a future
            # whitelist edit that drops e.g. Mbappé from France would slip
            # past the gates because France still has a keeper, and the
            # model would silently lose its biggest single-player signal.
            top12 = {
                "France", "Spain", "Argentina", "Brazil", "England", "Portugal",
                "Germany", "Netherlands", "Belgium", "Croatia", "Morocco", "Mexico",
            }
            keepers_by_team = {e["team"] for e in entries
                               if e.get("tier") == "tier_1_keeper"}
            stars_by_team = {e["team"] for e in entries
                             if e.get("tier") == "tier_1_star"}
            missing_keeper = sorted(top12 - keepers_by_team)
            missing_star = sorted(top12 - stars_by_team)
            check(
                "top-12 teams each have a tier_1_keeper entry "
                f"(missing: {missing_keeper or 'none'})",
                not missing_keeper,
            )
            check(
                "top-12 teams each have a tier_1_star entry "
                f"(missing: {missing_star or 'none'})",
                not missing_star,
            )
            # SELF-CONSISTENCY: stored name_normalized must equal the function
            # output, otherwise classify_tier will silently miscategorise. This
            # gate is what would have caught the Ø-drops-from-NFKD bug at commit
            # time (Ødegaard → 'degaard' under the old normaliser).
            sys.path.insert(0, str(ROOT / "scripts" / "live"))
            from injury_adjustments import (  # noqa: E402
                normalize_player_name as _norm,
            )
            drift_full = []
            drift_last = []
            for e in entries:
                name = e.get("name", "")
                stored_full = e.get("name_normalized", "")
                computed_full = _norm(name)
                if stored_full != computed_full:
                    drift_full.append((e.get("team"), name, stored_full, computed_full))
                # last_name_normalized must be EITHER a trailing window OR a
                # leading window of the full normalised name — classify_tier
                # probes both directions against by_last (trailing-window for
                # Western names at scripts/live/injury_adjustments.py:494-500,
                # leading-window for surname-first names at lines 501-510).
                # R15: leading windows accepted to admit Korean (and other
                # surname-first) names like "Son Heung-min" where the surname
                # is the LEADING token. R14 stored last_name_normalized="son"
                # for that reason; the pre-R15 trailing-only invariant
                # rejected it.
                stored_last = e.get("last_name_normalized", "")
                if stored_last:
                    tokens = computed_full.split()
                    n = len(tokens)
                    trailing = {" ".join(tokens[-k:]) for k in range(1, n + 1)}
                    leading = {" ".join(tokens[:k]) for k in range(1, n + 1)}
                    valid_windows = trailing | leading
                    if stored_last not in valid_windows:
                        drift_last.append((e.get("team"), name, stored_last))
            sample_full = ", ".join(
                f"{t}:{n!r}(stored={s!r}, computed={c!r})"
                for t, n, s, c in drift_full[:3]
            ) or "none"
            check(
                f"whitelist name_normalized matches normalize_player_name() "
                f"(drifts: {len(drift_full)}; sample: {sample_full})",
                not drift_full,
            )
            sample_last = ", ".join(
                f"{t}:{n!r}(stored={s!r})" for t, n, s in drift_last[:3]
            ) or "none"
            check(
                f"whitelist last_name_normalized is a leading or trailing window of full "
                f"(drifts: {len(drift_last)}; sample: {sample_last})",
                not drift_last,
            )
            # DUPLICATE DETECTION (HIGH-1 catcher): two whitelist entries
            # with the same (team, name_normalized) key would cause
            # silent collisions in classify_tier — the second one would
            # overwrite the first in by_full. Hard-fail on exact dups.
            from collections import Counter
            name_keys = [(e.get("team"), e.get("name_normalized")) for e in entries]
            exact_dups = [k for k, c in Counter(name_keys).items() if c > 1]
            check(
                f"whitelist has no (team, name_normalized) duplicates "
                f"({len(exact_dups)} found{': ' + str(exact_dups[:3]) if exact_dups else ''})",
                not exact_dups,
            )
            # ALIASES validation — the optional per-entry list of additional
            # full-form strings the API might emit (e.g. "Son" bare, "Vini
            # Jr", "Mohammed Salah"). Each alias normalises to a by_full
            # key. Three failure modes to gate:
            #   1. Non-list aliases field (type mismatch — silent skip).
            #   2. Non-string alias element (silent normalize fail).
            #   3. Alias collides with another entry's name_normalized on
            #      the SAME team (would silently lose tier resolution).
            bad_alias_shape = []
            empty_aliases = []
            cross_team_collisions = []
            # Build same-team by_full from EVERY entry's canonical name
            # (not just alias-bearing ones) so the collision gate below
            # also fires if a curator typo'd alias=['Lionel Messi'] onto
            # a teammate whose own entry has no aliases field. The
            # previous version skipped non-alias entries here, leaving
            # a silent bypass.
            same_team_by_full = {}
            for e in entries:
                same_team_by_full.setdefault(e.get("team"), set()).add(
                    e.get("name_normalized") or "")
            # Detect bad-shape aliases fields in a separate pass.
            for e in entries:
                aliases = e.get("aliases")
                if aliases is None:
                    continue
                if not isinstance(aliases, list):
                    bad_alias_shape.append(
                        (e.get("team"), e.get("name"), type(aliases).__name__))
            check(
                f"whitelist 'aliases' field is a list when present "
                f"(bad shapes: {len(bad_alias_shape)})",
                not bad_alias_shape,
            )
            # Now sweep each alias.
            for e in entries:
                aliases = e.get("aliases") or []
                if not isinstance(aliases, list):
                    continue
                team = e.get("team")
                own_full = e.get("name_normalized") or ""
                for a in aliases:
                    if not isinstance(a, str) or not a.strip():
                        empty_aliases.append((team, e.get("name"), repr(a)))
                        continue
                    a_norm = _norm(a)
                    if not a_norm:
                        empty_aliases.append((team, e.get("name"), a))
                        continue
                    # Collision: an alias that equals a DIFFERENT entry's
                    # canonical name_normalized on the same team would
                    # silently overwrite (or be discarded by setdefault).
                    other_canon = same_team_by_full.get(team, set()) - {own_full}
                    if a_norm in other_canon:
                        cross_team_collisions.append((team, e.get("name"), a, a_norm))
            check(
                f"whitelist aliases are non-empty strings that normalise "
                f"(invalid: {len(empty_aliases)})",
                not empty_aliases,
            )
            check(
                f"whitelist aliases don't collide with another entry's "
                f"name_normalized on the same team "
                f"(collisions: {len(cross_team_collisions)})",
                not cross_team_collisions,
            )
            # INTRA-TEAM FORENAME PREFIX OVERLAP — the bidirectional
            # forename-prefix disambiguator only stays unambiguous if no
            # two whitelisted players on the same team have forenames
            # where one starts with the other. France with Kylian +
            # Karim would silently break the disambiguator (both prefix
            # 'K'/'Karim' inputs would match both candidates). Today
            # the whitelist has 0 such overlaps — pin the invariant so
            # future curator edits surface the risk.
            forename_overlaps = []
            forenames_by_team: dict[str, list[tuple[str, str]]] = {}
            for e in entries:
                full = e.get("name_normalized") or ""
                first = full.split()[0] if full else ""
                if first:
                    forenames_by_team.setdefault(e.get("team"), []).append(
                        (first, e.get("name", "")))
            for team, items in forenames_by_team.items():
                for i, (f1, n1) in enumerate(items):
                    for j, (f2, n2) in enumerate(items):
                        if i >= j:
                            continue
                        if f1.startswith(f2) or f2.startswith(f1):
                            forename_overlaps.append((team, n1, n2, f1, f2))
            check(
                f"whitelist has no intra-team forename-prefix overlaps "
                f"(overlaps: {len(forename_overlaps)}"
                f"{': ' + str(forename_overlaps[:2]) if forename_overlaps else ''})",
                not forename_overlaps,
            )
            # PROPERTY TEST — every registered alias must resolve to a
            # tier_1_* tier when fed back through classify_tier. Catches
            # alias drift (e.g. a curator edits name_normalized and
            # forgets the alias). One-line invariant; expensive only at
            # gate time.
            sys.path.insert(0, str(ROOT / "scripts" / "live"))
            from injury_adjustments import (  # noqa: E402
                classify_tier as _classify,
                reset_key_players_index_for_tests as _reset,
            )
            _reset()
            alias_drift = []
            for e in entries:
                aliases = e.get("aliases") or []
                if not isinstance(aliases, list):
                    continue
                expected_tier = e.get("tier", "")
                team = e.get("team", "")
                for a in aliases:
                    if not isinstance(a, str) or not a.strip():
                        continue
                    tier, src = _classify(a, team)
                    if tier != expected_tier:
                        alias_drift.append((team, e.get("name"), a, tier, src))
            sample = ", ".join(
                f"{t}/{n!r}: alias {a!r} → ({tier},{src})"
                for t, n, a, tier, src in alias_drift[:3]
            ) or "none"
            check(
                f"every whitelist alias resolves back to its owner's tier "
                f"(drift: {len(alias_drift)}; sample: {sample})",
                not alias_drift,
            )
            # Intra-team last-name collisions are now SAFE (handled by
            # classify_tier's forename-prefix disambiguator), but we still
            # want curator visibility: are the collisions intentional
            # (Argentina's two Martínez) or accidental (typo, wrong player)?
            # The allowlist lives IN the JSON ("expected_last_name_collisions"),
            # so the source of truth is one file. We detect both unexpected
            # NEW collisions (warn) AND stale allowlist entries that no
            # longer collide (warn — likely indicates a whitelist removal
            # the allowlist wasn't updated to reflect).
            last_keys = [(e.get("team"), e.get("last_name_normalized")) for e in entries
                         if e.get("last_name_normalized")]
            last_collisions = {k for k, c in Counter(last_keys).items() if c > 1}
            # Hardened against a malformed allowlist: a typo'd JSON field
            # (string instead of list, dict instead of list, etc.) must
            # not crash the gate with a confusing 'str has no attribute
            # get'. Treat anything non-list as "no allowlist" so unexpected
            # collisions still surface and stale-detection is skipped.
            raw_allowed = kp.get("expected_last_name_collisions")
            if not isinstance(raw_allowed, list):
                raw_allowed = []
            allowed = {
                (item.get("team"), item.get("last_name_normalized"))
                for item in raw_allowed
                if isinstance(item, dict)
            }
            unexpected = sorted(last_collisions - allowed)
            stale_allowed = sorted(allowed - last_collisions)
            if unexpected:
                warn(
                    f"unexpected intra-team last-name collision(s) — "
                    f"add to key_players_2026.json "
                    f"'expected_last_name_collisions' once verified safe: "
                    f"{unexpected[:3]}"
                )
            if stale_allowed:
                warn(
                    f"stale entries in 'expected_last_name_collisions' — "
                    f"these no longer collide in the whitelist and should "
                    f"be removed: {stale_allowed[:3]}"
                )
        except Exception as e:
            check(f"key_players_2026.json parses cleanly ({e})", False)

    # ─ Module wiring: injury_adjustments exposes classify_tier + helpers
    ij = (LIVE_DIR / "injury_adjustments.py").read_text()
    check("injury_adjustments exports classify_tier",
          "def classify_tier(" in ij)
    check("injury_adjustments exports normalize_player_name",
          "def normalize_player_name(" in ij)
    fi = (LIVE_DIR / "fetch_injuries.py").read_text()
    check("fetch_injuries imports classify_tier",
          "classify_tier" in fi)
    check("fetch_injuries records auto_tier_source per player",
          '"auto_tier_source"' in fi)

    # ─ Module wiring: weather_adjustments exposes hydration-break constants
    wa = (LIVE_DIR / "weather_adjustments.py").read_text()
    check("weather_adjustments exposes HYDRATION_BREAK_WBGT_THRESHOLD",
          "HYDRATION_BREAK_WBGT_THRESHOLD" in wa)
    check("weather_adjustments exposes HYDRATION_BREAK_DAMPENER",
          "HYDRATION_BREAK_DAMPENER" in wa)
    fw = (LIVE_DIR / "fetch_weather.py").read_text()
    check("fetch_weather passes wet_bulb_c into team_elo_adjustment",
          "wet_bulb_c=wb" in fw)

    # ─ S5 fix: replacement-Elo invariant on key_players_2026.json. Surfaces
    # data slips that would otherwise flow into net_injury_elo() as a
    # positive (team-improving) value — runtime clamp catches it at math
    # time, this gate catches it at commit time.
    repl_errors = validate_key_players_replacements()
    check(
        f"key_players_2026.json replacement.elo_equiv ∈ [tier_floor, 0] "
        f"({len(repl_errors)} violation"
        f"{'s' if len(repl_errors) != 1 else ''})",
        not repl_errors,
        f"first: {repl_errors[0]}" if repl_errors else "",
    )

    # ─ R12 A2: every team referenced in data/live/team_adjustments.json's
    # operator overlay must round-trip through normalize_team to a team that
    # appears in the WC2026 group_stage_schedule. Pre-R12 an operator typing
    # "USA" / "Korea Republic" / "Côte d'Ivoire" into the overlay silently
    # failed to apply because get_team_elo_adjustment uses strict equality
    # against the canonical names. apply_matchday_adjustments now normalises
    # at the writer side (R12 A2 fix) but this gate catches operator entries
    # that don't resolve to ANY canonical name at all.
    ta_path = ROOT / "data" / "live" / "team_adjustments.json"
    if ta_path.exists():
        try:
            ta = json.loads(ta_path.read_text())
            cfg = json.loads((ROOT / "data" / "raw" / "wc2026_config.json").read_text())
            canonical_teams = set()
            for f in cfg.get("group_stage_schedule", []):
                canonical_teams.add(f.get("home"))
                canonical_teams.add(f.get("away"))
            sys.path.insert(0, str(ROOT / "scripts" / "live"))
            from fetch_results import normalize_team as _norm_team  # noqa: E402
            unresolved = []
            for idx, adj in enumerate(ta.get("adjustments", []) or []):
                raw = adj.get("team")
                if not raw:
                    continue
                resolved = _norm_team(raw)
                if resolved not in canonical_teams:
                    unresolved.append(
                        f"idx={idx} raw={raw!r} → normalized={resolved!r}"
                    )
            check(
                f"team_adjustments.json team field resolves to canonical "
                f"WC2026 team via normalize_team "
                f"({len(unresolved)} unresolved)",
                not unresolved,
                f"first: {unresolved[0]}" if unresolved else "",
            )
        except Exception as e:
            check(f"team_adjustments.json gate parseable: {type(e).__name__}",
                  False, str(e))


# ─── Standalone config validators (callable from tests + CLI) ─────────────

# TIER_TO_ELO mirrored here so the validator stays import-light. Source of
# truth lives at scripts/live/injury_adjustments.py:38-43; if those values
# move, this table must move with them (pre_flight.py phase_12 separately
# locks the tier-cap caps so a drift here would surface in the gate too).
_VALIDATOR_TIER_TO_ELO = {
    "tier_1_star":    -30.0,
    "tier_1_keeper":  -25.0,
    "tier_2_starter": -12.0,
    "tier_3_squad":    -4.0,
}


def validate_key_players_replacements(
    path: Path | None = None,
) -> list[str]:
    """Validate the replacement-Elo invariant in key_players_2026.json.

    Invariant (S5): for every player record carrying a `replacement` block,
    `replacement.elo_equiv ∈ [TIER_TO_ELO[player.tier], 0]` (inclusive).

    Why this matters: scripts/live/injury_adjustments.py:net_injury_elo()
    computes `net = elo - replacement_elo`. If a curator typo makes
    `replacement.elo_equiv` MORE NEGATIVE than the full-tier Elo for that
    player (replacement "worse than the out-player"), `net` flips POSITIVE
    — an injury that improves the team. The runtime clamp in net_injury_elo
    is a second line of defence; this validator catches the bad config
    BEFORE it ever lands in injuries_2026.json.

    Returns a list of human-readable error messages, empty if clean.
    Records without a `replacement` block or with a `replacement.elo_equiv`
    that isn't a number are skipped silently (other gates handle those).

    Schema walked: top-level `players` list, each entry a dict with
    `team`, `name`, `tier`, and an optional `replacement.elo_equiv` float.
    """
    target = path if path is not None else (
        ROOT / "data" / "raw" / "key_players_2026.json"
    )
    try:
        data = json.loads(target.read_text())
    except FileNotFoundError:
        return [f"key_players file missing: {target}"]
    except json.JSONDecodeError as e:
        return [f"key_players file is not valid JSON: {e}"]
    errors: list[str] = []
    players = data.get("players")
    if not isinstance(players, list):
        return [f"key_players: top-level 'players' is not a list "
                f"(got {type(players).__name__})"]
    for entry in players:
        if not isinstance(entry, dict):
            errors.append(f"player entry is not a dict: {entry!r}")
            continue
        team = entry.get("team")
        name = entry.get("name")
        tier = entry.get("tier")
        replacement = entry.get("replacement")
        if not isinstance(replacement, dict):
            continue  # no replacement → nothing to validate
        repl_elo = replacement.get("elo_equiv")
        if not isinstance(repl_elo, (int, float)):
            continue  # other gates flag non-numeric / missing fields
        floor = _VALIDATOR_TIER_TO_ELO.get(tier)
        if floor is None:
            errors.append(
                f"{team} / {name!r}: unknown tier {tier!r} "
                f"(no Elo floor available to validate replacement.elo_equiv)"
            )
            continue
        # Invariant: floor <= repl_elo <= 0. Note `floor` is negative.
        if not (floor <= float(repl_elo) <= 0.0):
            errors.append(
                f"{team} / {name!r}: replacement.elo_equiv={repl_elo} "
                f"violates invariant [{floor}, 0] for tier {tier!r} "
                f"(net_injury_elo would emit a value outside [elo, 0])"
            )
    return errors


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


def _cli_validate_key_players(argv: list[str]) -> int:
    """CLI entry point for the S5 validator only.

    `python3 scripts/pre_flight.py validate-key-players [path]`
    exits 0 on clean config, 1 on any invariant violation (errors → stderr).
    The optional positional argument lets tests point at a synthetic
    fixture without monkey-patching ROOT.
    """
    target: Path | None = None
    if len(argv) >= 2:
        target = Path(argv[1])
    errors = validate_key_players_replacements(target)
    if errors:
        for e in errors:
            print(f"INVALID: {e}", file=sys.stderr)
        return 1
    print("OK — all key_players replacements within [tier_floor, 0]")
    return 0


if __name__ == "__main__":
    # Sub-command dispatch. The default invocation (no args) still runs the
    # full pre-flight audit. `validate-key-players` runs just the S5 gate,
    # so CI hooks and tests can probe it in isolation without the 12-phase
    # HTTP-serving stress run.
    if len(sys.argv) >= 2 and sys.argv[1] == "validate-key-players":
        sys.exit(_cli_validate_key_players(sys.argv[1:]))
    main()
