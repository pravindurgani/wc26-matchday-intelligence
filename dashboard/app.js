/* World Cup 2026 Simulator — reads everything from JSON, no hardcoded values. */

const CONFED = {
  "Argentina":"CONMEBOL","Brazil":"CONMEBOL","Colombia":"CONMEBOL","Uruguay":"CONMEBOL",
  "Ecuador":"CONMEBOL","Paraguay":"CONMEBOL",
  "England":"UEFA","France":"UEFA","Spain":"UEFA","Portugal":"UEFA","Germany":"UEFA",
  "Netherlands":"UEFA","Belgium":"UEFA","Croatia":"UEFA","Switzerland":"UEFA",
  "Norway":"UEFA","Sweden":"UEFA","Austria":"UEFA","Czechia":"UEFA","Scotland":"UEFA",
  "Italy":"UEFA","Bosnia and Herzegovina":"UEFA","Turkey":"UEFA",
  "United States":"CONCACAF","Mexico":"CONCACAF","Canada":"CONCACAF","Panama":"CONCACAF",
  "Haiti":"CONCACAF","Curacao":"CONCACAF",
  "Morocco":"CAF","Egypt":"CAF","Senegal":"CAF","Ivory Coast":"CAF","Tunisia":"CAF",
  "Algeria":"CAF","DR Congo":"CAF","Cape Verde":"CAF","Ghana":"CAF","South Africa":"CAF",
  "Japan":"AFC","South Korea":"AFC","Iran":"AFC","Australia":"AFC","Saudi Arabia":"AFC",
  "Qatar":"AFC","Iraq":"AFC","Jordan":"AFC","Uzbekistan":"AFC","New Zealand":"OFC",
};

const fmt   = x => `${(x*100).toFixed(1)}%`;
const fmt0  = x => `${(x*100).toFixed(0)}%`;
const fmtNum = n => n == null ? "—" : Math.round(n).toLocaleString();
const escapeHtml = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const confedColorVar = team => {
  const c = CONFED[team];
  if (!c) return '--text-3';
  return ({UEFA:'--confed-uefa', CONMEBOL:'--confed-conmebol', CAF:'--confed-caf',
           CONCACAF:'--confed-concacaf', AFC:'--confed-afc', OFC:'--confed-ofc'})[c] || '--text-3';
};
const confedDotHtml = team =>
  `<span class="confed-dot" style="background: var(${confedColorVar(team)})" title="${CONFED[team] || ''}"></span>`;

// ---- THEME ----
document.getElementById('theme-toggle').addEventListener('click', () => {
  const cur = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  document.documentElement.dataset.theme = cur;
  localStorage.setItem('wc26-theme', cur);
  if (window._charts) window._charts.forEach(c => c.destroy());
  window._charts = [];
  if (window._data) renderAllCharts(window._data, window._cal);
});

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// ---- TOOLTIPS for .stat-info ----
function initTooltips() {
  let tip;
  function show(target, text) {
    if (!tip) {
      tip = document.createElement('div');
      tip.className = 'global-tooltip';
      document.body.appendChild(tip);
    }
    tip.textContent = text;
    tip.style.opacity = '1';
    const r = target.getBoundingClientRect();
    tip.style.left = Math.min(window.innerWidth - 320, Math.max(8, r.left + r.width/2 - 150)) + 'px';
    tip.style.top  = (r.bottom + window.scrollY + 8) + 'px';
  }
  function hide() { if (tip) tip.style.opacity = '0'; }
  document.querySelectorAll('.stat-info').forEach(el => {
    const txt = el.dataset.tip;
    if (!txt) return;
    el.addEventListener('mouseenter', () => show(el, txt));
    el.addEventListener('focus',      () => show(el, txt));
    el.addEventListener('mouseleave', hide);
    el.addEventListener('blur',       hide);
  });
}

// ---- Count-up animation ----
function countUp(el, target, suffix = '', duration = 900) {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    el.textContent = `${target}${suffix}`;
    return;
  }
  const start = performance.now();
  function tick(now) {
    const t = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - t, 3);
    const val = target * eased;
    el.textContent = (target >= 100 ? Math.round(val).toLocaleString() : val.toFixed(1)) + suffix;
    if (t < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

// ---- Boot ----
async function init() {
  const buster = `?t=${Date.now()}`;
  const fetchOptional = (url) => fetch(url + buster).then(r => r.ok ? r.json() : null).catch(() => null);
  const [data, cal, wf, abl, travel, liveState, liveDelta, livePred] = await Promise.all([
    fetch('./predictions.json' + buster).then(r => r.json()),
    fetchOptional('./calibration.json'),
    fetchOptional('./walk_forward.json'),
    fetchOptional('./ablation.json'),
    fetchOptional('./travel_impact.json'),
    fetchOptional('./live_state.json'),
    fetchOptional('./live_delta.json'),
    fetchOptional('./predictions_live.json'),
  ]);
  window._data = data;
  window._cal = cal;
  window._travel = travel;
  window._liveDelta = liveDelta;
  window._charts = [];

  initTooltips();
  renderLastUpdated(data, liveState);
  renderLiveStatusBar(liveState);
  renderHero(data, liveState, liveDelta);
  renderStatsStrip(data, cal);
  renderStorylines(data, travel);
  renderMovers(data, liveState, liveDelta);
  renderContenders(data, liveDelta, travel);
  renderGroups(data);
  renderInteresting(data);
  renderMatches(data, liveState);
  renderCompare(data, travel);
  renderAllCharts(data, cal);
  if (wf) renderWalkForward(wf);
  if (abl) renderAblation(abl);
  if (travel) renderTravel(travel);
  renderFooter(data, liveState);

  // Apply deep link AFTER renders settle
  applyDeepLink();
  window.addEventListener('hashchange', applyDeepLink);

  // Start live polling — fetches live_state/live_delta/predictions_live on
  // an interval and re-renders only the live-dependent sections when the
  // server-side `last_updated_utc` changes. Pauses while the tab is hidden.
  startLivePolling();
}

// Re-fetch the three live JSON files. Returns the new triple if any of them
// changed vs the in-memory snapshot, else null.
async function fetchLiveTriple() {
  const buster = `?t=${Date.now()}`;
  const fetchOptional = (url) =>
    fetch(url + buster, { cache: 'no-store' })
      .then(r => r.ok ? r.json() : null)
      .catch(() => null);
  const [liveState, liveDelta, livePred] = await Promise.all([
    fetchOptional('./live_state.json'),
    fetchOptional('./live_delta.json'),
    fetchOptional('./predictions_live.json'),
  ]);
  return { liveState, liveDelta, livePred };
}

function applyLiveUpdate({ liveState, liveDelta, livePred }) {
  const data = window._data;
  const travel = window._travel;
  const cal = window._cal;
  window._liveDelta = liveDelta;
  window._livePred = livePred;
  renderLastUpdated(data, liveState);
  renderLiveStatusBar(liveState);
  renderHero(data, liveState, liveDelta);
  renderMovers(data, liveState, liveDelta);
  renderContenders(data, liveDelta, travel);
  renderMatches(data, liveState);
  renderFooter(data, liveState);
}

let _livePollTimer = null;
let _lastLiveTimestamp = null;

function startLivePolling(intervalMs = 60_000) {
  // Seed last-seen ts from whatever was loaded at boot.
  _lastLiveTimestamp =
    document.querySelector('#last-updated')?.getAttribute('title') || null;

  const tick = async () => {
    if (document.hidden) return;          // pause when tab not visible
    const triple = await fetchLiveTriple();
    if (!triple.liveState) return;
    const ts = triple.liveState.last_updated_utc;
    if (ts && ts === _lastLiveTimestamp) return;   // nothing new
    _lastLiveTimestamp = ts;
    applyLiveUpdate(triple);
  };

  if (_livePollTimer) clearInterval(_livePollTimer);
  _livePollTimer = setInterval(tick, intervalMs);

  // Refresh immediately when tab regains focus — avoids waiting for the
  // next tick after returning from another app.
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) tick();
  });
}

