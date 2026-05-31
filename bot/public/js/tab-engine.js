/**
 * tab-engine.js — Engine analysis tab.
 *
 * Search feed table (per-depth rows), eval chart, aggregate stats,
 * time-per-move bar chart, confidence + budget indicators.
 */
'use strict';

const TabEngine = (() => {
  let _rendered = false;
  let _gameId = null;

  const panel = () => document.getElementById('panel-engine');

  /* ── lifecycle ──────────────────────────────────────────────────────── */

  function show() {
    const g = App.currentGame();
    if (!g) { panel().innerHTML = '<div class="empty-state"><div class="icon">⚙</div>No game data yet.</div>'; _rendered = false; return; }
    _gameId = g.id;
    _fullRender(g);
  }

  function onEvent(type, data) {
    const g = App.currentGame();
    if (!g || App.activeTab() !== 'engine') return;
    if (g.id !== _gameId) { _gameId = g.id; _rendered = false; }
    if (!_rendered) { _fullRender(g); return; }

    switch (type) {
      case 'search_info':
        _upsertSearchRow(data);
        _updateLiveStats(g);
        break;
      case 'search_start':
        _markRowsStale();
        _updateLiveStats(g);
        break;
      case 'search_end':
      case 'move':
        _updateAggStats(g);
        _updateChart(g);
        _updateTimeBars(g);
        break;
      case 'snapshot':
      case 'game_start':
      case 'game_end':
        _fullRender(g);
        break;
    }
  }

  /* ── full render ────────────────────────────────────────────────────── */

  function _fullRender(g) {
    panel().innerHTML = `
      <div class="engine-stats-row" id="eng-stats"></div>
      <div class="search-table-wrap" id="search-wrap">
        <table class="search-table" id="search-table">
          <thead><tr>
            <th>Depth</th><th>Sel</th><th>Eval</th><th>Nodes</th>
            <th>NPS</th><th>Conf</th><th>Time</th><th>PV</th>
          </tr></thead>
          <tbody id="search-tbody"></tbody>
        </table>
      </div>
      <div class="engine-chart-wrap" id="eng-chart"></div>
      <div class="move-time-chart" id="move-times"></div>
    `;
    _rendered = true;

    // Populate search rows from current search history
    if (g._searchHistory && g._searchHistory.length > 0) {
      for (const info of g._searchHistory) _upsertSearchRow(info);
    }

    _updateAggStats(g);
    _updateChart(g);
    _updateTimeBars(g);
    _updateLiveStats(g);
  }

  /* ── search feed table ──────────────────────────────────────────────── */

  /**
   * Upsert a row keyed by depth.
   * - If a row for this depth already exists: update it in-place and clear
   *   the `stale` class (new search has reached this depth again).
   * - If no row exists: append a new one.
   * Rows from the previous search that haven't been reached yet stay dimmed.
   */
  function _upsertSearchRow(info) {
    const tbody = document.getElementById('search-tbody');
    if (!tbody) return;

    const depth = info.depth ?? 0;
    const evalS = info.mate != null ? `M${info.mate}` :
                  info.eval_cp != null ? (info.eval_cp / 100).toFixed(2) : '–';
    const confP = info.confidence != null ? (info.confidence * 100).toFixed(0) + '%' : '–';
    const pv = info.pv ? info.pv.join(' ') : (info.pv0 || '–');
    const isPonder = !!info.ponder;

    const inner = `
      <td>${depth}</td>
      <td>${info.seldepth ?? '–'}</td>
      <td>${evalS}</td>
      <td>${App.fmtN(info.nodes ?? 0)}</td>
      <td>${App.fmtN(info.nps ?? 0)}</td>
      <td>${confP}</td>
      <td>${App.fmtMs(info.elapsed)}</td>
      <td class="pv-cell" title="${App.esc(pv)}">${App.esc(pv)}</td>
    `;

    // Try to find an existing row for this depth
    let tr = tbody.querySelector(`tr[data-depth="${depth}"]`);
    if (tr) {
      tr.innerHTML = inner;
      tr.classList.remove('stale');
      if (isPonder) tr.classList.add('ponder-row'); else tr.classList.remove('ponder-row');
    } else {
      tr = document.createElement('tr');
      tr.dataset.depth = depth;
      tr.innerHTML = inner;
      if (isPonder) tr.classList.add('ponder-row');
      // Insert in depth order (sorted ascending)
      let inserted = false;
      for (const existing of tbody.querySelectorAll('tr')) {
        const d = parseInt(existing.dataset.depth ?? '0', 10);
        if (d > depth) { tbody.insertBefore(tr, existing); inserted = true; break; }
      }
      if (!inserted) tbody.appendChild(tr);
    }

    // Scroll so the latest high-depth row is visible
    const wrap = document.getElementById('search-wrap');
    if (wrap) wrap.scrollTop = wrap.scrollHeight;
  }

  /** Dim all current rows — they're from the previous search. */
  function _markRowsStale() {
    const tbody = document.getElementById('search-tbody');
    if (!tbody) return;
    for (const tr of tbody.querySelectorAll('tr')) tr.classList.add('stale');
  }

  /* ── live stats (budget/confidence bars) ────────────────────────────── */

  function _updateLiveStats(g) {
    const el = document.getElementById('eng-stats');
    if (!el) return;

    const moves = g.moves || [];
    const totalNodes = moves.reduce((s, m) => s + (m.nodes || 0), 0);
    const avgDepth = moves.length ? (moves.reduce((s, m) => s + m.depth, 0) / moves.length).toFixed(1) : '–';
    const avgNps = moves.length ? App.fmtN(moves.reduce((s, m) => s + (m.nps || 0), 0) / moves.length) : '–';
    const totalTime = moves.reduce((s, m) => s + (m.time_ms || 0), 0);

    // Budget bar
    let budgetPct = 0;
    let budgetMax = '–';
    if (g._searchProfile && g._searchProfile.maxTimeMs) {
      budgetMax = App.fmtMs(g._searchProfile.maxTimeMs);
      if (g.searchLive && g.searchLive.elapsed) {
        budgetPct = Math.min(100, (g.searchLive.elapsed / g._searchProfile.maxTimeMs) * 100);
      }
    }

    // Confidence bar
    const conf = g.confidence ?? 0;
    const confPct = (conf * 100).toFixed(0);
    const confColor = conf >= 0.8 ? 'var(--green)' : conf >= 0.5 ? 'var(--yellow)' : 'var(--red)';

    el.innerHTML = `
      <div class="stat-card">
        <span class="stat-label">Total Nodes</span>
        <span class="stat-value">${App.fmtN(totalNodes)}</span>
      </div>
      <div class="stat-card">
        <span class="stat-label">Avg Depth</span>
        <span class="stat-value">${avgDepth}</span>
      </div>
      <div class="stat-card">
        <span class="stat-label">Avg NPS</span>
        <span class="stat-value">${avgNps}</span>
      </div>
      <div class="stat-card">
        <span class="stat-label">Search Time</span>
        <span class="stat-value">${App.fmtMs(totalTime)}</span>
      </div>
      <div class="stat-card">
        <span class="stat-label">Budget (${budgetMax})</span>
        <div class="bar-wrap"><div class="bar-fill" style="width:${budgetPct}%;background:var(--accent);"></div></div>
      </div>
      <div class="stat-card">
        <span class="stat-label">Confidence</span>
        <span class="stat-value" style="color:${confColor}">${confPct}%</span>
        <div class="bar-wrap"><div class="bar-fill" style="width:${confPct}%;background:${confColor};"></div></div>
      </div>
    `;
  }

  /* ── aggregate stats + chart ────────────────────────────────────────── */

  function _updateAggStats(g) { _updateLiveStats(g); }

  function _updateChart(g) {
    const el = document.getElementById('eng-chart');
    if (el && (g.moves || []).length >= 2) Chart.renderEval(el, g);
  }

  /* ── time per move bar chart ────────────────────────────────────────── */

  function _updateTimeBars(g) {
    const el = document.getElementById('move-times');
    if (!el) return;
    const moves = g.moves || [];
    if (moves.length === 0) { el.innerHTML = '<span class="muted" style="padding:0.3rem;">No moves yet</span>'; return; }

    const maxT = Math.max(...moves.map(m => m.time_ms || 0), 1);
    let html = '';
    for (let i = 0; i < moves.length; i++) {
      const m = moves[i];
      const t = m.time_ms || 0;
      const pct = (t / maxT * 100).toFixed(1);
      const moveNum = m.ply != null ? Math.ceil(m.ply / 2) : i + 1;
      html += `<div class="mt-row">
        <span class="mt-num">${moveNum}</span>
        <div class="mt-bar" style="width:${pct}%"></div>
        <span class="mt-val">${App.fmtMs(t)}</span>
      </div>`;
    }
    el.innerHTML = html;
  }

  /* ── register ──────────────────────────────────────────────────────── */

  App.registerTab('engine', { show, onEvent });
  return { show, onEvent };
})();
