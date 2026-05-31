/**
 * tab-analytics.js — Game analytics export + PGN download tab.
 *
 * Lets the operator pull PGN exports from Lichess filtered by:
 *   Period  — Today / This Week / This Month / All Time
 *   Result  — All / Won / Lost / Drawn
 *
 * Actions:
 *   Copy PGN         → clipboard
 *   Download PGN     → browser file download
 *   Save to Analyze  → POST /api/analytics/save (writes to <workspace>/analyze/)
 */
'use strict';

const TabAnalytics = (() => {
  const panel = () => document.getElementById('panel-analytics');

  /* ── filter state ────────────────────────────────────────────────────── */
  let _period  = localStorage.getItem('analytics-period')  || 'week';
  let _result  = localStorage.getItem('analytics-result')  || 'all';
  let _loading = false;
  let _lastPgn = '';        // cached from last fetch
  let _lastCount = 0;

  const PERIODS = [
    { id: 'today', label: 'Today'      },
    { id: 'week',  label: 'This Week'  },
    { id: 'month', label: 'This Month' },
    { id: 'all',   label: 'All Time'   },
  ];

  const RESULTS = [
    { id: 'all',  label: 'All'   },
    { id: 'win',  label: 'Won'   },
    { id: 'loss', label: 'Lost'  },
    { id: 'draw', label: 'Drawn' },
  ];

  /* ── lifecycle ───────────────────────────────────────────────────────── */
  function show() {
    _render();
    _loadFiles();
  }

  function onEvent() { /* no live updates needed */ }

  /* ── render ──────────────────────────────────────────────────────────── */
  function _render() {
    // Session stats from in-memory store
    const sessionGames = [...App.games.values()].filter(g => g.status === 'finished');
    const sW = sessionGames.filter(g => App.isWin(g)).length;
    const sL = sessionGames.filter(g => App.isLoss(g)).length;
    const sD = sessionGames.filter(g => !App.isWin(g) && !App.isLoss(g) && g.result).length;

    panel().innerHTML = `
<div class="analytics-wrap">

  <!-- ── Filter bar ──────────────────────────────────────────────────── -->
  <div class="analytics-filters">
    <div class="filter-group">
      <span class="filter-label">Period</span>
      <div class="pill-row" id="period-pills">
        ${PERIODS.map(p => `
          <button class="pill${_period === p.id ? ' active' : ''}" data-period="${p.id}">${p.label}</button>
        `).join('')}
      </div>
    </div>
    <div class="filter-group">
      <span class="filter-label">Result</span>
      <div class="pill-row" id="result-pills">
        ${RESULTS.map(r => `
          <button class="pill${_result === r.id ? ' active' : ''}" data-result="${r.id}">${r.label}</button>
        `).join('')}
      </div>
    </div>
  </div>

  <!-- ── Session summary ─────────────────────────────────────────────── -->
  <div class="analytics-session-bar">
    <span class="analytics-session-label">This session</span>
    <span class="analytics-badge win">${sW}<small>W</small></span>
    <span class="analytics-badge loss">${sL}<small>L</small></span>
    <span class="analytics-badge draw">${sD}<small>D</small></span>
    <span class="analytics-session-total">${sessionGames.length} game${sessionGames.length !== 1 ? 's' : ''}</span>
  </div>

  <!-- ── Action buttons ─────────────────────────────────────────────── -->
  <div class="analytics-actions">
    <button class="analytics-btn" id="btn-copy-pgn">
      <div class="analytics-btn-icon">⎘</div>
      <div class="analytics-btn-body">
        <div class="analytics-btn-title">Copy PGN</div>
        <div class="analytics-btn-sub">to clipboard</div>
      </div>
    </button>
    <button class="analytics-btn" id="btn-download-pgn">
      <div class="analytics-btn-icon">↓</div>
      <div class="analytics-btn-body">
        <div class="analytics-btn-title">Download PGN</div>
        <div class="analytics-btn-sub">browser file save</div>
      </div>
    </button>
    <button class="analytics-btn accent" id="btn-save-analyze">
      <div class="analytics-btn-icon">📂</div>
      <div class="analytics-btn-body">
        <div class="analytics-btn-title">Save to Analyze</div>
        <div class="analytics-btn-sub">analyze/ folder</div>
      </div>
    </button>
  </div>

  <!-- ── Status line ──────────────────────────────────────────────────── -->
  <div class="analytics-status" id="analytics-status"></div>

  <!-- ── Saved files list ────────────────────────────────────────────── -->
  <div class="analytics-files-section">
    <div class="analytics-files-header">
      <span>Saved files</span>
      <button class="analytics-refresh-btn" id="btn-refresh-files" title="Refresh file list">↻</button>
    </div>
    <div id="analytics-file-list" class="analytics-file-list">
      <span class="analytics-empty">No saved files yet</span>
    </div>
  </div>

</div>`;

    _bindEvents();
  }

  /* ── event binding ───────────────────────────────────────────────────── */
  function _bindEvents() {
    // Period pills
    document.getElementById('period-pills')?.addEventListener('click', e => {
      const btn = e.target.closest('.pill[data-period]');
      if (!btn) return;
      _period = btn.dataset.period;
      localStorage.setItem('analytics-period', _period);
      document.querySelectorAll('#period-pills .pill').forEach(b =>
        b.classList.toggle('active', b.dataset.period === _period));
      _lastPgn = ''; _lastCount = 0;   // invalidate cache
      _setStatus('');
    });

    // Result pills
    document.getElementById('result-pills')?.addEventListener('click', e => {
      const btn = e.target.closest('.pill[data-result]');
      if (!btn) return;
      _result = btn.dataset.result;
      localStorage.setItem('analytics-result', _result);
      document.querySelectorAll('#result-pills .pill').forEach(b =>
        b.classList.toggle('active', b.dataset.result === _result));
      _lastPgn = ''; _lastCount = 0;
      _setStatus('');
    });

    // Copy PGN
    document.getElementById('btn-copy-pgn')?.addEventListener('click', async () => {
      const pgn = await _fetchPgn();
      if (!pgn) return;
      try {
        await navigator.clipboard.writeText(pgn);
        _setStatus(`✓ Copied ${_lastCount} game${_lastCount !== 1 ? 's' : ''} to clipboard`, 'ok');
      } catch (_) {
        _setStatus('✗ Clipboard access denied', 'err');
      }
    });

    // Download PGN
    document.getElementById('btn-download-pgn')?.addEventListener('click', async () => {
      const pgn = await _fetchPgn();
      if (!pgn) return;
      const ts    = new Date().toISOString().slice(0, 10);
      const name  = `H035_${_result}_${_period}_${ts}.pgn`;
      const blob  = new Blob([pgn], { type: 'application/x-chess-pgn' });
      const url   = URL.createObjectURL(blob);
      const a     = document.createElement('a');
      a.href      = url;
      a.download  = name;
      a.click();
      URL.revokeObjectURL(url);
      _setStatus(`✓ Downloaded ${_lastCount} game${_lastCount !== 1 ? 's' : ''} as ${name}`, 'ok');
    });

    // Save to analyze folder
    document.getElementById('btn-save-analyze')?.addEventListener('click', async () => {
      const pgn = await _fetchPgn();
      if (!pgn) return;
      try {
        const ts   = new Date().toISOString().replace(/[:.]/g, '-').replace('T', '_').slice(0, 16);
        const name = `${_result}_${_period}_${ts}.pgn`;
        const res  = await fetch('/api/analytics/save', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pgn, filename: name }),
        });
      let data;
      try { data = await res.json(); } catch (_) { throw new Error(`Server error ${res.status}`); }
        if (!res.ok) throw new Error(data.error || 'Save failed');
        _setStatus(`✓ Saved ${_lastCount} game${_lastCount !== 1 ? 's' : ''} → analyze/${data.file}`, 'ok');
        _loadFiles();   // refresh file list
      } catch (e) {
        _setStatus(`✗ ${e.message}`, 'err');
      }
    });

    // Refresh files
    document.getElementById('btn-refresh-files')?.addEventListener('click', _loadFiles);
  }

  /* ── fetch PGN from server ────────────────────────────────────────────── */
  async function _fetchPgn() {
    if (_lastPgn) return _lastPgn;   // use cached result from this render cycle
    if (_loading) return null;
    _loading = true;
    _setStatus('Fetching games from Lichess…', 'loading');

    try {
      const url = `/api/analytics?period=${_period}&result=${_result}&max=500`;
      const res  = await fetch(url);
      if (!res.ok) {
        let msg;
        try { msg = (await res.json()).error; } catch (_) { msg = res.statusText; }
        throw new Error(msg || `Server error ${res.status}`);
      }
      const data = await res.json();
      if (!data.pgn) throw new Error('No games found for this filter');

      _lastPgn   = data.pgn;
      _lastCount = data.count;
      _setStatus(`Found ${data.count} game${data.count !== 1 ? 's' : ''} — ${_label()}`, 'ok');
      return data.pgn;
    } catch (e) {
      _setStatus(`✗ ${e.message}`, 'err');
      return null;
    } finally {
      _loading = false;
    }
  }

  /* ── file list ────────────────────────────────────────────────────────── */
  async function _loadFiles() {
    const list = document.getElementById('analytics-file-list');
    if (!list) return;

    try {
      const res   = await fetch('/api/analytics/files');
      if (!res.ok) { list.innerHTML = '<span class="analytics-empty">No saved files yet</span>'; return; }
      const files = await res.json();

      if (!files.length) {
        list.innerHTML = '<span class="analytics-empty">No saved files yet</span>';
        return;
      }

      list.innerHTML = files.map(f => `
        <div class="analytics-file-row">
          <span class="analytics-file-name">${App.esc(f.name)}</span>
          <span class="analytics-file-meta">${_fmtBytes(f.size)} &middot; ${_fmtDate(f.mtime)}</span>
        </div>
      `).join('');
    } catch (_) {
      /* ignore */
    }
  }

  /* ── helpers ─────────────────────────────────────────────────────────── */
  function _setStatus(msg, cls = '') {
    const el = document.getElementById('analytics-status');
    if (!el) return;
    el.textContent = msg;
    el.className   = `analytics-status${cls ? ' ' + cls : ''}`;
  }

  function _label() {
    const r = RESULTS.find(x => x.id === _result)?.label ?? _result;
    const p = PERIODS.find(x => x.id === _period)?.label ?? _period;
    return `${r} · ${p}`;
  }

  function _fmtBytes(n) {
    if (n < 1024)        return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1024 / 1024).toFixed(1)} MB`;
  }

  function _fmtDate(iso) {
    try { return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }); }
    catch (_) { return iso.slice(0, 10); }
  }

  /* ── register ──────────────────────────────────────────────────────────── */
  App.registerTab('analytics', { show, onEvent });
  return { show, onEvent };
})();