function renderAllCharts(data, cal) {
  renderTitleChart(data);
  renderConfedChart(data);
  renderFeatureChart(data);
  if (cal) renderCalibration(cal);
}

function renderLastUpdated(data, liveState) {
  const el = document.getElementById('last-updated');
  if (!el) return;
  const ts = liveState?.last_updated_utc || data.generated_at || '';
  if (!ts) { el.textContent = ''; return; }
  const d = new Date(ts);
  const opts = { hour: '2-digit', minute: '2-digit', day: '2-digit', month: 'short', timeZone: 'UTC' };
  el.textContent = `Updated ${d.toLocaleString('en-GB', opts)} UTC`;
  el.title = ts;
}

function providerLabel(source) {
  const s = (source || '').toLowerCase();
  if (s === 'api_football')      return 'API-Football (live)';
  if (s === 'football_data')     return 'football-data.org (live)';
  if (s === 'sportmonks')        return 'Sportmonks (live)';
  if (s === 'manual/mock' || s === 'mock' || !s) return 'manual / mock';
  return source;
}

function renderLiveStatusBar(liveState) {
  if (!liveState) return;
  const banner = document.getElementById('live-status');
  if (!banner) return;
  const isLive = liveState.mode === 'live';
  const providerActive = liveState.provider_mode === 'active';
  banner.classList.toggle('is-live', isLive);
  banner.classList.toggle('is-pre', !isLive);
  banner.innerHTML = `
    <span class="live-dot" aria-hidden="true"></span>
    <span class="live-mode">${isLive ? 'Live-adjusted' : 'Pre-tournament static'}</span>
    <span class="live-meta">
      ${liveState.completed_matches_count} of 104 matches locked ·
      provider: ${escapeHtml(providerLabel(liveState.source))}${providerActive ? '' : ' (no live API key configured)'}${
        isLive ? '' : ' · live updates activate once kickoff begins on 11 Jun 2026'
      }
    </span>
  `;
}

function renderHero(data, liveState, liveDelta) {
  const top = data.team_predictions[0];
  document.getElementById('champ-team').innerHTML = `${confedDotHtml(top.team)}${escapeHtml(top.team)}`;
  countUp(document.getElementById('champ-prob'), +(top.p_champion * 100).toFixed(1), '%');
  document.getElementById('champ-ci').textContent =
    top.p_champion_p05 != null
      ? `Simulation range: ${fmt(top.p_champion_p05)} – ${fmt(top.p_champion_p95)}`
      : '';

  const finalLeader = [...data.team_predictions].sort((a,b) => b.p_reach_final - a.p_reach_final)[0];
  document.getElementById('final-team').innerHTML = `${confedDotHtml(finalLeader.team)}${escapeHtml(finalLeader.team)}`;
  document.getElementById('final-prob').textContent = `${fmt(finalLeader.p_reach_final)} to reach the final`;

  const dh = darkHorse(data);
  document.getElementById('dh-team').innerHTML = `${confedDotHtml(dh.team)}${escapeHtml(dh.team)}`;
  document.getElementById('dh-prob').textContent = `${fmt(dh.p_reach_sf)} reach SF · Elo ${Math.round(dh.elo)}`;

  const isLive = liveState?.mode === 'live';
  document.getElementById('mode-label').textContent = isLive ? 'Live-adjusted' : 'Pre-tournament';
  document.getElementById('mode-sub').textContent = isLive
    ? `${liveState.completed_matches_count} of 104 matches locked`
    : 'Pre-kickoff · 0 of 104 matches locked';
}

function darkHorse(data) {
  const top8 = new Set([...data.team_predictions].sort((a,b) => b.elo - a.elo).slice(0,8).map(t => t.team));
  return data.team_predictions.filter(t => !top8.has(t.team)).sort((a,b) => b.p_reach_sf - a.p_reach_sf)[0];
}

function renderStatsStrip(data, cal) {
  const total = data.n_simulations_total || data.n_simulations || 0;
  countUp(document.getElementById('stat-sims'), total);
  document.getElementById('stat-sims-sub').textContent =
    `${data.n_seeds || 1} seeds × ${(data.n_simulations_per_seed || total).toLocaleString()}`;

  const m = data.model_metrics || {};
  document.getElementById('stat-logloss').textContent =
    m.implied_wdl_log_loss ? m.implied_wdl_log_loss.toFixed(3) : '—';

  if (cal?.holdout) {
    document.getElementById('stat-acc').textContent = `${(cal.holdout.accuracy*100).toFixed(1)}%`;
  } else { document.getElementById('stat-acc').textContent = '—'; }

  const c = data.concentration || {};
  const top1 = (c.top1_champion_p || 0) * 100;
  document.getElementById('stat-top1').textContent = `${top1.toFixed(1)}%`;
  document.getElementById('stat-misses').textContent = `${data.annex_c_misses || 0}`;
}

// ---- STORYLINES ----
function renderStorylines(data, travel) {
  const grid = document.getElementById('storylines-grid');
  if (!grid) return;

  const teams = data.team_predictions;
  const fav = teams[0];
  const dh = darkHorse(data);

  const byGroup = {};
  teams.forEach(t => (byGroup[t.group] = byGroup[t.group] || []).push(t));
  let toughest = null, hi = -1;
  for (const [g, ts] of Object.entries(byGroup)) {
    const advs = ts.map(t => t.p_advance_groups);
    const sum = advs.reduce((a,b) => a+b, 0);
    const ent = -advs.map(p => p/sum).reduce((a,p) => a + (p > 0 ? p * Math.log(p) : 0), 0);
    if (ent > hi) { hi = ent; toughest = { group: g, teams: ts.slice().sort((a,b) => b.p_advance_groups - a.p_advance_groups) }; }
  }

  const km = (travel?.total_group_travel_km_by_team) || {};
  let travelTop = ['—', 0];
  for (const [t, k] of Object.entries(km)) if (k > travelTop[1]) travelTop = [t, k];

  const candidates = teams.slice(1, 15).filter(t => t.p_champion_p05 != null);
  let volatile = null, widest = -1;
  for (const t of candidates) {
    const w = (t.p_champion_p95 - t.p_champion_p05) * 100;
    if (w > widest) { widest = w; volatile = t; }
  }

  const icon = svg =>
    `<svg class="sl-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${svg}</svg>`;

  const cards = [
    {
      icon: icon(`<path d="M6 9H4.5a2.5 2.5 0 0 1 0-5H6"/><path d="M18 9h1.5a2.5 2.5 0 0 0 0-5H18"/><path d="M4 22h16"/><path d="M10 14.66V17c0 .55-.47.98-.97 1.21C7.85 18.75 7 20.24 7 22"/><path d="M14 14.66V17c0 .55.47.98.97 1.21C16.15 18.75 17 20.24 17 22"/><path d="M18 2H6v7a6 6 0 0 0 12 0V2Z"/>`),
      label: 'Strongest favourite', team: fav.team,
      stat: `Won ${fmt(fav.p_champion)} of simulations · Elo ${Math.round(fav.elo)}`,
      link: `#team=${encodeURIComponent(fav.team)}`,
    },
    {
      icon: icon(`<path d="M3 12h18"/><path d="m13 5 7 7-7 7"/>`),
      label: 'Dark horse', team: dh.team,
      stat: `Reaches SF in ${fmt(dh.p_reach_sf)} of simulations`,
      link: `#team=${encodeURIComponent(dh.team)}`,
    },
    {
      icon: icon(`<circle cx="12" cy="12" r="10"/><path d="m8 12 3 3 5-6"/>`),
      label: `Toughest group · ${toughest.group}`,
      team: toughest.teams.slice(0, 2).map(t => t.team).join(' / '),
      stat: `Top 2 advance: ${fmt0(toughest.teams[0].p_advance_groups)} / ${fmt0(toughest.teams[1].p_advance_groups)}`,
      link: `#group=${encodeURIComponent(toughest.group)}`,
    },
    {
      icon: icon(`<path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"/>`),
      label: 'Biggest travel burden', team: travelTop[0],
      stat: `${Math.round(travelTop[1]).toLocaleString()} km in group stage`,
      link: `#team=${encodeURIComponent(travelTop[0])}`,
    },
    {
      icon: icon(`<path d="M3 3v18h18"/><path d="M7 12V6"/><path d="M11 16V9"/><path d="M15 12V8"/><path d="M19 18v-7"/>`),
      label: 'Most volatile contender', team: volatile ? volatile.team : '—',
      stat: volatile ? `Won ${fmt(volatile.p_champion)} of sims · ${widest.toFixed(1)}pp range` : '—',
      link: volatile ? `#team=${encodeURIComponent(volatile.team)}` : '#contenders',
    },
  ];

  grid.innerHTML = cards.map(c => `
    <a class="storyline-card reveal" href="${c.link}">
      <div style="display: flex; align-items: center; gap: 8px">
        ${c.icon}
        <span class="sl-label">${escapeHtml(c.label)}</span>
      </div>
      <div class="sl-team">${c.team !== '—' ? confedDotHtml(c.team) : ''}${escapeHtml(c.team)}</div>
      <div class="sl-stat">${escapeHtml(c.stat)}</div>
    </a>
  `).join('');
}

