/**
 * tab-history.js — Persistent game history viewer.
 *
 * Two-pane layout:
 *   Left  — paginated game list (filters: All | Wins | Losses | Draws + opponent search)
 *   Right — selected game detail: SVG board + clickable SAN move list
 *
 * Click a game row to open it. Click any move (or use ←/→ keys) to step
 * through positions on the board.
 */
'use strict';

const TabHistory = (() => {
  const STANDARD_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
  const PAGE = 50;

  // ── state ──────────────────────────────────────────────────────────────
  let _games    = [];
  let _offset   = 0;
  let _hasMore  = true;
  let _loading  = false;
  let _filter   = { bot_result: '', opponent: '' };

  // Detail pane state
  let _detailRow  = null;   // currently open game_history row
  let _fens       = [];     // _fens[0] = initial, _fens[i] = FEN after move i
  let _sanList    = [];     // SAN strings (0-indexed parallel to UCI moves)
  let _ply        = 0;      // currently displayed ply (0 = start, N = after move N)
  let _boardInst  = null;   // Board instance for detail pane
  let _flipped    = false;  // board orientation

  // ── DOM helpers ────────────────────────────────────────────────────────
  const panel    = () => document.getElementById('panel-history');
  const listBody = () => document.getElementById('hist-list-body');
  const detailEl = () => document.getElementById('hist-detail');

  /* ── lifecycle ──────────────────────────────────────────────────────── */

  function show() {
    const p = panel();
    if (!p.dataset.init) {
      p.dataset.init = '1';
      _buildLayout(p);
      _fetchStats();
      _fetchGames(true);
    }
  }

  function onEvent(type) {
    if (App.activeTab() !== 'history') return;
    if (type === 'game_end') { _fetchStats(); _fetchGames(true); }
  }

  /* ── layout ─────────────────────────────────────────────────────────── */

  function _buildLayout(p) {
    p.innerHTML = `
      <div class="hist-top">
        <div class="hist-stats-row" id="hist-stats"></div>
        <input class="hist-search" id="hist-search" type="text" placeholder="Search opponent…" />
      </div>
      <div class="hist-body">
        <div class="hist-list-col">
          <div class="hist-list-scroll">
            <table class="hist-list-table">
              <thead><tr>
                <th>Date</th><th>Opponent</th><th>TC</th><th>C</th><th>Result</th><th>N</th>
              </tr></thead>
              <tbody id="hist-list-body"></tbody>
            </table>
          </div>
          <button class="hist-load-more" id="hist-load-more" style="display:none">↓ Load more</button>
        </div>
        <div class="hist-detail-col" id="hist-detail">
          <div class="hist-placeholder">← Select a game to view moves</div>
        </div>
      </div>
    `;

    // Stat pills are the filters — event delegation so it works after async re-render
    document.getElementById('hist-stats').addEventListener('click', e => {
      const btn = e.target.closest('[data-r]');
      if (!btn) return;
      _filter.bot_result = btn.dataset.r;
      document.querySelectorAll('#hist-stats [data-r]').forEach(b =>
        b.classList.toggle('active', b.dataset.r === _filter.bot_result)
      );
      _fetchGames(true);
    });

    // Opponent search (debounced)
    let _st;
    document.getElementById('hist-search').addEventListener('input', e => {
      clearTimeout(_st);
      _st = setTimeout(() => { _filter.opponent = e.target.value.trim(); _fetchGames(true); }, 350);
    });

    document.getElementById('hist-load-more').addEventListener('click', () => _fetchGames(false));

    // Keyboard navigation when history tab is active
    document.addEventListener('keydown', _onKeyDown);
  }

  /* ── keyboard navigation ─────────────────────────────────────────────── */

  function _onKeyDown(e) {
    if (App.activeTab() !== 'history') return;
    if (!_detailRow || !_fens.length) return;
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    if (e.key === 'ArrowLeft')  { e.preventDefault(); _showPly(_ply - 1); }
    if (e.key === 'ArrowRight') { e.preventDefault(); _showPly(_ply + 1); }
    if (e.key === 'Home')       { e.preventDefault(); _showPly(0); }
    if (e.key === 'End')        { e.preventDefault(); _showPly(_fens.length - 1); }
  }

  /* ── stats banner ────────────────────────────────────────────────────── */

  async function _fetchStats() {
    try {
      const s = await fetch('/api/gamedb/stats').then(r => r.json());
      const el = document.getElementById('hist-stats');
      if (!el) return;
      const w = s.botWins   ?? s.wins;
      const l = s.botLosses ?? s.losses;
      const d = s.botDraws  ?? s.draws;
      const a = (r) => _filter.bot_result === r ? ' active' : '';
      el.innerHTML = `
        <button class="hist-stat-pill${a('')}" data-r="">All <strong>${s.total}</strong></button>
        <button class="hist-stat-pill win${a('win')}" data-r="win">Wins <strong>${w}</strong></button>
        <button class="hist-stat-pill loss${a('loss')}" data-r="loss">Losses <strong>${l}</strong></button>
        <button class="hist-stat-pill draw${a('draw')}" data-r="draw">Draws <strong>${d}</strong></button>
      `;
    } catch (_) {}
  }

  /* ── game list ───────────────────────────────────────────────────────── */

  async function _fetchGames(reset) {
    if (_loading) return;
    _loading = true;
    if (reset) { _games = []; _offset = 0; _hasMore = true; }

    const qs = new URLSearchParams({ limit: String(PAGE), offset: String(_offset) });
    if (_filter.bot_result) qs.set('bot_result', _filter.bot_result);
    if (_filter.opponent)   qs.set('opponent',   _filter.opponent);

    try {
      const rows = await fetch(`/api/gamedb?${qs}`).then(r => r.json());
      _games  = reset ? rows : [..._games, ...rows];
      _offset += rows.length;
      _hasMore = rows.length === PAGE;
      _renderList();
    } catch (e) { console.error('[history]', e); }
    _loading = false;
  }

  function _renderList() {
    const tb = listBody();
    if (!tb) return;
    const lm = document.getElementById('hist-load-more');

    if (_games.length === 0) {
      tb.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:1rem">No games found</td></tr>`;
      if (lm) lm.style.display = 'none';
      return;
    }

    tb.innerHTML = _games.map((g, i) => {
      const date  = (g.date_utc || '?').slice(5);   // MM-DD
      const opp   = App.esc(g.opponent || '?');
      // Show clock string (e.g. '3+2') when available; fall back to speed category
      const tcRaw = g.time_control;
      const tc    = tcRaw && tcRaw !== 'correspondence'
        ? tcRaw
        : ({ bullet:'Blt', blitz:'Blz', rapid:'Rpd', classical:'Cls',
             correspondence:'Cor', unknown:'?' }[g.speed] ?? g.speed ?? '–');
      const col   = g.our_color === 'white' ? '♔' : g.our_color === 'black' ? '♚' : '–';
      const badge = _badge(g);
      const sel   = _detailRow && _detailRow.id === g.id ? ' sel' : '';
      return `<tr class="hist-row${sel}" data-idx="${i}">
        <td class="mono">${date}</td>
        <td>${opp}</td>
        <td class="mono">${tc}</td>
        <td style="text-align:center">${col}</td>
        <td>${badge}</td>
        <td class="mono" style="text-align:right">${g.ply_count || 0}</td>
      </tr>`;
    }).join('');

    tb.querySelectorAll('.hist-row').forEach(row => {
      row.addEventListener('click', () => {
        tb.querySelectorAll('.hist-row').forEach(r => r.classList.remove('sel'));
        row.classList.add('sel');
        _openGame(_games[+row.dataset.idx]);
      });
    });

    if (lm) lm.style.display = _hasMore ? 'block' : 'none';
  }

  /* ── game detail ─────────────────────────────────────────────────────── */

  async function _openGame(g) {
    _detailRow = g;
    _ply = 0;
    _fens = [];
    _sanList = [];

    if (_boardInst) { _boardInst.destroy(); _boardInst = null; }
    const d = detailEl();
    d.innerHTML = `<div class="hist-placeholder">Loading…</div>`;

    try {
      const data = await fetch(`/api/gamedb/game/${encodeURIComponent(g.id)}`).then(r => r.json());
      if (data.error) throw new Error(data.error);

      const startFen = data.initial_fen || STANDARD_FEN;
      const uciMoves = Array.isArray(data.full_moves) ? data.full_moves.filter(Boolean) : [];

      _fens    = _buildFens(startFen, uciMoves);
      _sanList = San.buildSanList(startFen, uciMoves);
      _flipped = (g.our_color === 'black');

      _renderDetail(d, g, uciMoves);
      _showPly(0);
    } catch (e) {
      d.innerHTML = `<div class="hist-placeholder">Error: ${App.esc(e.message)}</div>`;
    }
  }

  /* Build FEN list: fens[0] = startFen, fens[i] = FEN after i-th UCI move */
  function _buildFens(startFen, uciMoves) {
    const fens = [startFen];
    let pos = San.parseFen(startFen);
    for (const uci of uciMoves) {
      pos = San.applyMove(pos, uci);
      fens.push(San.posToFen(pos));
    }
    return fens;
  }

  function _renderDetail(d, g, uciMoves) {
    const opp      = App.esc(g.opponent || '?');
    const tc       = g.time_control || '–';
    const date     = g.date_utc || '?';
    const badge    = _badge(g);
    const colLabel = g.our_color === 'white' ? '♔ White' : g.our_color === 'black' ? '♚ Black' : '';
    const monthKey = g.month_key || '';
    const total    = uciMoves.length;

    d.innerHTML = `
      <div class="hist-det-header">
        <span class="hist-det-opp">${opp}</span>
        <span class="hist-det-meta">${tc} · ${date} · ${colLabel}${g.engine_build != null ? ` · B${g.engine_build}` : ''}${g.reason ? ` · ${App.esc(_fmtReason(g.reason))}` : ''}</span>
        ${badge}
        ${g.id ? `<a class="hist-pgn-link" href="/api/gamedb/game/${encodeURIComponent(g.id)}/pgn" download>↓ PGN</a>` : ''}
      </div>
      <div class="hist-det-main">
        <div class="hist-board-area">
          <div class="hist-board-wrap" id="hist-board-wrap">
            <button class="hist-flip-btn" id="hist-flip-btn" title="Flip board">⇅</button>
          </div>
        </div>
        <div class="hist-move-col">
          <div class="hist-nav-bar">
            <button class="hist-nav" id="hn-start" title="Start (Home)">⏮</button>
            <button class="hist-nav" id="hn-prev"  title="Back (←)">◀</button>
            <span   class="hist-nav-ply" id="hn-ply">0 / ${total}</span>
            <button class="hist-nav" id="hn-next"  title="Forward (→)">▶</button>
            <button class="hist-nav" id="hn-end"   title="End (End)">⏭</button>
            <button class="hist-nav hist-fen-btn" id="hist-fen-copy" title="Copy FEN to clipboard">Copy FEN</button>
          </div>
          <div class="hist-move-list" id="hist-move-list"></div>
        </div>
      </div>
    `;

    // Create board instance
    const wrap = document.getElementById('hist-board-wrap');
    _boardInst = Board.create(wrap);

    // Flip button
    document.getElementById('hist-flip-btn').addEventListener('click', () => {
      _flipped = !_flipped;
      if (_boardInst && _fens.length) _boardInst.update(_fens[_ply], { flip: _flipped });
    });

    _renderMoveTable();

    document.getElementById('hn-start').addEventListener('click', () => _showPly(0));
    document.getElementById('hn-prev').addEventListener('click',  () => _showPly(_ply - 1));
    document.getElementById('hn-next').addEventListener('click',  () => _showPly(_ply + 1));
    document.getElementById('hn-end').addEventListener('click',   () => _showPly(_fens.length - 1));

    document.getElementById('hist-fen-copy').addEventListener('click', function() {
      const fen = _fens[_ply];
      if (!fen) return;
      navigator.clipboard.writeText(fen).then(() => {
        this.textContent = 'Copied!';
        this.classList.add('copied');
        setTimeout(() => { this.textContent = 'Copy FEN'; this.classList.remove('copied'); }, 1500);
      }).catch(() => {
        // Fallback for non-secure contexts
        const ta = document.createElement('textarea');
        ta.value = fen; ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        this.textContent = 'Copied!';
        this.classList.add('copied');
        setTimeout(() => { this.textContent = 'Copy FEN'; this.classList.remove('copied'); }, 1500);
      });
    });
  }

  function _renderMoveTable() {
    const el = document.getElementById('hist-move-list');
    if (!el) return;

    if (_sanList.length === 0) {
      el.innerHTML = '<div class="hist-no-moves">No moves stored for this game.</div>';
      return;
    }

    let html = '<table class="hist-moves-table"><tbody>';
    for (let i = 0; i < _sanList.length; i += 2) {
      const num  = Math.floor(i / 2) + 1;
      const wSan = App.esc(_sanList[i]     || '');
      const bSan = App.esc(_sanList[i + 1] || '');
      // ply: after white move i → ply i+1, after black move i+1 → ply i+2
      html += `<tr>
        <td class="hist-mn">${num}.</td>
        <td class="hist-mv" data-ply="${i + 1}">${wSan}</td>
        <td class="hist-mv" data-ply="${i + 2}">${bSan}</td>
      </tr>`;
    }
    html += '</tbody></table>';
    el.innerHTML = html;

    el.querySelectorAll('.hist-mv').forEach(cell => {
      if (cell.textContent.trim()) {
        cell.addEventListener('click', () => _showPly(+cell.dataset.ply));
      }
    });
  }

  /* ── ply navigation ──────────────────────────────────────────────────── */

  function _showPly(ply) {
    if (!_fens.length) return;
    _ply = Math.max(0, Math.min(_fens.length - 1, ply));

    if (_boardInst) _boardInst.update(_fens[_ply], { flip: _flipped });

    const plyEl = document.getElementById('hn-ply');
    if (plyEl) plyEl.textContent = `${_ply} / ${_fens.length - 1}`;

    const moveList = document.getElementById('hist-move-list');
    if (moveList) {
      moveList.querySelectorAll('.hist-mv').forEach(cell => {
        cell.classList.toggle('active', +cell.dataset.ply === _ply);
      });
      const active = moveList.querySelector('.hist-mv.active');
      if (active) active.scrollIntoView({ block: 'nearest' });
    }
  }

  /* ── helpers ─────────────────────────────────────────────────────────── */

  const REASON_LABELS = {
    mate:                 'Checkmate',
    stalemate:            'Stalemate',
    repetition:           'Threefold repetition',
    insufficient:         'Insufficient material',
    insufficientMaterial: 'Insufficient material',
    fiftyMoves:           'Fifty-move rule',
    draw:                 'Draw',
    outoftime:            'Timeout',
    resign:               'Resignation',
    aborted:              'Aborted',
    noStart:              'No start',
    cheat:                'Cheat detected',
    timeout:              'Timeout',
    variantEnd:           'Variant end',
  };

  function _fmtReason(raw) {
    return REASON_LABELS[raw] ?? raw ?? '';
  }

  function _badge(g) {
    const r = g.bot_result;
    const reason = g.reason ? ` <span style="font-size:0.75em;opacity:0.75">(${App.esc(_fmtReason(g.reason))})</span>` : '';
    if (r === 'win')  return `<span class="badge win">Win${reason}</span>`;
    if (r === 'loss') return `<span class="badge loss">Loss${reason}</span>`;
    if (r === 'draw') return `<span class="badge draw">Draw${reason}</span>`;
    if (!g.result)    return '<span class="badge" style="background:var(--accent-dim);color:var(--accent)">Live</span>';
    return `<span class="badge">${App.esc(g.result)}</span>`;
  }

  /* ── register ────────────────────────────────────────────────────────── */

  App.registerTab('history', { show, onEvent });
  return { show, onEvent };
})();
