/**
 * app.js — SSE client, global state, tab router, shared helpers.
 *
 * Loaded first. Exposes `App` on window. All tab modules read from
 * App.games and register via App callbacks.
 */
'use strict';

const App = (() => {

  /* ── state ──────────────────────────────────────────────────────────── */
  const games   = new Map();                 // gameId → GameRecord
  const logs    = [];                        // { level, msg, ts, gameId }
  const MAX_LOGS = 500;
  let activeTab    = 'game';
  let evtSource    = null;
  let clockTimer   = null;
  let buildVersion = null;

  /* ── DOM refs ───────────────────────────────────────────────────────── */
  let dotEl, labelEl, statsEl;

  /* ── init ────────────────────────────────────────────────────────────── */
  function init() {
    dotEl   = document.getElementById('status-dot');
    labelEl = document.getElementById('status-label');
    statsEl = document.getElementById('session-stats');

    // tabs
    document.querySelectorAll('.tab').forEach(btn => {
      btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    });

    connect();

    // Build version badge
    fetch('/api/version')
      .then(r => r.ok ? r.json() : null)
      .then(v => {
        if (!v) return;
        buildVersion = v;
        const el = document.getElementById('build-version');
        if (el && v.build != null) el.textContent = `build #${v.build}`;
      })
      .catch(() => {});

    // clock ticker — runs every 100 ms, decrements the active side's display
    clockTimer = setInterval(tickClocks, 100);

    // Trigger show() on the default active tab so its DOM (including the
    // log feed element) is created before the first SSE events arrive.
    // All tab modules have already registered by this point (scripts are
    // loaded synchronously before DOMContentLoaded fires).
    const initMod = _tabModules[activeTab];
    if (initMod && initMod.show) initMod.show();
  }

  /* ── SSE ─────────────────────────────────────────────────────────────── */
  function connect() {
    if (evtSource) { try { evtSource.close(); } catch (_) {} }
    evtSource = new EventSource('/events');
    evtSource.onopen = () => {
      dotEl.className = 'connected';
      labelEl.textContent = 'live';
    };
    evtSource.onerror = () => {
      dotEl.className = 'disconnected';
      labelEl.textContent = 'disconnected';
    };
    evtSource.onmessage = (e) => {
      try { dispatch(JSON.parse(e.data)); } catch (_) {}
    };
  }

  /* ── dispatch ────────────────────────────────────────────────────────── */
  function dispatch({ type, data }) {
    switch (type) {

      case 'snapshot':
        games.clear();
        for (const g of data.games) {
          games.set(g.id, g);
          // Bootstrap client-only search history from last known state so tab-engine shows data on reconnect
          if (g.searchLive && !g._searchHistory) g._searchHistory = [g.searchLive];
        }
        // Restore log history from server-side ring buffer
        logs.length = 0;
        if (data.logs) for (const l of data.logs) logs.push(l);
        _updateStats();
        _notifyAll('snapshot', data);
        break;

      case 'game_start':
        games.set(data.id, data);
        _updateStats();
        _notifyAll('game_start', data);
        break;

      case 'game_end': {
        const g = games.get(data.gameId);
        if (g) { g.status = 'finished'; g.result = data.result; g.resultReason = data.reason; g.endedAt = Date.now(); }
        _updateStats();
        _notifyAll('game_end', data);
        break;
      }

      case 'game_meta': {
        const g = games.get(data.gameId);
        if (g) {
          if (data.ourRating   != null) g.ourRating   = data.ourRating;
          if (data.oppRating   != null) g.oppRating   = data.oppRating;
          if (data.rated       != null) g.rated       = data.rated;
          if (data.timeControl != null) g.timeControl = data.timeControl;
          // Sync the displayed FEN to the real start FEN before any moves are
          // played so custom/odds positions (all-knights, etc.) render correctly.
          if (data.initialFen != null) {
            g.initialFen = data.initialFen;
            if (!g.fullMoves || g.fullMoves.length === 0) g.fen = data.initialFen;
          }
        }
        _notifyAll('game_meta', data);
        break;
      }

      case 'move': {
        const g = games.get(data.gameId);
        if (g) {
          if (!g.moves) g.moves = [];
          g.moves.push(data.moveStat);
          // Also track in fullMoves so the move list shows both sides.
          // moveStat.ply is 1-indexed; convert to 0-indexed array position.
          const ms = data.moveStat;
          if (ms.move && ms.ply != null) {
            if (!g.fullMoves) g.fullMoves = [];
            const ply0 = ms.ply - 1;
            while (g.fullMoves.length <= ply0) g.fullMoves.push(null);
            g.fullMoves[ply0] = ms.move;
          }
        }
        _notifyAll('move', data);
        break;
      }

      case 'opponent_move': {
        const g = games.get(data.gameId);
        if (g) {
          if (!g.fullMoves) g.fullMoves = [];
          while (g.fullMoves.length <= data.ply) g.fullMoves.push(null);
          g.fullMoves[data.ply] = data.move;
        }
        _notifyAll('opponent_move', data);
        break;
      }

      case 'clock_update': {
        const g = games.get(data.gameId);
        if (g) {
          g.clock = { wtime: data.wtime, btime: data.btime };
          g._clockSnap = Date.now(); // when we last got server clock
        }
        _notifyAll('clock_update', data);
        break;
      }

      case 'search_start': {
        const g = games.get(data.gameId);
        if (g) { g.searchLive = null; g.confidence = null; g.pondering = false; g._ponderMoveUci = null; g._searchProfile = data.profile; g._searchHistory = []; }
        _notifyAll('search_start', data);
        break;
      }

      case 'search_info': {
        const g = games.get(data.gameId);
        if (g) {
          g.searchLive = data;
          g.confidence = data.confidence ?? g.confidence;
          // Keep pondering flag consistent — ponder_end clears it, not search_info
          if (!data.ponder) g.pondering = false;
          if (!g._searchHistory) g._searchHistory = [];
          g._searchHistory.push(data);
        }
        _notifyAll('search_info', data);
        break;
      }

      case 'search_end': {
        const g = games.get(data.gameId);
        if (g) { g.searchLive = null; g.confidence = data.finalConf; }
        _notifyAll('search_end', data);
        break;
      }

      case 'ponder_start': {
        const g = games.get(data.gameId);
        if (g) {
          g.pondering = true;
          g._ponderFromDepth = data.fromDepth ?? 0;
          g._ponderMoveUci   = data.ponderMove ?? null;
        }
        _notifyAll('ponder_start', data);
        break;
      }

      case 'ponder_end': {
        const g = games.get(data.gameId);
        if (g) { g.pondering = false; g.searchLive = null; g._ponderMoveUci = null; }
        _notifyAll('ponder_end', data);
        break;
      }

      case 'fen_update': {
        const g = games.get(data.gameId);
        if (g) g.fen = data.fen;
        _notifyAll('fen_update', data);
        break;
      }

      case 'chat_line': {
        const g = games.get(data.gameId);
        if (g) { if (!g.chat) g.chat = []; g.chat.push(data); }
        _notifyAll('chat_line', data);
        break;
      }

      case 'log':
        logs.push(data);
        if (logs.length > MAX_LOGS) logs.shift();
        _notifyAll('log', data);
        break;

      // ── Selfplay match events ────────────────────────────────────────────────
      // Standard game events (game_start, move, opponent_move, search_info,
      // clock_update, game_end) are now fired directly by SelfPlayAdapter via
      // the store, so no translation shim is needed here.
      // The sp_* variants are forwarded for the sidebar standings / config UI.

      case 'sp_game_start':
        _notifyAll('sp_game_start', data);
        break;

      case 'sp_move':
        _notifyAll('sp_move', data);
        break;

      case 'sp_info':
        _notifyAll('sp_info', data);
        break;

      case 'sp_game_end':
        _notifyAll('sp_game_end', data);
        break;

      case 'sp_state': {
        // Reconnect recovery: game is now in the store, so the SSE snapshot
        // already includes any in-progress game.  sp_state is forwarded for
        // sidebar config / standings only.
        _notifyAll('sp_state', data);
        break;
      }

      default:
        // Forward any unrecognised event type (e.g. challenge_sent,
        // challenge_declined, challenge_canceled) directly to all tabs.
        _notifyAll(type, data);
        break;
    }
  }

  /* ── tab switching ──────────────────────────────────────────────────── */
  function switchTab(name) {
    activeTab = name;
    document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
    document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === `panel-${name}`));
    // notify the freshly-shown tab to do a full render
    const mod = _tabModules[name];
    if (mod && mod.show) mod.show();
  }

  /* ── tab module registry ────────────────────────────────────────────── */
  const _tabModules = {};
  function registerTab(name, mod) { _tabModules[name] = mod; }

  /** Notify every registered tab of an event */
  function _notifyAll(type, data) {
    for (const [name, mod] of Object.entries(_tabModules)) {
      if (mod.onEvent) mod.onEvent(type, data);
    }
  }

  /* ── get current game (active, or most-recently started) ────────────── */
  function currentGame() {
    let best = null;
    for (const g of games.values()) {
      if (g.status === 'active') return g;
      if (!best || (g.startedAt ?? 0) > (best.startedAt ?? 0)) best = g;
    }
    return best;
  }

  /* ── clock ticker ───────────────────────────────────────────────────── */
  function tickClocks() {
    const g = currentGame();
    if (!g || g.status !== 'active' || !g.clock || !g._clockSnap) return;
    // determine whose turn it is from FEN
    const fen = g.fen || '';
    const turn = (fen.split(' ')[1]) || 'w';
    const elapsed = Date.now() - g._clockSnap;
    // compute display times
    const wDisplay = turn === 'w' ? Math.max(0, g.clock.wtime - elapsed) : g.clock.wtime;
    const bDisplay = turn === 'b' ? Math.max(0, g.clock.btime - elapsed) : g.clock.btime;
    g._wDisplay = wDisplay;
    g._bDisplay = bDisplay;
    g._turn = turn;
    // let game tab update if visible
    const mod = _tabModules['game'];
    if (mod && mod.onClockTick && activeTab === 'game') mod.onClockTick(g);
  }

  /* ── session stats ──────────────────────────────────────────────────── */
  function _updateStats() {
    let w = 0, d = 0, l = 0;
    for (const g of games.values()) {
      if (isWin(g)) w++;
      else if (g.result === '1/2-1/2') d++;
      else if (isLoss(g)) l++;
    }
    if (statsEl) statsEl.textContent = `${w}W ${d}D ${l}L  ·  ${games.size} games`;
  }

  /* ── helpers (shared) ───────────────────────────────────────────────── */
  function evalStr(m) {
    if (!m) return '–';
    if (m.mate != null) {
      if (m.mate === 0) return '#';          // terminal checkmate position
      return `M${Math.abs(m.mate)}`;
    }
    if (m.eval_cp != null) { const cp = m.eval_cp / 100; return (cp >= 0 ? '+' : '') + cp.toFixed(2); }
    return '–';
  }

  function evalPct(cpOrMate, isMate) {
    if (isMate) {
      // mate=0 means the position IS checkmate — terminal, no directional info here.
      // Return sentinel 50 so callers that need direction handle it themselves.
      if (cpOrMate === 0) return 50;
      // Scale by mate distance: M1→99/1, further mates taper toward 95/5
      const dist = Math.abs(cpOrMate);
      const peg = Math.max(95, 100 - dist);   // M1→99, M5→95 (floor 95)
      return cpOrMate > 0 ? peg : (100 - peg);
    }
    const clamped = Math.max(-600, Math.min(600, cpOrMate));
    return Math.round(50 + (clamped / 600) * 48); // range 2–98
  }

  function fmtClock(ms) {
    if (ms == null) return '–:––';
    const t = Math.max(0, Math.floor(ms / 1000));
    const m = Math.floor(t / 60);
    const s = t % 60;
    if (m >= 60) { const h = Math.floor(m / 60); return `${h}:${(m % 60).toString().padStart(2,'0')}:${s.toString().padStart(2,'0')}`; }
    return `${m}:${s.toString().padStart(2, '0')}`;
  }

  function fmtMs(ms) {
    if (ms == null) return '–';
    if (ms >= 60000) return (ms / 60000).toFixed(1) + 'm';
    if (ms >= 1000) return (ms / 1000).toFixed(1) + 's';
    return ms + 'ms';
  }

  function fmtN(n) {
    if (n >= 1e9) return (n / 1e9).toFixed(1) + 'G';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(0) + 'k';
    return String(n);
  }

  function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

  function isWin(g)  { return g.result && ((g.color==='white' && g.result==='1-0') || (g.color==='black' && g.result==='0-1')); }
  function isLoss(g) { return g.result && ((g.color==='white' && g.result==='0-1') || (g.color==='black' && g.result==='1-0')); }

  return {
    init, games, logs, currentGame, switchTab, registerTab, activeTab: () => activeTab,
    evalStr, evalPct, fmtClock, fmtMs, fmtN, esc, isWin, isLoss,
    getBuildVersion: () => buildVersion,
  };
})();

window.addEventListener('DOMContentLoaded', () => App.init());