// ---- BIGGEST MOVERS ----
function renderMovers(data, liveState, liveDelta) {
  const root = document.getElementById('movers-content');
  if (!root) return;
  const isLive = liveState?.mode === 'live';
  const movers = (liveDelta?.all_movers || []);

  if (!isLive || movers.length === 0) {
    root.innerHTML = `
      <div class="movers-empty">
        <div class="movers-empty-icon" aria-hidden="true">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
        </div>
        <div>
          <strong>No matches have finished yet.</strong>
          <span class="muted small"> Champion-probability deltas will appear here automatically as matches lock during the tournament. The simulator re-runs every 15 minutes during the matchday window (11 Jun → 19 Jul 2026).</span>
        </div>
      </div>`;
    return;
  }

  const ups = movers.filter(m => m.delta_pp > 0).slice(0, 6);
  const downs = movers.filter(m => m.delta_pp < 0).slice(0, 6);
  root.innerHTML = `
    <div class="movers-grid">
      <div class="movers-col">
        <h4 class="movers-h">▲ Climbers</h4>
        <ul class="movers-list">${ups.map(m =>
          `<li><span class="m-team">${confedDotHtml(m.team)}${escapeHtml(m.team)}</span>
               <span class="m-now">${fmt(m.live)}</span>
               <span class="m-delta pos">+${m.delta_pp.toFixed(1)}pp</span></li>`).join('') || '<li class="muted small">No upward movers yet.</li>'}</ul>
      </div>
      <div class="movers-col">
        <h4 class="movers-h">▼ Faders</h4>
        <ul class="movers-list">${downs.map(m =>
          `<li><span class="m-team">${confedDotHtml(m.team)}${escapeHtml(m.team)}</span>
               <span class="m-now">${fmt(m.live)}</span>
               <span class="m-delta neg">${m.delta_pp.toFixed(1)}pp</span></li>`).join('') || '<li class="muted small">No downward movers yet.</li>'}</ul>
      </div>
    </div>`;
}

// ---- CONTENDERS ----
function renderContenders(data, liveDelta, travel) {
  const tbody = document.querySelector('#contenders-table tbody');
  const all = data.team_predictions;
  const maxP = all[0].p_champion;
  const countEl = document.getElementById('contenders-count');
  const groupSel = document.getElementById('team-group');
  const regionSel = document.getElementById('team-region');
  const searchEl = document.getElementById('team-search');
  const resetBtn = document.getElementById('contenders-reset');
  const headerCells = document.querySelectorAll('#contenders-table thead th[data-sort]');

  [...new Set(all.map(t => t.group))].sort().forEach(g => {
    const o = document.createElement('option'); o.value = g; o.textContent = `Group ${g}`;
    if (groupSel) groupSel.appendChild(o);
  });

  const deltaMap = {};
  if (liveDelta?.all_movers) liveDelta.all_movers.forEach(m => deltaMap[m.team] = m.delta_pp);
  const travelKmByTeam = (travel?.total_group_travel_km_by_team) || {};
  const travelDelta = {};
  (travel?.all_diffs || []).forEach(d => travelDelta[d.team] = d.delta_pp);

  const expanded = new Set();
  let sortKey = 'p_champion';
  let sortDir = 'desc';

  function sortFn(a, b) {
    if (sortKey === 'rank') return all.indexOf(a) - all.indexOf(b);
    if (sortKey === 'team') return a.team.localeCompare(b.team) * (sortDir === 'asc' ? 1 : -1);
    if (sortKey === 'group') return a.group.localeCompare(b.group) * (sortDir === 'asc' ? 1 : -1);
    const va = a[sortKey], vb = b[sortKey];
    if (va == null) return 1;
    if (vb == null) return -1;
    return (vb - va) * (sortDir === 'desc' ? 1 : -1);
  }

  function rowHtml(t, displayIdx, opensDrawer) {
    const ciTxt = t.p_champion_p05 != null
      ? `<span class="ci">Sim range ${fmt(t.p_champion_p05)} – ${fmt(t.p_champion_p95)}</span>` : '';
    const w = Math.min(100, maxP > 0 ? (t.p_champion / maxP) * 100 : 0).toFixed(1);
    const delay = (displayIdx * 0.02).toFixed(2);
    const delta = deltaMap[t.team];
    const deltaHtml = delta != null && Math.abs(delta) > 0.01
      ? `<span class="row-delta ${delta > 0 ? 'pos' : 'neg'}">${delta > 0 ? '+' : ''}${delta.toFixed(1)}pp</span>` : '';
    const isOpen = expanded.has(t.team);
    const expander = opensDrawer
      ? `<svg class="row-expander" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>` : '';
    const rank = all.indexOf(t) + 1;
    return `<tr class="team-row${opensDrawer ? ' is-expandable' : ''}${isOpen ? ' is-open' : ''}" data-team="${escapeHtml(t.team)}">
      <td>${rank}</td>
      <td>${expander}${confedDotHtml(t.team)}<strong>${escapeHtml(t.team)}</strong>${deltaHtml}</td>
      <td class="hide-sm">${escapeHtml(t.group)}</td>
      <td class="num hide-md">${Math.round(t.elo)}</td>
      <td class="num hide-sm">${t.fifa_pts ? t.fifa_pts.toFixed(0) : '—'}</td>
      <td class="num hide-sm">${fmtNum(t.squad_value_eur_m)}</td>
      <td class="num">${fmt0(t.p_advance_groups)}</td>
      <td class="num hide-sm">${fmt0(t.p_reach_r16)}</td>
      <td class="num">${fmt0(t.p_reach_qf)}</td>
      <td class="num">${fmt0(t.p_reach_sf)}</td>
      <td class="num">${fmt0(t.p_reach_final)}</td>
      <td class="num hide-sm">${fmt(t.p_third_place || 0)}</td>
      <td class="num gold bar-cell" style="--bar-w:${w}%; --bar-delay:${delay}s"><span>${fmt(t.p_champion)}${ciTxt}</span></td>
    </tr>`;
  }

  function drawerHtml(t) {
    const km = travelKmByTeam[t.team];
    const td  = travelDelta[t.team];
    const groupTeams = all.filter(x => x.group === t.group);
    const groupStrength = groupTeams.map(x => x.elo).reduce((a,b)=>a+b,0) / groupTeams.length;
    const rank = all.findIndex(x => x.team === t.team) + 1;
    const ciWidth = t.p_champion_p05 != null ? (t.p_champion_p95 - t.p_champion_p05) * 100 : null;
    const chips = [];
    if (rank <= 5) chips.push(`top-${rank} pre-tournament`);
    if (t.elo > 2100) chips.push(`elite Elo (${Math.round(t.elo)})`);
    if (t.p_advance_groups > 0.85) chips.push(`almost-certain qualifier`);
    if (t.p_advance_groups < 0.6) chips.push(`group exit risk`);
    if (groupStrength > 1900) chips.push(`hard group (avg Elo ${Math.round(groupStrength)})`);
    if (td != null && td > 0.2) chips.push(`+${td.toFixed(2)}pp from travel asymmetry`);
    if (td != null && td < -0.2) chips.push(`${td.toFixed(2)}pp travel drag`);
    if (ciWidth != null && ciWidth < 1) chips.push(`narrow simulation range`);
    if (ciWidth != null && ciWidth > 2) chips.push(`wide simulation range — estimate seed-sensitive`);
    const narrative = chips.length
      ? `<div class="drawer-narrative"><strong>Why ${escapeHtml(t.team)}:</strong> ${chips.map(escapeHtml).join(' · ')}.</div>`
      : '';
    const teamUrl = `#team=${encodeURIComponent(t.team)}`;
    return `<tr class="team-drawer-row is-open" data-drawer-for="${escapeHtml(t.team)}"><td colspan="13">
      <div class="team-drawer">
        <div class="drawer-metric">
          <span class="dm-label">Champion</span>
          <span class="dm-value">${fmt(t.p_champion)}</span>
          <span class="dm-sub">${t.p_champion_p05 != null ? `Sim range ${fmt(t.p_champion_p05)} – ${fmt(t.p_champion_p95)}` : '—'}</span>
        </div>
        <div class="drawer-metric">
          <span class="dm-label">Elo (effective)</span>
          <span class="dm-value">${Math.round(t.elo)}</span>
          <span class="dm-sub">${CONFED[t.team] || '—'}</span>
        </div>
        <div class="drawer-metric">
          <span class="dm-label">Group ${escapeHtml(t.group)} finish</span>
          <span class="dm-value">${fmt0(t.p_finish_1st_group)} / ${fmt0(t.p_finish_2nd_group)}</span>
          <span class="dm-sub">1st / 2nd · ${fmt0(t.p_advance_groups)} advance</span>
        </div>
        <div class="drawer-metric">
          <span class="dm-label">Path strength</span>
          <span class="dm-value">${fmt0(t.p_reach_qf)} → ${fmt0(t.p_reach_sf)} → ${fmt0(t.p_reach_final)}</span>
          <span class="dm-sub">QF · SF · Final</span>
        </div>
        <div class="drawer-metric">
          <span class="dm-label">Squad value</span>
          <span class="dm-value">€${fmtNum(t.squad_value_eur_m)}M</span>
          <span class="dm-sub">FIFA pts ${t.fifa_pts ? t.fifa_pts.toFixed(0) : '—'}</span>
        </div>
        ${km != null ? `
        <div class="drawer-metric">
          <span class="dm-label">Group travel</span>
          <span class="dm-value">${Math.round(km).toLocaleString()} km</span>
          <span class="dm-sub">${td != null ? `Δ${td >= 0 ? '+' : ''}${td.toFixed(2)}pp on title` : '—'}</span>
        </div>` : ''}
        <div class="drawer-actions">
          <button class="btn-ghost" data-copy="${escapeHtml(teamUrl)}">Copy share link</button>
          <a class="btn-ghost" href="${teamUrl}">Permalink ↗</a>
        </div>
        ${narrative}
      </div>
    </td></tr>`;
  }

  let showAll = false;
  const btn = document.getElementById('toggle-all-teams');

  function paint() {
    const q = (searchEl?.value || '').trim().toLowerCase();
    const g = (groupSel?.value || 'all');
    const r = (regionSel?.value || 'all');
    let filtered = all.filter(t => {
      if (q && !t.team.toLowerCase().includes(q)) return false;
      if (g !== 'all' && t.group !== g) return false;
      if (r !== 'all' && (CONFED[t.team] || 'Other') !== r) return false;
      return true;
    });

    filtered = filtered.slice().sort(sortFn);

    const filterActive = q || g !== 'all' || r !== 'all';
    if (!filterActive && !showAll) filtered = filtered.slice(0, 20);

    if (countEl) countEl.textContent = `${filtered.length} of ${all.length}`;

    if (!filtered.length) {
      tbody.innerHTML = `<tr><td colspan="13"><div class="empty-state"><strong>No teams match.</strong> Try clearing the search or filters.</div></td></tr>`;
      return;
    }

    const topVisible = new Set(filtered.slice(0, 10).map(t => t.team));
    const rows = [];
    filtered.forEach((t, i) => {
      const opensDrawer = topVisible.has(t.team);
      rows.push(rowHtml(t, i, opensDrawer));
      if (opensDrawer && expanded.has(t.team)) rows.push(drawerHtml(t));
    });
    tbody.innerHTML = rows.join('');

    if (btn) btn.textContent = (showAll || filterActive) ? `Show top 20` : `Show all ${all.length}`;
    paintHeaderSort();
  }

  function paintHeaderSort() {
    headerCells.forEach(th => {
      th.classList.remove('sort-asc', 'sort-desc');
      if (th.dataset.sort === sortKey) th.classList.add(sortDir === 'asc' ? 'sort-asc' : 'sort-desc');
    });
  }

  tbody.addEventListener('click', (e) => {
    // Drawer action handlers
    const copyBtn = e.target.closest('[data-copy]');
    if (copyBtn) {
      const path = copyBtn.dataset.copy;
      const url = window.location.origin + window.location.pathname + path;
      navigator.clipboard?.writeText(url).then(() => {
        const old = copyBtn.textContent; copyBtn.textContent = 'Copied!';
        setTimeout(() => { copyBtn.textContent = old; }, 1500);
      });
      return;
    }
    const tr = e.target.closest('tr.team-row');
    if (!tr || !tr.classList.contains('is-expandable')) return;
    const team = tr.dataset.team;
    if (expanded.has(team)) expanded.delete(team); else expanded.add(team);
    paint();
  });

  headerCells.forEach(th => {
    th.style.cursor = 'pointer';
    th.addEventListener('click', () => {
      const key = th.dataset.sort;
      if (sortKey === key) {
        sortDir = sortDir === 'asc' ? 'desc' : 'asc';
      } else {
        sortKey = key;
        sortDir = th.dataset.sortDefault || (['team', 'group', 'rank'].includes(key) ? 'asc' : 'desc');
      }
      paint();
    });
  });

  if (btn) btn.addEventListener('click', () => { showAll = !showAll; paint(); });
  searchEl?.addEventListener('input', () => paint());
  groupSel?.addEventListener('change', () => paint());
  regionSel?.addEventListener('change', () => paint());
  resetBtn?.addEventListener('click', () => {
    if (searchEl) searchEl.value = '';
    if (groupSel) groupSel.value = 'all';
    if (regionSel) regionSel.value = 'all';
    sortKey = 'p_champion'; sortDir = 'desc';
    paint();
  });

  // Expose for deep link
  window._openContenderDrawer = (team) => {
    if (!all.some(t => t.team === team)) return false;
    expanded.add(team);
    if (searchEl) searchEl.value = '';
    if (groupSel) groupSel.value = 'all';
    if (regionSel) regionSel.value = 'all';
    sortKey = 'p_champion'; sortDir = 'desc';
    showAll = true;
    paint();
    return true;
  };
  window._setGroupFilter = (g) => {
    if (groupSel) groupSel.value = g;
    paint();
  };

  paint();
}

function renderGroups(data) {
  const grid = document.getElementById('groups-grid');
  const byGroup = {};
  data.team_predictions.forEach(t => {
    (byGroup[t.group] = byGroup[t.group] || []).push(t);
  });
  grid.innerHTML = Object.keys(byGroup).sort().map(g => {
    const teams = byGroup[g].slice().sort((a, b) => b.p_finish_1st_group - a.p_finish_1st_group);
    return `<div class="card group-card">
      <h4>GROUP ${escapeHtml(g)}</h4>
      <div class="gr head"><div>Team</div><div class="pos">1st</div><div class="pos">2nd</div><div class="pos">Adv</div></div>
      ${teams.map(t => `
        <div class="gr">
          <div>${confedDotHtml(t.team)}${escapeHtml(t.team)}</div>
          <div class="pos p1">${fmt0(t.p_finish_1st_group)}</div>
          <div class="pos p2">${fmt0(t.p_finish_2nd_group)}</div>
          <div class="pos q">${fmt0(t.p_advance_groups)}</div>
        </div>`).join('')}
    </div>`;
  }).join('');
}

// ---- INTERESTING MATCHES ----
function renderInteresting(data) {
  const grid = document.getElementById('interesting-grid');
  if (!grid) return;
  const ms = data.match_predictions;

  // Pick categories
  const closest = ms.slice().sort((a,b) => {
    const aMax = Math.max(a.p_home_win, a.p_draw, a.p_away_win);
    const bMax = Math.max(b.p_home_win, b.p_draw, b.p_away_win);
    return aMax - bMax;
  })[0];

  const highestGoals = ms.slice().sort((a,b) => (b.lam_home + b.lam_away) - (a.lam_home + a.lam_away))[0];
  const mostLikelyDraw = ms.slice().sort((a,b) => b.p_draw - a.p_draw)[0];
  const biggestMismatch = ms.slice().sort((a,b) =>
    Math.max(b.p_home_win, b.p_away_win) - Math.max(a.p_home_win, a.p_away_win))[0];
  // Biggest upset potential: largest Elo gap with non-trivial underdog chance
  const upset = ms.slice().sort((a,b) => {
    const aGap = Math.abs(a.elo_home - a.elo_away);
    const bGap = Math.abs(b.elo_home - b.elo_away);
    const aUnder = Math.min(a.p_home_win, a.p_away_win);
    const bUnder = Math.min(b.p_home_win, b.p_away_win);
    return (bGap * bUnder) - (aGap * aUnder);
  })[0];
  // Group decider: high entropy + late date
  const groupDates = {};
  ms.forEach(m => {
    if (!groupDates[m.group] || m.date > groupDates[m.group]) groupDates[m.group] = m.date;
  });
  const groupDecider = ms.slice().sort((a,b) => {
    const aFinal = a.date === groupDates[a.group] ? 1 : 0;
    const bFinal = b.date === groupDates[b.group] ? 1 : 0;
    if (aFinal !== bFinal) return bFinal - aFinal;
    const aEnt = -[a.p_home_win, a.p_draw, a.p_away_win].reduce((s,p) => s + (p > 0 ? p*Math.log(p) : 0), 0);
    const bEnt = -[b.p_home_win, b.p_draw, b.p_away_win].reduce((s,p) => s + (p > 0 ? p*Math.log(p) : 0), 0);
    return bEnt - aEnt;
  })[0];

  const cards = [
    { label: 'Closest match', m: closest,
      stat: `${(Math.max(closest.p_home_win, closest.p_draw, closest.p_away_win)*100).toFixed(0)}% top outcome`,
      tone: 'accent' },
    { label: 'Highest expected goals', m: highestGoals,
      stat: `${(highestGoals.lam_home + highestGoals.lam_away).toFixed(2)} expected goals`,
      tone: 'gold' },
    { label: 'Most likely draw', m: mostLikelyDraw,
      stat: `${(mostLikelyDraw.p_draw*100).toFixed(0)}% chance of a draw`,
      tone: 'neutral' },
    { label: 'Biggest mismatch', m: biggestMismatch,
      stat: `${(Math.max(biggestMismatch.p_home_win, biggestMismatch.p_away_win)*100).toFixed(0)}% favourite`,
      tone: 'warning' },
    { label: 'Biggest upset potential', m: upset,
      stat: `${Math.round(Math.abs(upset.elo_home - upset.elo_away))} Elo gap · ${(Math.min(upset.p_home_win, upset.p_away_win)*100).toFixed(0)}% underdog`,
      tone: 'danger' },
    { label: 'Most decisive group game', m: groupDecider,
      stat: `Group ${groupDecider.group} · ${groupDecider.date}`,
      tone: 'success' },
  ];

  grid.innerHTML = cards.map(c => `
    <a class="interesting-card tone-${c.tone}" href="#match-${c.m.m}">
      <div class="ic-label">${escapeHtml(c.label)}</div>
      <div class="ic-teams">
        <span>${confedDotHtml(c.m.home)}${escapeHtml(c.m.home)}</span>
        <span class="ic-vs">vs</span>
        <span>${confedDotHtml(c.m.away)}${escapeHtml(c.m.away)}</span>
      </div>
      <div class="ic-meta">
        <span>${escapeHtml(c.m.date)} · ${escapeHtml(c.m.venue)}</span>
        <span class="ic-stat">${escapeHtml(c.stat)}</span>
      </div>
    </a>`).join('');
}

// ---- MATCHES ----
function renderMatches(data, liveState) {
  const list = document.getElementById('matches-list');
  const groupSel = document.getElementById('f-group');
  const dateSel = document.getElementById('f-date');
  const venueSel = document.getElementById('f-venue');
  const searchEl = document.getElementById('match-search');
  const closeOnly = document.getElementById('f-close-only');
  const resetBtn = document.getElementById('matches-reset');
  const countEl = document.getElementById('filter-count');
  const toggleButtons = document.querySelectorAll('.match-view-toggle button');
  const chips = document.querySelectorAll('#venue-chips .chip');

  while (groupSel.options.length > 1) groupSel.remove(1);
  while (dateSel.options.length > 1) dateSel.remove(1);
  while (venueSel.options.length > 1) venueSel.remove(1);
  [...new Set(data.match_predictions.map(m => m.group))].sort().forEach(g => {
    const o = document.createElement('option'); o.value = g; o.textContent = `Group ${g}`; groupSel.appendChild(o);
  });
  [...new Set(data.match_predictions.map(m => m.date))].sort().forEach(d => {
    const o = document.createElement('option'); o.value = d; o.textContent = d; dateSel.appendChild(o);
  });
  [...new Set(data.match_predictions.map(m => m.venue))].sort().forEach(v => {
    const o = document.createElement('option'); o.value = v; o.textContent = v; venueSel.appendChild(o);
  });

  let view = 'featured';
  let chip = 'all';

  function featuredMatches() {
    const all = data.match_predictions.slice().sort((a, b) => (a.date + a.time).localeCompare(b.date + b.time));
    const unfinished = all.filter(m => !m.locked_score);
    return (unfinished.length ? unfinished : all).slice(0, 12);
  }

  function chipMatches(m) {
    if (chip === 'all') return true;
    if (chip.startsWith('country:')) return m.venue_country === chip.split(':')[1];
    if (chip === 'hot') return (m.climate || '').includes('hot');
    if (chip === 'altitude') return (m.altitude_m || 0) > 1500;
    if (chip === 'travel') {
      const total = (m.home_travel_km || 0) + (m.away_travel_km || 0);
      return total > 3000;
    }
    return true;
  }

  function paint() {
    const q = (searchEl?.value || '').trim().toLowerCase();
    const g = groupSel.value, d = dateSel.value, vn = venueSel.value;
    const close = closeOnly?.checked;
    const userFilterActive = q || g !== 'all' || d !== 'all' || vn !== 'all' || close || chip !== 'all';

    let pool = userFilterActive || view === 'all'
      ? data.match_predictions : featuredMatches();

    let filtered = pool.filter(m => {
      if (g !== 'all' && m.group !== g) return false;
      if (d !== 'all' && m.date !== d) return false;
      if (vn !== 'all' && m.venue !== vn) return false;
      if (!chipMatches(m)) return false;
      if (q && !(m.home.toLowerCase().includes(q) || m.away.toLowerCase().includes(q) || m.venue.toLowerCase().includes(q))) return false;
      if (close) {
        const max = Math.max(m.p_home_win, m.p_draw, m.p_away_win);
        if (max > 0.55) return false;
      }
      return true;
    });

    const total = view === 'all' || userFilterActive ? data.match_predictions.length : featuredMatches().length;
    if (countEl) countEl.textContent = `${filtered.length} of ${total}`;

    if (!filtered.length) {
      list.innerHTML = `<div class="empty-state" style="grid-column: 1/-1"><strong>No matches found.</strong> Try clearing filters or switching to "All 72".</div>`;
      return;
    }

    list.innerHTML = filtered.map(m => {
      const ph = (m.p_home_win * 100).toFixed(0);
      const pd = (m.p_draw * 100).toFixed(0);
      const pa = (m.p_away_win * 100).toFixed(0);
      const winner = m.p_home_win > m.p_away_win
        ? (m.p_home_win > m.p_draw ? m.home : 'Draw')
        : (m.p_away_win > m.p_draw ? m.away : 'Draw');
      const tags = [];
      if (m.altitude_m > 1500) tags.push(`<span class="tag alt">⛰ ${m.altitude_m}m</span>`);
      if ((m.climate || '').includes('very_hot')) tags.push('<span class="tag hot">very hot</span>');
      else if ((m.climate || '').includes('hot')) tags.push('<span class="tag hot">hot</span>');
      const travel = Math.max(m.home_travel_km || 0, m.away_travel_km || 0);
      if (travel > 2500) tags.push(`<span class="tag travel">${(travel/1000).toFixed(1)}k km</span>`);
      if (m.locked_score) tags.push(`<span class="tag locked">final ${escapeHtml(m.locked_score)}</span>`);

      return `<div class="card match" id="match-${m.m}">
        <div class="match-head">
          <span class="pill">M${m.m} · Grp ${escapeHtml(m.group)}</span>
          <span>${escapeHtml(m.date)} ${escapeHtml(m.time)} · ${escapeHtml(m.venue)}</span>
        </div>
        ${tags.length ? `<div class="venue-tags">${tags.join('')}</div>` : ''}
        <div class="match-teams">
          <div class="team-home">
            <div class="team-name">${confedDotHtml(m.home)}${escapeHtml(m.home)}</div>
            <span class="team-lambda">λ ${m.lam_home.toFixed(2)}</span>
          </div>
          <div class="vs">vs</div>
          <div class="team-away">
            <div class="team-name">${confedDotHtml(m.away)}${escapeHtml(m.away)}</div>
            <span class="team-lambda">λ ${m.lam_away.toFixed(2)}</span>
          </div>
        </div>
        <div class="prob-bar">
          <div class="ph" style="width:${ph}%" title="${escapeHtml(m.home)} win">${ph}%</div>
          <div class="pd" style="width:${pd}%" title="Draw">${pd}%</div>
          <div class="pa" style="width:${pa}%" title="${escapeHtml(m.away)} win">${pa}%</div>
        </div>
        <div class="match-meta">
          <span>Elo ${Math.round(m.elo_home)} vs ${Math.round(m.elo_away)}</span>
          <span>Predicted: <strong>${escapeHtml(winner)}</strong></span>
        </div>
      </div>`;
    }).join('');
  }

  toggleButtons.forEach(btn => {
    btn.addEventListener('click', () => {
      toggleButtons.forEach(b => { b.classList.remove('active'); b.setAttribute('aria-selected', 'false'); });
      btn.classList.add('active'); btn.setAttribute('aria-selected', 'true');
      view = btn.dataset.view;
      paint();
    });
  });

  chips.forEach(c => {
    c.addEventListener('click', () => {
      chips.forEach(x => x.classList.remove('active'));
      c.classList.add('active');
      chip = c.dataset.chip;
      paint();
    });
  });

  groupSel.addEventListener('change', paint);
  dateSel.addEventListener('change', paint);
  venueSel.addEventListener('change', paint);
  searchEl?.addEventListener('input', paint);
  closeOnly?.addEventListener('change', paint);
  resetBtn?.addEventListener('click', () => {
    if (searchEl) searchEl.value = '';
    groupSel.value = 'all'; dateSel.value = 'all'; venueSel.value = 'all';
    if (closeOnly) closeOnly.checked = false;
    chip = 'all';
    chips.forEach(c => c.classList.toggle('active', c.dataset.chip === 'all'));
    view = 'featured';
    toggleButtons.forEach(b => { b.classList.toggle('active', b.dataset.view === 'featured'); b.setAttribute('aria-selected', b.dataset.view === 'featured' ? 'true' : 'false'); });
    paint();
  });

  // Expose for deep-link
  window._setMatchGroup = (g) => { groupSel.value = g; chip = 'all'; chips.forEach(c => c.classList.toggle('active', c.dataset.chip === 'all')); view = 'all'; toggleButtons.forEach(b => { b.classList.toggle('active', b.dataset.view === 'all'); b.setAttribute('aria-selected', b.dataset.view === 'all' ? 'true' : 'false'); }); paint(); };

  paint();
}

// ---- COMPARE TWO TEAMS ----
function renderCompare(data, travel) {
  const a = document.getElementById('cmp-a');
  const b = document.getElementById('cmp-b');
  const grid = document.getElementById('compare-grid');
  const swap = document.getElementById('cmp-swap');
  if (!a || !b || !grid) return;

  const teams = data.team_predictions.slice().sort((x, y) => x.team.localeCompare(y.team));
  teams.forEach(t => {
    const o1 = document.createElement('option'); o1.value = t.team; o1.textContent = t.team; a.appendChild(o1);
    const o2 = document.createElement('option'); o2.value = t.team; o2.textContent = t.team; b.appendChild(o2);
  });
  // Default to top-2 favourites
  const sortedByChamp = data.team_predictions.slice().sort((x,y) => y.p_champion - x.p_champion);
  a.value = sortedByChamp[0]?.team || teams[0].team;
  b.value = sortedByChamp[1]?.team || teams[1].team;

  const travelKm = (travel?.total_group_travel_km_by_team) || {};
  const travelDelta = {};
  (travel?.all_diffs || []).forEach(d => travelDelta[d.team] = d.delta_pp);

  function row(label, va, vb, fmtFn, betterIsHigher = true) {
    const cmp = (va == null || vb == null) ? '' :
      va === vb ? 'cmp-eq' :
      ((betterIsHigher ? va > vb : va < vb) ? 'cmp-a-better' : 'cmp-b-better');
    return `<div class="cmp-row ${cmp}">
      <div class="cmp-label">${escapeHtml(label)}</div>
      <div class="cmp-a-val">${va == null ? '—' : fmtFn(va)}</div>
      <div class="cmp-bar" aria-hidden="true">
        <span class="ab-a" style="width:${pctBar(va, vb)}%"></span>
        <span class="ab-b" style="width:${pctBar(vb, va)}%"></span>
      </div>
      <div class="cmp-b-val">${vb == null ? '—' : fmtFn(vb)}</div>
    </div>`;
  }
  function pctBar(x, y) {
    if (x == null || y == null) return 0;
    const m = Math.max(Math.abs(x), Math.abs(y));
    if (m === 0) return 0;
    return Math.min(100, (Math.abs(x) / m) * 100);
  }

  function paint() {
    const ta = data.team_predictions.find(t => t.team === a.value);
    const tb = data.team_predictions.find(t => t.team === b.value);
    if (!ta || !tb) return;

    grid.innerHTML = `
      <div class="cmp-head">
        <div class="cmp-team-a">${confedDotHtml(ta.team)}<strong>${escapeHtml(ta.team)}</strong><span class="muted small">Group ${escapeHtml(ta.group)}</span></div>
        <div class="cmp-vs">vs</div>
        <div class="cmp-team-b">${confedDotHtml(tb.team)}<strong>${escapeHtml(tb.team)}</strong><span class="muted small">Group ${escapeHtml(tb.group)}</span></div>
      </div>
      <div class="cmp-rows">
        ${row('Champion',           ta.p_champion,           tb.p_champion,           fmt)}
        ${row('Reach Final',        ta.p_reach_final,        tb.p_reach_final,        fmt)}
        ${row('Reach SF',           ta.p_reach_sf,           tb.p_reach_sf,           fmt)}
        ${row('Reach QF',           ta.p_reach_qf,           tb.p_reach_qf,           fmt)}
        ${row('Advance from group', ta.p_advance_groups,     tb.p_advance_groups,     fmt0)}
        ${row('Win group',          ta.p_finish_1st_group,   tb.p_finish_1st_group,   fmt0)}
        ${row('Elo',                ta.elo,                  tb.elo,                  v => Math.round(v))}
        ${row('FIFA points',        ta.fifa_pts,             tb.fifa_pts,             v => v ? v.toFixed(0) : '—')}
        ${row('Squad value (€M)',   ta.squad_value_eur_m,    tb.squad_value_eur_m,    fmtNum)}
        ${row('Group travel (km)',  travelKm[ta.team],       travelKm[tb.team],       v => Math.round(v).toLocaleString(), false)}
        ${row('Travel impact (pp)', travelDelta[ta.team],    travelDelta[tb.team],    v => `${v >= 0 ? '+' : ''}${v.toFixed(2)}pp`)}
      </div>`;
  }

  a.addEventListener('change', paint);
  b.addEventListener('change', paint);
  swap?.addEventListener('click', () => {
    const tmp = a.value; a.value = b.value; b.value = tmp; paint();
  });

  window._setCompare = (teamA, teamB) => {
    if (teamA && data.team_predictions.some(t => t.team === teamA)) a.value = teamA;
    if (teamB && data.team_predictions.some(t => t.team === teamB)) b.value = teamB;
    paint();
  };

  paint();
}

// ---- DEEP LINKING (hardened — malformed hashes never crash) ----
function safeDecode(s) {
  try { return decodeURIComponent(s); }
  catch (e) { return s; } // malformed %XX sequences fall through verbatim
}

function parseHash() {
  const raw = window.location.hash.replace(/^#/, '');
  const params = {};
  if (!raw) return params;
  try {
    if (raw.includes('=')) {
      raw.split('&').forEach(part => {
        const idx = part.indexOf('=');
        if (idx < 0) return;
        const k = part.slice(0, idx).trim();
        const v = part.slice(idx + 1);
        if (k) params[k] = safeDecode(v);
      });
    } else {
      params.section = raw;
    }
  } catch (e) {
    console.warn('[deep-link] parseHash failed, ignoring', e);
  }
  return params;
}

function applyDeepLink() {
  let p;
  try { p = parseHash(); } catch (e) { console.warn('[deep-link] applyDeepLink:', e); return; }

  if (p.team && typeof window._openContenderDrawer === 'function') {
    try {
      const ok = window._openContenderDrawer(p.team);
      if (ok) {
        setTimeout(() => {
          try {
            const sel = `tr.team-row[data-team="${(window.CSS?.escape || (s => s))(p.team)}"]`;
            const tr = document.querySelector(sel);
            tr?.scrollIntoView({ behavior: 'smooth', block: 'center' });
          } catch (e) { console.warn('[deep-link] scrollIntoView:', e); }
        }, 100);
      } else {
        console.info(`[deep-link] team="${p.team}" not in roster; ignoring`);
      }
    } catch (e) { console.warn('[deep-link] team handler:', e); }
  }

  if (p.group && typeof window._setGroupFilter === 'function') {
    try {
      window._setGroupFilter(p.group);
      if (typeof window._setMatchGroup === 'function') window._setMatchGroup(p.group);
      setTimeout(() => {
        document.getElementById('groups')?.scrollIntoView({ behavior: 'smooth' });
      }, 100);
    } catch (e) { console.warn('[deep-link] group handler:', e); }
  }

  if (p.compare && typeof window._setCompare === 'function') {
    try {
      const [tA, tB] = String(p.compare).split(',').map(s => s.trim()).filter(Boolean);
      window._setCompare(tA, tB);
      setTimeout(() => {
        document.getElementById('compare')?.scrollIntoView({ behavior: 'smooth' });
      }, 100);
    } catch (e) { console.warn('[deep-link] compare handler:', e); }
  }
}

// ---- CHARTS ----
function chartTheme() {
  return {
    text:   cssVar('--text'),
    text2:  cssVar('--text-2'),
    grid:   cssVar('--border'),
    accent: cssVar('--accent'),
    gold:   cssVar('--gold'),
  };
}

function renderTitleChart(data) {
  const top20 = data.team_predictions.slice(0, 20);
  const th = chartTheme();
  const c = new Chart(document.getElementById('title-chart'), {
    type: 'bar',
    data: {
      labels: top20.map(t => t.team),
      datasets: [
        { label: 'Champion', data: top20.map(t => +(t.p_champion*100).toFixed(2)), backgroundColor: th.gold },
        { label: 'Final',    data: top20.map(t => +(t.p_reach_final*100).toFixed(2)), backgroundColor: th.accent },
        { label: 'SF',       data: top20.map(t => +(t.p_reach_sf*100).toFixed(2)), backgroundColor: 'rgba(96, 165, 250, 0.4)' },
      ],
    },
    options: {
      maintainAspectRatio: false,
      indexAxis: 'y',
      animation: { duration: 800 },
      plugins: {
        legend: { labels: { color: th.text, font: { size: 11 } } },
        tooltip: { callbacks: { label: c => `${c.dataset.label}: ${c.parsed.x.toFixed(1)}%` } },
      },
      scales: {
        x: { ticks: { color: th.text2, callback: v => v + '%' }, grid: { color: th.grid } },
        y: { ticks: { color: th.text, font: { size: 11 } }, grid: { display: false } },
      },
    },
  });
  window._charts.push(c);
}

function renderConfedChart(data) {
  const th = chartTheme();
  const buckets = {};
  data.team_predictions.forEach(t => {
    const k = CONFED[t.team] || 'Other';
    buckets[k] = (buckets[k] || 0) + t.p_champion;
  });
  const labels = Object.keys(buckets);
  const colors = {
    UEFA: cssVar('--confed-uefa'), CONMEBOL: cssVar('--confed-conmebol'),
    CAF: cssVar('--confed-caf'),  CONCACAF: cssVar('--confed-concacaf'),
    AFC: cssVar('--confed-afc'),  OFC: cssVar('--confed-ofc'),
  };
  const c = new Chart(document.getElementById('confed-chart'), {
    type: 'doughnut',
    data: {
      labels,
      datasets: [{
        data: labels.map(l => +(buckets[l]*100).toFixed(2)),
        backgroundColor: labels.map(l => colors[l] || cssVar('--text-3')),
        borderColor: cssVar('--bg'), borderWidth: 2,
      }],
    },
    options: {
      maintainAspectRatio: false,
      animation: { duration: 800 },
      plugins: {
        legend: { position: 'right', labels: { color: th.text, padding: 14, font: { size: 12 } } },
        tooltip: { callbacks: { label: c => `${c.label}: ${c.parsed.toFixed(1)}%` } },
      },
    },
  });
  window._charts.push(c);
}

function renderFeatureChart(data) {
  const th = chartTheme();
  const fi = data.feature_importances_home || data.feature_importances || {};
  const labels = Object.keys(fi);
  const c = new Chart(document.getElementById('feat-chart'), {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        data: labels.map(k => +(fi[k]*100).toFixed(2)),
        backgroundColor: labels.map((_, i) => i === 0 ? th.gold : th.accent),
      }],
    },
    options: {
      maintainAspectRatio: false,
      animation: { duration: 700 },
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: th.text2, maxRotation: 40, minRotation: 40 }, grid: { display: false } },
        y: { ticks: { color: th.text2, callback: v => v + '%' }, grid: { color: th.grid } },
      },
    },
  });
  window._charts.push(c);
}

function renderCalibration(cal) {
  const th = chartTheme();
  const c = cal.calibration || {};
  const datasets = [];
  const colors = { home: cssVar('--success'), draw: cssVar('--text-2'), away: cssVar('--warning') };
  for (const k of ['home', 'draw', 'away']) {
    const pts = (c[k] || []).map(b => ({ x: b.mean_pred, y: b.actual_freq, r: Math.min(18, Math.sqrt(b.n)/2) }));
    datasets.push({ label: `${k[0].toUpperCase()+k.slice(1)} win`, data: pts, backgroundColor: colors[k], borderColor: colors[k] });
  }
  datasets.push({
    type: 'line', label: 'Perfect calibration',
    data: [{ x: 0, y: 0 }, { x: 1, y: 1 }],
    showLine: true, borderColor: cssVar('--text-3'), borderDash: [4, 4], pointRadius: 0, fill: false,
  });
  const ch = new Chart(document.getElementById('cal-chart'), {
    type: 'bubble',
    data: { datasets },
    options: {
      maintainAspectRatio: false,
      animation: { duration: 800 },
      plugins: {
        legend: { labels: { color: th.text } },
        tooltip: { callbacks: { label: c => `pred=${c.parsed.x.toFixed(2)} actual=${c.parsed.y.toFixed(2)}` } },
      },
      scales: {
        x: { min: 0, max: 1, title: { display: true, text: 'Predicted', color: th.text2 }, ticks: { color: th.text2 }, grid: { color: th.grid } },
        y: { min: 0, max: 1, title: { display: true, text: 'Actual', color: th.text2 }, ticks: { color: th.text2 }, grid: { color: th.grid } },
      },
    },
  });
  window._charts.push(ch);
}

function renderWalkForward(wf) {
  const tbody = document.querySelector('#wf-table tbody');
  tbody.innerHTML = Object.entries(wf).map(([year, r]) => `
    <tr><td><strong>${escapeHtml(year)}</strong></td>
    <td class="num">${r.test_n}</td>
    <td class="num">${r.log_loss.toFixed(3)}</td>
    <td class="num hide-sm">${r.brier.toFixed(3)}</td>
    <td class="num">${(r.accuracy*100).toFixed(1)}%</td>
    <td class="num" style="color: var(--success)">+${r.lift_vs_baseline.toFixed(3)}</td>
    </tr>`).join('');
}

function renderAblation(abl) {
  const tbody = document.querySelector('#abl-table tbody');
  const rows = [];
  for (const [name, m] of Object.entries(abl)) {
    if (name === 'lift' || name === 'note') continue;
    rows.push(`<tr><td><strong>${escapeHtml(name.replace(/_/g, ' '))}</strong></td>
      <td class="num">${m.log_loss.toFixed(3)}</td>
      <td class="num hide-sm">${m.brier.toFixed(3)}</td>
      <td class="num">${(m.accuracy*100).toFixed(1)}%</td></tr>`);
  }
  tbody.innerHTML = rows.join('');
}

function renderTravel(travel) {
  const km = travel.total_group_travel_km_by_team || {};
  const top = Object.entries(km).filter(([_, k]) => k > 0).slice(0, 8);
  document.getElementById('travel-km-list').innerHTML = top.map(([t, k]) =>
    `<li><span>${confedDotHtml(t)}${escapeHtml(t)}</span><span></span><span class="delta">${k.toFixed(0)} km</span></li>`
  ).join('');
  const benefit = travel.beneficiaries_top5 || [];
  const losers = travel.losers_top5 || [];
  document.getElementById('travel-delta-list').innerHTML = [
    ...benefit.map(d => `<li><span>${confedDotHtml(d.team)}${escapeHtml(d.team)}</span><span></span><span class="delta pos">+${d.delta_pp.toFixed(2)}pp</span></li>`),
    ...losers.map(d  => `<li><span>${confedDotHtml(d.team)}${escapeHtml(d.team)}</span><span></span><span class="delta neg">${d.delta_pp.toFixed(2)}pp</span></li>`),
  ].join('');
}

function renderFooter(data, liveState) {
  const m = data.model_metrics || {};
  const total = (data.n_simulations_total || data.n_simulations || 0).toLocaleString();
  const seeds = data.n_seeds || 1;
  const sps = (data.n_simulations_per_seed || data.n_simulations || 0).toLocaleString();
  const last = liveState?.last_updated_utc || data.generated_at || '';
  document.getElementById('footer-meta').textContent =
    `Generated ${(last || '').slice(0, 19)} UTC · ${total} simulations (${seeds} seeds × ${sps}) · model trained on ${m.n_train ? m.n_train.toLocaleString() : '—'} matches`;
}

init().catch(err => {
  document.body.innerHTML = `<div style="padding:40px;color:var(--danger);font-family:system-ui"><h2>Could not load data</h2><pre>${escapeHtml(err.stack || err)}</pre></div>`;
});
