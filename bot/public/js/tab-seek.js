'use strict';

/**
 * tab-seek.js — Auto-challenger UI.
 *
 * Controls: ON/OFF, Rated, TC chips, Elo range.
 * Shows: active challenge banner, bot table (sorted by relevance), log.
 * No manual add/remove/pause/resume. All bots auto-discovered.
 */
const TabSeek = (() => {

  let _state          = null;
  let _refreshTimer   = null;
  let _countdownTimer = null;

  const panel = () => document.getElementById('comms-pane-seek');

  const TC_PRESETS = [
    { id: '1+0',   label: '1+0',   cls: 'bullet' },
    { id: '2+1',   label: '2+1',   cls: 'bullet' },
    { id: '3+2',   label: '3+2',   cls: 'blitz'  },
    { id: '5+3',   label: '5+3',   cls: 'blitz'  },
    { id: '10+5',  label: '10+5',  cls: 'rapid'  },
    { id: '15+10', label: '15+10', cls: 'rapid'  },
  ];

  /* ─────────────────────────────────────────────────────────────────────
     Lifecycle
  ──────────────────────────────────────────────────────────────────────*/

  function show() {
    const isSp = _isSelfplayService();
    if (isSp) {
      // Selfplay match mode
      if (!_spShellReady || !document.getElementById('sp-toggle')) {
        _renderSelfplayShell();
      }
      _loadSpState();
      if (_refreshTimer) clearInterval(_refreshTimer);
      _refreshTimer = setInterval(_loadSpState, 5000);
    } else {
      // Normal challenger mode
      _spShellReady = false;
      if (!document.getElementById('sq-toggle')) _renderShell();
      _loadState();
      if (_refreshTimer) clearInterval(_refreshTimer);
      _refreshTimer = setInterval(_loadState, 3000);
    }
  }

  function onEvent(type, data) {
    if (type === 'queue_state') { _state = data; _render(); return; }
    if (type === 'challenge_declined' || type === 'challenge_canceled' || type === 'game_start') {
      _loadState();
    }

    // Selfplay match events
    if (type === 'sp_game_start') { _onSpGameStart(data); return; }
    if (type === 'sp_move')       { _onSpMove(data);      return; }
    if (type === 'sp_info')       { _onSpInfo(data);      return; }
    if (type === 'sp_game_end')   { _onSpGameEnd(data);   return; }
    if (type === 'sp_state') {
      _spMatchState = data.matchState;
      _renderSp();
      return;
    }
  }

  /* ─────────────────────────────────────────────────────────────────────
     Shell
  ──────────────────────────────────────────────────────────────────────*/

  function _renderShell() {
    const tcChips = TC_PRESETS.map(tc =>
      `<button class="sq-tc ${tc.cls}" data-tc="${tc.id}">${tc.label}</button>`
    ).join('');

    panel().innerHTML = `
      <!-- ── Top bar: service pills + action ─────────────────────── -->
      <div class="sq-top-bar">
        <div class="sq-svc-bar" id="sq-svc-bar"></div>
        <div class="sq-top-action">
          <span class="sq-lbl">Challenger</span>
          <div class="sq-sw" id="sq-toggle" title="Toggle auto-challenger">
            <div class="sq-sw-knob"></div>
          </div>
          <span class="sq-sw-lbl" id="sq-toggle-lbl">OFF</span>
        </div>
      </div>

      <!-- ── Controls section ─────────────────────────────────────── -->
      <div class="sq-controls">
        <div class="sq-ctrl-row">
          <div class="sq-sw sm" id="sq-rated" title="Rated / Casual">
            <div class="sq-sw-knob"></div>
          </div>
          <span class="sq-mini-lbl" id="sq-rated-lbl">Rated</span>
          <div class="sq-ctrl-div"></div>
          <div class="sq-color-sel" id="sq-color-sel">
            <button class="sq-color-btn" data-color="white" title="Play as white">W</button>
            <button class="sq-color-btn" data-color="random" title="Random color">A</button>
            <button class="sq-color-btn" data-color="black" title="Play as black">B</button>
          </div>
          <div class="sq-ctrl-div"></div>
          <div class="sq-tc-row" id="sq-tc-chips">${tcChips}</div>
        </div>
        <div class="sq-ctrl-row">
          <span class="sq-ctrl-lbl">Elo</span>
          <input type="number" id="sq-elo-min" class="sq-elo" placeholder="Min" />
          <span class="sq-elo-sep">\u2013</span>
          <input type="number" id="sq-elo-max" class="sq-elo" placeholder="Max" />
          <span class="sq-elo-info" id="sq-our-elo"></span>
          <div class="sq-ctrl-spacer"></div>
          <div class="sq-sp-fallback-wrap" id="sq-sp-fallback-wrap">
            <div class="sq-sw sm" id="sq-sp-fallback" title="Fall back to selfplay when no opponents are ready">
              <div class="sq-sw-knob"></div>
            </div>
            <span class="sq-mini-lbl">Selfplay fallback</span>
          </div>
          <button class="sq-icon-btn" id="sq-refresh" title="Refresh bot list">\u21bb</button>
          <button class="sq-icon-btn danger" id="sq-nuke-btn" title="Reset database">DB</button>
        </div>
      </div>

      <!-- ── Active banner ───────────────────────────────────────── -->
      <div class="sq-banner" id="sq-active" style="display:none">
        <span class="sq-banner-dot"></span>
        <span id="sq-active-text">\u2014</span>
        <span class="sq-banner-timer" id="sq-active-timer"></span>
        <button class="sq-banner-cancel" id="sq-cancel-active">\u2715 Cancel</button>
      </div>

      <!-- ── Stats ───────────────────────────────────────────────────────── -->
      <div class="sq-stats" id="sq-stats"></div>

      <!-- ── Body: bots + log side by side ─────────────────────────────── -->
      <div class="sq-body">

        <!-- ── Bot table ──────────────────────────────────────────────── -->
        <div class="sq-sec">
          <div class="sq-sec-hdr">
            <span>Bots</span>
            <input type="text" id="sq-search" class="sq-search"
                   placeholder="Search\u2026" autocomplete="off" spellcheck="false" />
            <span class="sq-count" id="sq-bot-count"></span>
          </div>
          <div class="sq-bots" id="sq-bot-list"></div>
        </div>

        <!-- ── Log ────────────────────────────────────────────────────── -->
        <div class="sq-sec sq-sec-log">
          <div class="sq-sec-hdr">
            <span>Log</span>
            <span class="sq-badge" id="sq-log-count"></span>
          </div>
          <div class="sq-log-wrap" id="sq-log-list">
            <div class="sq-empty">No log entries</div>
          </div>
        </div>

      </div><!-- /.sq-body -->
    `;

    _bindEvents();
  }

  /* ─────────────────────────────────────────────────────────────────────
     Events
  ──────────────────────────────────────────────────────────────────────*/

  function _bindEvents() {
    // Toggle
    document.getElementById('sq-toggle')?.addEventListener('click', async () => {
      const el = document.getElementById('sq-toggle');
      const isOn = el?.classList.contains('on');
      await fetch(isOn ? '/api/queue/stop' : '/api/queue/start', { method: 'POST' });
      _loadState();
    });

    // Rated toggle
    const ratedEl = document.getElementById('sq-rated');
    ratedEl?.addEventListener('click', () => {
      ratedEl.classList.toggle('on');
      const rated = ratedEl.classList.contains('on');
      document.getElementById('sq-rated-lbl').textContent = rated ? 'Rated' : 'Casual';
      _patch({ rated });
    });

    // TC chips
    document.getElementById('sq-tc-chips')?.addEventListener('click', e => {
      const chip = e.target.closest('.sq-tc');
      if (!chip) return;
      chip.classList.toggle('active');
      const enabledTCs = [];
      document.querySelectorAll('.sq-tc.active').forEach(c => enabledTCs.push(c.dataset.tc));
      _patch({ enabledTCs });
    });

    // Color selector (W = white, A = auto/random, B = black)
    document.getElementById('sq-color-sel')?.addEventListener('click', e => {
      const btn = e.target.closest('.sq-color-btn');
      if (!btn) return;
      document.querySelectorAll('.sq-color-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      _patch({ color: btn.dataset.color });
    });

    // Elo range
    const deb = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

    document.getElementById('sq-elo-min')?.addEventListener('input', deb(() => {
      const v = parseInt(document.getElementById('sq-elo-min').value, 10);
      _patch({ eloMin: isNaN(v) ? null : v });
    }, 500));

    document.getElementById('sq-elo-max')?.addEventListener('input', deb(() => {
      const v = parseInt(document.getElementById('sq-elo-max').value, 10);
      _patch({ eloMax: isNaN(v) ? null : v });
    }, 500));

    // Search
    document.getElementById('sq-search')?.addEventListener('input', deb(() => _render(), 200));

    // Refresh
    document.getElementById('sq-refresh')?.addEventListener('click', async () => {
      const btn = document.getElementById('sq-refresh');
      if (btn) { btn.disabled = true; btn.textContent = '\u23f3'; }
      await fetch('/api/queue/refresh', { method: 'POST' });
      if (btn) { btn.disabled = false; btn.textContent = '\u21bb'; }
      _loadState();
    });

    // Nuke
    document.getElementById('sq-nuke-btn')?.addEventListener('click', async () => {
      if (!confirm('Reset the bot database? Clears all bots and logs.')) return;
      await fetch('/api/queue/nuke', { method: 'POST' });
      _loadState();
    });

    // Selfplay fallback toggle
    document.getElementById('sq-sp-fallback')?.addEventListener('click', () => {
      const el = document.getElementById('sq-sp-fallback');
      if (!el) return;
      el.classList.toggle('on');
      _patch({ selfplayFallback: el.classList.contains('on') });
    });

    // Bot selection toggle (selfplay) — event delegation on the bot list
    document.getElementById('sq-bot-list')?.addEventListener('change', async e => {
      const cb = e.target.closest('.sq-sel-cb');
      if (cb) {
        await fetch('/api/queue/bots/select', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: cb.dataset.user, selected: cb.checked }),
        }).catch(() => {});
        _loadState();
        return;
      }

      const sel = e.target.closest('.sq-odds-sel');
      if (sel) {
        await fetch('/api/queue/bots/odds', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: sel.dataset.user, oddsLevel: parseInt(sel.value, 10) }),
        }).catch(() => {});
        // No full reload needed — just visual confirmation (value already set by browser)
      }
    });
  }

  /* ─────────────────────────────────────────────────────────────────────
     State
  ──────────────────────────────────────────────────────────────────────*/

  async function _loadState() {
    try {
      const res = await fetch('/api/queue');
      if (!res.ok) return;
      _state = await res.json();
      // If service switched to/from selfplay, rebuild the shell
      const isSp = _isSelfplayService();
      const hasSpShell  = !!document.getElementById('sp-toggle');
      const hasNrmShell = !!document.getElementById('sq-toggle');
      if (isSp && !hasSpShell)  { _spShellReady = false; _renderSelfplayShell(); _loadSpState(); return; }
      if (!isSp && !hasNrmShell){ _renderShell(); }
      _render();
    } catch {}
  }

  function _render() {
    if (!_state) return;
    if (_isSelfplayService()) { _renderSp(); return; }

    // Toggle
    const tEl = document.getElementById('sq-toggle');
    if (tEl) tEl.classList.toggle('on', _state.running);
    const tLbl = document.getElementById('sq-toggle-lbl');
    if (tLbl) tLbl.textContent = _state.running ? 'ON' : 'OFF';

    // Stamp service on the panel so CSS can scope per-service styles
    panel().dataset.svc = _state.settings?.service ?? 'lichess';

    _syncSettings(_state.settings);
    _renderServiceBar(_state.availableServices, _state.settings?.service);

    // Our elo
    if (_state.ourRatings) {
      const el = document.getElementById('sq-our-elo');
      if (el) {
        const r = _state.ourRatings;
        const fmt = v => v != null ? v : '\u2014';
        el.textContent = `You: ${fmt(r.bullet)}/${fmt(r.blitz)}/${fmt(r.rapid)}`;
      }
    }

    _renderActive(_state.active);
    _renderStats(_state.stats);
    _renderBots(_state.bots);
    _renderLog(_state.log);
  }

  function _syncSettings(s) {
    if (!s) return;
    const rEl = document.getElementById('sq-rated');
    if (rEl) {
      rEl.classList.toggle('on', s.rated);
      document.getElementById('sq-rated-lbl').textContent = s.rated ? 'Rated' : 'Casual';
    }
    document.querySelectorAll('.sq-tc').forEach(c =>
      c.classList.toggle('active', (s.enabledTCs || []).includes(c.dataset.tc))
    );
    // Color selector
    if (s.color != null) {
      document.querySelectorAll('.sq-color-btn').forEach(b =>
        b.classList.toggle('active', b.dataset.color === s.color)
      );
    }
    // Don't overwrite elo inputs while user is typing
    const minEl = document.getElementById('sq-elo-min');
    if (minEl && document.activeElement !== minEl && s.eloMin != null) minEl.value = s.eloMin;
    const maxEl = document.getElementById('sq-elo-max');
    if (maxEl && document.activeElement !== maxEl && s.eloMax != null) maxEl.value = s.eloMax;
    // Selfplay fallback toggle
    const spFbEl = document.getElementById('sq-sp-fallback');
    if (spFbEl) spFbEl.classList.toggle('on', s.selfplayFallback === true);
  }

  function _renderServiceBar(services, activeId) {
    const bar = document.getElementById('sq-svc-bar');
    if (!bar || !services?.length) return;
    // Only show the bar when more than one service is available
    if (services.length < 2) { bar.innerHTML = ''; return; }
    bar.innerHTML = services.map(s =>
      `<button class="sq-svc-pill${s.id === activeId ? ' active' : ''}" data-svc="${_esc(s.id)}">${_esc(s.name)}</button>`
    ).join('');
    bar.querySelectorAll('.sq-svc-pill').forEach(btn => {
      btn.addEventListener('click', () => {
        if (btn.classList.contains('active')) return;
        _patch({ service: btn.dataset.svc });
        _loadState();
      });
    });
  }

  /* ── Active banner ─────────────────────────────────────────────────── */

  function _renderActive(active) {
    const el = document.getElementById('sq-active');
    if (!el) return;
    if (!active) {
      el.style.display = 'none';
      if (_countdownTimer) { clearInterval(_countdownTimer); _countdownTimer = null; }
      return;
    }
    el.style.display = 'flex';
    document.getElementById('sq-active-text').textContent =
      `Challenging ${active.username}${active.tc ? ' (' + active.tc + ')' : ''}`;

    const sentAt = active.sentAt || Date.now();
    const timerEl = document.getElementById('sq-active-timer');
    const update = () => {
      const sec = Math.floor((Date.now() - sentAt) / 1000);
      if (timerEl) {
        timerEl.textContent = `${sec}s`;
        timerEl.classList.toggle('overdue', sec > 30);
      }
    };
    update();
    if (_countdownTimer) clearInterval(_countdownTimer);
    _countdownTimer = setInterval(update, 1000);

    // Cancel button
    const cancelBtn = document.getElementById('sq-cancel-active');
    if (cancelBtn) {
      const nb = cancelBtn.cloneNode(true);
      cancelBtn.replaceWith(nb);
      nb.addEventListener('click', async () => {
        nb.disabled = true; nb.textContent = '\u23f3';
        await fetch('/api/queue/cancel', { method: 'POST' }).catch(() => {});
        _loadState();
      });
    }
  }

  /* ── Stats ─────────────────────────────────────────────────────────── */

  function _renderStats(stats) {
    const el = document.getElementById('sq-stats');
    if (!el || !stats) return;
    el.innerHTML =
      `<span>Today <b>${stats.gamesToday}</b></span>` +
      `<span>Online <b>${stats.online}</b></span>` +
      `<span>Ready <b>${stats.ready}</b></span>` +
      `<span>Cooldown <b>${stats.cooling}</b></span>` +
      `<span>Known <b>${stats.total}</b></span>`;
  }

  /* ── Bot table ─────────────────────────────────────────────────────── */

  function _renderBots(bots) {
    const listEl = document.getElementById('sq-bot-list');
    const cntEl  = document.getElementById('sq-bot-count');
    if (!listEl || !bots) return;

    const q = (document.getElementById('sq-search')?.value ?? '').trim().toLowerCase();
    let list = bots;
    if (q) list = list.filter(b => (b.username || '').toLowerCase().includes(q));

    if (cntEl) cntEl.textContent = list.length;

    if (!list.length) {
      listEl.innerHTML = '<div class="sq-empty">No bots \u2014 hit \u21bb to refresh</div>';
      return;
    }

    const rows = list.slice(0, 250).map(b => {
      const name = _esc(b.username);

      let status, sCls;
      if (!b.online) {
        status = 'offline'; sCls = 'off';
      } else if (b.playing) {
        status = '\u2659 Playing\u2026'; sCls = 'playing';
      } else if (b.ready) {
        status = '\u2713 ready'; sCls = 'ok';
      } else {
        status = '\u23f3 ' + _fmtDur(b.cooldownLeft || 0); sCls = 'cd';
      }

      let last = '';
      if (b.last_outcome) {
        const ago = b.last_challenged ? _fmtAgo(b.last_challenged) : '';
        last = b.last_outcome + (ago ? ' ' + ago : '');
      }

      const r = v => v ? `${v}` : '\u2014';
      const rowCls = b.ready ? ' rdy' : !b.online ? ' off' : '';
      const pUrl = b.profileUrl ? b.profileUrl : '#';

      // Selection checkbox — only rendered for selfplay; CSS hides for other services
      const selCb = `<input type="checkbox" class="sq-sel-cb" data-user="${name}" ${b.selected !== 0 ? 'checked' : ''} title="Include in rotation">`;

      // Odds selector — only rendered when the bot has oddsLabels (selfplay).
      // When present, it replaces the last-result text so the column stays clean.
      const oddsSel = b.oddsLabels
        ? `<select class="sq-odds-sel" data-user="${name}" title="Odds level">${
            b.oddsLabels.map((lbl, i) =>
              `<option value="${i}"${i === (b.oddsLevel ?? 0) ? ' selected' : ''}>${_esc(lbl)}</option>`
            ).join('')
          }</select>`
        : '';

      return `<div class="sq-brow${rowCls}">
        <span class="sq-bc nm">${selCb}${b.online ? '<span class="sq-odot on"></span>' : '<span class="sq-odot"></span>'}<a class="sq-bl" href="${_esc(pUrl)}" target="_blank">${name}</a></span>
        <span class="sq-bc bu">${r(b.elo_bullet)}</span>
        <span class="sq-bc bl">${r(b.elo_blitz)}</span>
        <span class="sq-bc rp">${r(b.elo_rapid)}</span>
        <span class="sq-bc st ${sCls}">${status}</span>
        <span class="sq-bc last">${oddsSel || _esc(last)}</span>
      </div>`;
    });

    const isSelfplay = _state.settings?.service === 'selfplay';
    listEl.innerHTML = `
      <div class="sq-bth">
        <div class="sq-bc nm">Bot</div>
        <div class="sq-bc bu">Bul</div>
        <div class="sq-bc bl">Bli</div>
        <div class="sq-bc rp">Rap</div>
        <div class="sq-bc st">Status</div>
        <div class="sq-bc last">${isSelfplay ? 'Odds' : 'Last'}</div>
      </div>
      ${rows.join('')}
    `;
  }

  /* ── Log ────────────────────────────────────────────────────────────── */

  const LOG_CLR = {
    sent: 'var(--accent)', game_started: 'var(--green)', game_ended: 'var(--green)',
    declined: 'var(--red)', timeout: 'var(--yellow)', error: 'var(--red)',
    started: 'var(--green)', stopped: 'var(--muted)', refreshed: 'var(--muted)',
  };

  function _renderLog(entries) {
    const listEl = document.getElementById('sq-log-list');
    const cntEl  = document.getElementById('sq-log-count');
    if (!listEl) return;
    if (!entries?.length) {
      listEl.innerHTML = '<div class="sq-empty">No log entries</div>';
      if (cntEl) cntEl.textContent = '';
      return;
    }
    if (cntEl) cntEl.textContent = entries.length;
    listEl.innerHTML = entries.map(e => {
      const ts  = new Date(e.ts).toLocaleTimeString();
      const who = e.username ? `<span class="sq-log-who">${_esc(e.username)}</span>` : '';
      const clr = LOG_CLR[e.event] ?? 'var(--fg)';

      // Detail key-value pairs go into a separate body row below the header
      let body = '';
      try {
        const d = typeof e.detail === 'string' ? JSON.parse(e.detail) : e.detail;
        if (d && typeof d === 'object') {
          const parts = Object.entries(d).map(([k, v]) =>
            `<span class="sq-log-k">${k}</span>:${typeof v === 'object' ? JSON.stringify(v) : v}`
          );
          if (parts.length) body = `<div class="sq-log-body">${parts.join(' ')}</div>`;
        }
      } catch {}

      return `<div class="sq-log-card" title="${ts}">
        <div class="sq-log-hdr">
          <span class="sq-log-ev" style="color:${clr}">${e.event}</span>
          ${who}
        </div>${body}
      </div>`;
    }).join('');
  }

  /* ── Helpers ────────────────────────────────────────────────────────── */

  async function _patch(data) {
    try {
      await fetch('/api/queue/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      });
    } catch {}
  }

  function _esc(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function _fmtDur(ms) {
    if (ms < 0) return 'now';
    if (ms < 60_000) return `${Math.ceil(ms / 1000)}s`;
    if (ms < 3600_000) return `${Math.floor(ms / 60_000)}m`;
    const h = Math.floor(ms / 3600_000);
    const m = Math.floor((ms % 3600_000) / 60_000);
    return `${h}h${m ? ' ' + m + 'm' : ''}`;
  }

  function _fmtAgo(ts) {
    if (!ts) return '';
    const d = Date.now() - ts;
    if (d < 60_000) return 'now';
    if (d < 3600_000) return `${Math.floor(d / 60_000)}m ago`;
    if (d < 86400_000) return `${Math.floor(d / 3600_000)}h ago`;
    return `${Math.floor(d / 86400_000)}d ago`;
  }

  /* ─────────────────────────────────────────────────────────────────────
     Selfplay match orchestrator UI
  ──────────────────────────────────────────────────────────────────────*/

  // Selfplay-specific state
  let _spEngines    = [];
  let _spCategories = [];   // [{ id, name, locked, positions:[{id,name,fen}] }]
  let _spMatchState = null;
  let _spShellReady = false;
  let _spArchOpen   = false; // whether the archive build picker is expanded

  /** Collect all currently-selected engine IDs (cards + archive chips). */
  function _collectEnabledEngines() {
    const fromCards = Array.from(document.querySelectorAll('.sp-eng-card.sp-eng-selected')).map(c => c.dataset.id);
    const fromChips = Array.from(document.querySelectorAll('.sp-arch-chip.sp-arch-sel')).map(c => c.dataset.id);
    return [...fromCards, ...fromChips];
  }

  // Position editor state
  let _spPosBoard    = null;   // Board.js instance for position editor
  let _spSelCatId    = null;   // currently selected category ID
  let _spEditPosId   = null;   // position being edited (null = new)
  let _spEditCatId   = null;   // category of edited position
  let _spEditGrid    = null;   // 8×8 piece array for live editing
  let _spSelPiece    = 'P';    // currently selected piece to place
  let _spEditFenRest = 'w KQkq - 0 1'; // non-board FEN tokens

  const SP_PIECE_CYCLE = ['P','N','B','R','Q','K','p','n','b','r','q','k'];
  const SP_PIECE_GLYPHS = {
    K:'♔', Q:'♕', R:'♖', B:'♗', N:'♘', P:'♙',
    k:'♚', q:'♛', r:'♜', b:'♝', n:'♞', p:'♟',
  };

  // ── Position editor helpers (module-level so _loadSpState can call them) ──

  function _fenToGrid(fen) { return Board.parseFen(fen).board; }
  function _gridToFen(grid, rest) {
    const rows = grid.map(row => {
      let s = '', empty = 0;
      for (const p of row) {
        if (!p) { empty++; }
        else { if (empty) { s += empty; empty = 0; } s += p; }
      }
      if (empty) s += empty;
      return s;
    });
    return rows.join('/') + ' ' + (rest || 'w KQkq - 0 1');
  }
  function _updateEditCursor() {
    const el = document.getElementById('sp-edit-piece');
    if (!el) return;
    el.textContent = SP_PIECE_GLYPHS[_spSelPiece] ?? _spSelPiece;
    el.className = 'sp-edit-piece ' + (_spSelPiece === _spSelPiece.toUpperCase() ? 'sp-edit-w' : 'sp-edit-b');
  }
  function _attachBoardEditHandlers() {
    if (!_spPosBoard) return;
    const svg = _spPosBoard.svg;
    svg.addEventListener('wheel', e => {
      e.preventDefault();
      const dir = e.deltaY > 0 ? 1 : -1;
      const idx = SP_PIECE_CYCLE.indexOf(_spSelPiece);
      _spSelPiece = SP_PIECE_CYCLE[(idx + dir + SP_PIECE_CYCLE.length) % SP_PIECE_CYCLE.length];
      _updateEditCursor();
    }, { passive: false });
    svg.addEventListener('mousedown', e => {
      if (e.button !== 0) return;
      const editorEl = document.getElementById('sp-pos-editor');
      if (!editorEl || editorEl.style.display === 'none') return;
      e.preventDefault();
      const sq = _spPosBoard.squareAt(e);
      if (!sq || !_spEditGrid) return;
      if (e.shiftKey) {
        _spEditGrid[sq.rank][sq.file] = '';
      } else {
        const cur = _spEditGrid[sq.rank][sq.file];
        _spEditGrid[sq.rank][sq.file] = cur === _spSelPiece ? '' : _spSelPiece;
      }
      const fen = _gridToFen(_spEditGrid, _spEditFenRest);
      const fenInp = document.getElementById('sp-pos-fen-inp');
      if (fenInp) fenInp.value = fen;
      _spPosBoard.update(fen);
    });
  }

  function _rebuildCatSel() {
    const sel = document.getElementById('sp-cat-sel');
    if (!sel) return;
    sel.innerHTML = _spCategories.map(c =>
      `<option value="${_esc(c.id)}"${c.id === _spSelCatId ? ' selected' : ''}>${_esc(c.name)}</option>`
    ).join('');
    if (!_spSelCatId && _spCategories.length) _spSelCatId = _spCategories[0].id;
    else if (!_spCategories.find(c => c.id === _spSelCatId)) _spSelCatId = _spCategories[0]?.id ?? null;
    _rebuildPosList();
  }
  function _rebuildPosList() {
    const list = document.getElementById('sp-pos-list');
    if (!list) return;
    const cat = _spCategories.find(c => c.id === _spSelCatId);
    if (!cat) { list.innerHTML = ''; return; }
    const enabledSet = new Set(_spMatchState?.enabledPositions ?? []);
    list.innerHTML = (cat.positions ?? []).map(p => {
      const checked = enabledSet.has(p.id) ? 'checked' : '';
      const locked  = !!cat.locked;
      return `<div class="sp-pos-row" data-id="${_esc(p.id)}">
        <input type="checkbox" class="sp-pos-cb" data-id="${_esc(p.id)}" ${checked} ${locked ? 'disabled' : ''} />
        <span class="sp-pos-name">${_esc(p.name)}</span>
        ${!locked ? `
          <button class="sp-pos-row-btn" data-action="edit" data-id="${_esc(p.id)}" title="Edit">&#x270F;</button>
          <button class="sp-pos-row-btn danger" data-action="del" data-id="${_esc(p.id)}" title="Delete">&#x2715;</button>
        ` : ''}
      </div>`;
    }).join('');
  }
  function _showPosInfo(posId) {
    const cat = _spCategories.find(c => c.id === _spSelCatId);
    const pos = (cat?.positions ?? []).find(p => p.id === posId);
    if (!pos) return;
    const nameEl = document.getElementById('sp-pos-name-display');
    const fenEl  = document.getElementById('sp-pos-fen-display');
    if (nameEl) nameEl.textContent = pos.name;
    if (fenEl)  fenEl.textContent  = pos.fen;
    if (_spPosBoard) _spPosBoard.update(pos.fen);
  }
  function _showEditor(posId) {
    _spEditPosId = posId ?? null;
    const cat = _spCategories.find(c => c.id === _spSelCatId);
    const pos = posId ? (cat?.positions ?? []).find(p => p.id === posId) : null;
    const nameInp = document.getElementById('sp-pos-name-inp');
    const fenInp  = document.getElementById('sp-pos-fen-inp');
    if (nameInp) nameInp.value = pos?.name ?? '';
    const targetFen = pos?.fen ?? 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
    if (fenInp) fenInp.value = targetFen;
    _spEditGrid    = _fenToGrid(targetFen);
    _spEditFenRest = targetFen.split(' ').slice(1).join(' ') || 'w KQkq - 0 1';
    if (_spPosBoard) _spPosBoard.update(targetFen);
    const editor = document.getElementById('sp-pos-editor');
    const info   = document.getElementById('sp-pos-info');
    const cursor = document.getElementById('sp-edit-cursor');
    if (editor) editor.style.display = '';
    if (info)   info.style.display   = 'none';
    if (cursor) cursor.style.display = '';
    _updateEditCursor();
  }
  function _hideEditor() {
    const editor = document.getElementById('sp-pos-editor');
    const info   = document.getElementById('sp-pos-info');
    const cursor = document.getElementById('sp-edit-cursor');
    if (editor) editor.style.display = 'none';
    if (info)   info.style.display   = '';
    if (cursor) cursor.style.display = 'none';
    _spEditPosId = null;
  }
  async function _reloadPositions() {
    try {
      const r = await fetch('/api/selfplay/positions');
      if (r.ok) _spCategories = await r.json();
      _rebuildCatSel();
    } catch (_) {}
  }

  function _isSelfplayService() {
    return _state?.settings?.service === 'selfplay';
  }

  /** Load all selfplay state from backend. */
  async function _loadSpState() {
    try {
      const [engRes, posRes, stateRes] = await Promise.all([
        fetch('/api/selfplay/engines'),
        fetch('/api/selfplay/positions'),
        fetch('/api/selfplay/state'),
      ]);
      if (engRes.ok)   _spEngines    = await engRes.json();
      if (posRes.ok)   _spCategories = await posRes.json();
      if (stateRes.ok) _spMatchState = await stateRes.json();
      _renderSp();
      _rebuildCatSel();
    } catch (_) {}
  }

  /** Render the selfplay shell (one-time HTML structure). */
  function _renderSelfplayShell() {
    _spShellReady = true;
    panel().innerHTML = `
      <!-- ── Top bar: service pills + action ─────────────────────── -->
      <div class="sq-top-bar">
        <div class="sq-svc-bar" id="sq-svc-bar"></div>
        <div class="sq-top-action">
          <span class="sq-lbl">Selfplay</span>
          <div class="sq-sw" id="sp-toggle" title="Toggle selfplay">
            <div class="sq-sw-knob"></div>
          </div>
          <span class="sq-sw-lbl" id="sp-toggle-lbl">OFF</span>
          <span class="sp-game-counter" id="sp-game-counter"></span>
        </div>
      </div>

      <!-- ── Config bar: mode/ponder left · tc right ─────────────── -->
      <div class="sp-cfg-bar">
        <div class="sp-cfg-left">
          <select class="sp-mode-sel" id="sp-mode-sel">
            <option value="single">Single</option>
            <option value="loop">Loop</option>
            <option value="rr">Round-Robin</option>
          </select>
          <div class="sp-ponder-grp">
            <div class="sq-sw sm" id="sp-ponder-sw" title="Enable pondering">
              <div class="sq-sw-knob"></div>
            </div>
            <span class="sq-mini-lbl">Ponder</span>
          </div>
        </div>
        <div class="sp-cfg-sep"></div>
        <div class="sp-cfg-right">
          <div class="sp-tc-presets" id="sp-tc-presets">
            <div class="sq-tc-row">
              <button class="sq-tc bullet sp-tc-card" data-initial="60000"  data-inc="0">1+0</button>
              <button class="sq-tc bullet sp-tc-card" data-initial="120000" data-inc="1000">2+1</button>
              <button class="sq-tc blitz  sp-tc-card" data-initial="180000" data-inc="2000">3+2</button>
              <button class="sq-tc blitz  sp-tc-card" data-initial="300000" data-inc="3000">5+3</button>
              <button class="sq-tc rapid  sp-tc-card" data-initial="600000" data-inc="5000">10+5</button>
              <button class="sq-tc rapid  sp-tc-card" data-initial="900000" data-inc="10000">15+10</button>
            </div>
          </div>
          <div class="sp-movetime-wrap" id="sp-movetime-wrap" style="display:none">
            <input type="number" id="sp-movetime-input" class="sp-movetime-input"
                   value="2000" min="100" max="60000" step="100" title="ms per move" />
            <span class="sp-movetime-unit">ms&#8202;/&#8202;move</span>
          </div>
          <div class="sp-tm-toggle">
            <button class="sp-tm-btn" data-tm="clock" title="Game clock">&#9200;</button>
            <button class="sp-tm-btn" data-tm="movetime" title="Fixed ms per move">&#9202;</button>
          </div>
        </div>
      </div>

      <!-- ── Config columns ───────────────────────────────────── -->
      <div class="sp-config-body">
        <div class="sp-col sp-engines-col">
          <div id="sp-engine-list"></div>
        </div>
        <div class="sp-col sp-pools-col">
          <!-- ── Two-pane position editor ──────────────────────── -->
          <div class="sp-pos-section">
            <div class="sp-pos-left">
              <div class="sp-pos-cat-bar">
                <select class="sp-cat-sel" id="sp-cat-sel"></select>
                <button class="sp-pos-icon-btn" id="sp-cat-add" title="New category">+</button>
                <button class="sp-pos-icon-btn" id="sp-cat-del" title="Delete category">&#x2715;</button>
              </div>
              <div class="sp-pos-list" id="sp-pos-list"></div>
              <div class="sp-pos-add-bar">
                <button class="sp-pos-add-btn" id="sp-pos-add">+ Position</button>
              </div>
            </div>
            <div class="sp-pos-right">
              <div class="sp-pos-board-wrap" id="sp-pos-board-wrap"></div>
              <div class="sp-edit-cursor" id="sp-edit-cursor" style="display:none">
                <span class="sp-edit-piece" id="sp-edit-piece">&#9823;</span>
                <span class="sp-edit-hint">scroll=cycle &middot; click=place &middot; &#x21E7;click=remove</span>
              </div>
              <div class="sp-pos-info" id="sp-pos-info">
                <div class="sp-pos-name-display" id="sp-pos-name-display"></div>
                <div class="sp-pos-fen-display sp-mono" id="sp-pos-fen-display"></div>
              </div>
              <div class="sp-pos-editor" id="sp-pos-editor" style="display:none">
                <input type="text" id="sp-pos-name-inp" class="sp-pos-inp" placeholder="Name…" autocomplete="off" />
                <input type="text" id="sp-pos-fen-inp"  class="sp-pos-inp sp-mono" placeholder="FEN…" autocomplete="off" spellcheck="false" />
                <div class="sp-pos-editor-row">
                  <button class="sp-pos-btn save" id="sp-pos-save">Save</button>
                  <button class="sp-pos-btn" id="sp-pos-cancel">Cancel</button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- ── Standings ────────────────────────────────────────── -->
      <div class="sp-standings-wrap" id="sp-standings-wrap" style="display:none">
        <div class="sp-col-hdr">Standings</div>
        <table class="sp-standings-table" id="sp-standings-table">
          <thead><tr>
            <th>Engine</th><th>W</th><th>D</th><th>L</th><th>Pts</th>
          </tr></thead>
          <tbody id="sp-standings-body"></tbody>
        </table>
      </div>
    `;
    _bindSpEvents();
  }

  /** Threshold in ms below which ponder is auto-disabled in Fixed (movetime) mode. */
  const SP_PONDER_THRESHOLD = 500;

  /** Engine accent colors — keyed by badgeClass. Used for CSS --eng-color inline var. */
  const SP_ENGINE_COLORS = {
    'sp-badge-sf':         'var(--accent)',
    'sp-badge-hce':        'var(--orange)',
    'sp-badge-nnue':       'var(--purple)',
    'sp-badge-berserk':    'var(--green)',
    'sp-badge-obsidian':   'var(--yellow)',
    'sp-badge-stormphrax': '#e878e8',
    'sp-badge-clover':     '#7ddb7d',
  };

  /** Apply/remove white/black glow classes based on the current game. */
  function _applyEngineGlows() {
    const game = panel()._spCurrentGame;
    document.querySelectorAll('.sp-eng-card').forEach(card => {
      const id = card.dataset.id;
      card.classList.toggle('sp-eng-playing-w', !!game && game.whiteId === id);
      card.classList.toggle('sp-eng-playing-b', !!game && game.blackId === id);
    });
  }

  /** Swap between TC-preset pills and movetime input; auto-adjust ponder. */
  function _syncTmMode(tmMode, movetimeMs) {
    const presetsEl = document.getElementById('sp-tc-presets');
    const mtWrapEl  = document.getElementById('sp-movetime-wrap');
    const isFixed   = tmMode === 'movetime';
    if (presetsEl) presetsEl.style.display = isFixed ? 'none' : '';
    if (mtWrapEl)  mtWrapEl.style.display  = isFixed ? '' : 'none';
    // Clock: always ponder on. Fixed: threshold-based.
    _autoAdjustPonder(isFixed ? movetimeMs : Infinity);
  }

  /** Auto-set ponder on/off. Pass Infinity to force on (clock mode). Always overridable. */
  function _autoAdjustPonder(movetimeMs) {
    const shouldPonder = movetimeMs >= SP_PONDER_THRESHOLD;
    const ponderEl = document.getElementById('sp-ponder-sw');
    if (!ponderEl) return;
    ponderEl.classList.toggle('on', shouldPonder);
    if (_spMatchState) _spMatchState.ponder = shouldPonder;
    _spPatch({ ponder: shouldPonder });
  }

  /** Bind interactive events on the selfplay shell. */
  function _bindSpEvents() {
    // Mode pills
    panel().addEventListener('click', async (e) => {
      // TC preset pills
      const tcPill = e.target.closest('.sp-tc-card');
      if (tcPill) {
        const initial   = parseInt(tcPill.dataset.initial, 10);
        const increment = parseInt(tcPill.dataset.inc,     10);
        if (_spMatchState) _spMatchState.tc = { initial, increment };
        document.querySelectorAll('.sp-tc-card').forEach(p =>
          p.classList.toggle('active', p === tcPill)
        );
        _spPatch({ tc: { initial, increment } });
        return;
      }

      // TM mode pills
      const tmPill = e.target.closest('.sp-tm-btn');
      if (tmPill) {
        const tmMode = tmPill.dataset.tm;
        if (_spMatchState) _spMatchState.tmMode = tmMode;
        document.querySelectorAll('.sp-tm-btn').forEach(p =>
          p.classList.toggle('active', p.dataset.tm === tmMode)
        );
        _syncTmMode(tmMode, _spMatchState?.movetimeMs ?? 2000);
        _spPatch({ tmMode });
        return;
      }

      // Selfplay toggle (start / stop)
      if (e.target.closest('#sp-toggle')) {
        const running = _spMatchState?.running ?? false;
        if (running) {
          try {
            const r = await fetch('/api/selfplay/stop', { method: 'POST' });
            const data = await r.json();
            if (data.ok) { _spMatchState = data.state; _renderSp(); }
          } catch (_) {}
        } else {
          const tc         = _spMatchState?.tc ?? { initial: 180000, increment: 2000 };
          const ponder     = !!document.getElementById('sp-ponder-sw')?.classList.contains('on');
          const tmMode     = _spMatchState?.tmMode    ?? 'clock';
          const movetimeMs = _spMatchState?.movetimeMs ?? 2000;
          const enabled   = _collectEnabledEngines();
          const enabledPositions = Array.from(
            document.querySelectorAll('.sp-pos-cb:not(:disabled):checked')
          ).map(cb => cb.dataset.id);
          try {
            const r = await fetch('/api/selfplay/start', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                enabledEngines: enabled,
                enabledPositions,
                tc,
                ponder,
                tmMode,
                movetimeMs,
              }),
            });
            const data = await r.json();
            if (data.ok) { _spMatchState = data.state; _renderSp(); }
            else { console.warn('[sp-start]', data.error); }
          } catch (err) { console.error(err); }
        }
        return;
      }
    });

    // Ponder toggle
    document.getElementById('sp-ponder-sw')?.addEventListener('click', () => {
      const el = document.getElementById('sp-ponder-sw');
      if (!el) return;
      el.classList.toggle('on');
      const ponder = el.classList.contains('on');
      if (_spMatchState) _spMatchState.ponder = ponder;
      _spPatch({ ponder });
    });

    // Movetime input — also auto-adjusts ponder
    document.getElementById('sp-movetime-input')?.addEventListener('change', () => {
      const inp = document.getElementById('sp-movetime-input');
      const v = parseInt(inp?.value ?? '', 10);
      if (!isNaN(v) && v >= 100) {
        if (_spMatchState) _spMatchState.movetimeMs = v;
        _spPatch({ movetimeMs: v });
        _autoAdjustPonder(v);
      }
    });

    // Mode select
    document.getElementById('sp-mode-sel')?.addEventListener('change', async (e) => {
      const mode = e.target.value;
      if (_spMatchState) _spMatchState.mode = mode;
      await _spPatch({ mode });
    });

    // ── Position editor ──────────────────────────────────────────

    // Helper: debouncer
    const _spDeb = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

    // Board.js instance for position editor
    const boardWrap = document.getElementById('sp-pos-board-wrap');
    if (boardWrap && typeof Board !== 'undefined') {
      _spPosBoard = Board.create(boardWrap, { noAnnotations: true });
      const startFen = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
      _spPosBoard.update(startFen);
      _spEditGrid = _fenToGrid(startFen);
      _attachBoardEditHandlers();
    }

    // Category select
    document.getElementById('sp-cat-sel')?.addEventListener('change', (e) => {
      _spSelCatId = e.target.value;
      _rebuildPosList();
    });

    // Add category
    document.getElementById('sp-cat-add')?.addEventListener('click', async () => {
      const name = prompt('New category name:');
      if (!name?.trim()) return;
      try {
        const r = await fetch('/api/selfplay/categories', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: name.trim() }),
        });
        const d = await r.json();
        if (d.ok) { _spSelCatId = d.category.id; await _reloadPositions(); }
        else alert(d.error ?? 'Failed to create category');
      } catch (_) {}
    });

    // Delete category
    document.getElementById('sp-cat-del')?.addEventListener('click', async () => {
      const cat = _spCategories.find(c => c.id === _spSelCatId);
      if (!cat) return;
      if (cat.locked) { alert('Cannot delete a locked category.'); return; }
      if (!confirm(`Delete category "${cat.name}"?`)) return;
      try {
        const r = await fetch(`/api/selfplay/categories/${encodeURIComponent(_spSelCatId)}`, { method: 'DELETE' });
        const d = await r.json();
        if (d.ok) { _spSelCatId = null; await _reloadPositions(); }
        else alert(d.error ?? 'Failed to delete category');
      } catch (_) {}
    });

    // Add position button → show editor
    document.getElementById('sp-pos-add')?.addEventListener('click', () => {
      _showEditor(null);
    });

    // Position list: row clicks (info/edit/del/checkbox)
    document.getElementById('sp-pos-list')?.addEventListener('click', async (e) => {
      const row = e.target.closest('.sp-pos-row');
      if (!row) return;
      const posId = row.dataset.id;

      // Edit button
      const editBtn = e.target.closest('.sp-pos-row-btn[data-action="edit"]');
      if (editBtn) { _showEditor(posId); return; }

      // Delete button
      const delBtn = e.target.closest('.sp-pos-row-btn[data-action="del"]');
      if (delBtn) {
        const cat = _spCategories.find(c => c.id === _spSelCatId);
        const pos = cat?.positions.find(p => p.id === posId);
        if (!confirm(`Delete position "${pos?.name ?? posId}"?`)) return;
        try {
          const r = await fetch(
            `/api/selfplay/categories/${encodeURIComponent(_spSelCatId)}/positions/${encodeURIComponent(posId)}`,
            { method: 'DELETE' }
          );
          const d = await r.json();
          if (d.ok) await _reloadPositions();
          else alert(d.error ?? 'Failed to delete position');
        } catch (_) {}
        return;
      }

      // Checkbox toggle handled by change event below
      if (e.target.classList.contains('sp-pos-cb')) return;

      // Row click → show on board
      _showPosInfo(posId);
    });

    // Checkbox change → update enabledPositions
    document.getElementById('sp-pos-list')?.addEventListener('change', (e) => {
      if (!e.target.classList.contains('sp-pos-cb')) return;
      const enabled = Array.from(
        document.querySelectorAll('.sp-pos-cb:not(:disabled):checked')
      ).map(cb => cb.dataset.id);
      if (_spMatchState) _spMatchState.enabledPositions = enabled;
      _spPatch({ enabledPositions: enabled });
    });

    // Save button
    document.getElementById('sp-pos-save')?.addEventListener('click', async () => {
      const name = document.getElementById('sp-pos-name-inp')?.value.trim();
      const fen  = document.getElementById('sp-pos-fen-inp')?.value.trim();
      if (!name || !fen) { alert('Name and FEN are required.'); return; }
      try {
        let r;
        if (_spEditPosId) {
          r = await fetch(
            `/api/selfplay/categories/${encodeURIComponent(_spSelCatId)}/positions/${encodeURIComponent(_spEditPosId)}`,
            { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, fen }) }
          );
        } else {
          r = await fetch(
            `/api/selfplay/categories/${encodeURIComponent(_spSelCatId)}/positions`,
            { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, fen }) }
          );
        }
        const d = await r.json();
        if (d.ok) { _hideEditor(); await _reloadPositions(); }
        else alert(d.error ?? 'Failed to save position');
      } catch (_) {}
    });

    // Cancel button
    document.getElementById('sp-pos-cancel')?.addEventListener('click', () => _hideEditor());

    // FEN input live board preview (debounced)
    document.getElementById('sp-pos-fen-inp')?.addEventListener('input', _spDeb(() => {
      const fen = document.getElementById('sp-pos-fen-inp')?.value.trim();
      if (fen && _spPosBoard) {
        _spPosBoard.update(fen);
        try {
          _spEditGrid    = _fenToGrid(fen);
          _spEditFenRest = fen.split(' ').slice(1).join(' ') || 'w KQkq - 0 1';
        } catch (_) {}
      }
    }, 400));

    // Initial category/position list population — will be empty until _loadSpState resolves
    _rebuildCatSel();
  }

  async function _spPatch(data) {
    try {
      await fetch('/api/selfplay/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
      });
    } catch (_) {}
  }

  /** Full re-render of selfplay UI from _spEngines / _spCategories / _spMatchState. */
  function _renderSp() {
    if (!_spShellReady) return;

    const ms = _spMatchState;

    // Sync service bar
    _renderServiceBar(_state?.availableServices, _state?.settings?.service);

    // Mode select
    const modeSelEl = document.getElementById('sp-mode-sel');
    if (modeSelEl && ms?.mode) modeSelEl.value = ms.mode;

    // TC pills — sync active pill from current tc
    if (ms?.tc) {
      document.querySelectorAll('.sp-tc-card').forEach(p => {
        const match = parseInt(p.dataset.initial, 10) === ms.tc.initial &&
                      parseInt(p.dataset.inc,     10) === ms.tc.increment;
        p.classList.toggle('active', match);
      });
    }

    // TM mode pills + swap presets/movetime visibility
    if (ms?.tmMode != null) {
      document.querySelectorAll('.sp-tm-btn').forEach(p =>
        p.classList.toggle('active', p.dataset.tm === ms.tmMode)
      );
      const presetsEl = document.getElementById('sp-tc-presets');
      const mtWrapEl  = document.getElementById('sp-movetime-wrap');
      const mtInp     = document.getElementById('sp-movetime-input');
      const isFixed   = ms.tmMode === 'movetime';
      if (presetsEl) presetsEl.style.display = isFixed ? 'none' : '';
      if (mtWrapEl)  mtWrapEl.style.display  = isFixed ? '' : 'none';
      if (mtInp && document.activeElement !== mtInp && ms.movetimeMs != null) mtInp.value = ms.movetimeMs;
    }

    // Ponder switch — derive from mode: clock always on, fixed by threshold
    const ponderEl = document.getElementById('sp-ponder-sw');
    if (ponderEl && ms != null) {
      const effectiveMt = ms.tmMode === 'movetime' ? (ms.movetimeMs ?? 2000) : Infinity;
      ponderEl.classList.toggle('on', effectiveMt >= SP_PONDER_THRESHOLD);
    }

    // Selfplay toggle
    const toggleEl  = document.getElementById('sp-toggle');
    const toggleLbl = document.getElementById('sp-toggle-lbl');
    if (toggleEl) {
      const running = ms?.running ?? false;
      toggleEl.classList.toggle('on', running);
      if (toggleLbl) toggleLbl.textContent = running ? 'ON' : 'OFF';
    }

    // Game counter
    const counterEl = document.getElementById('sp-game-counter');
    if (counterEl && ms != null) {
      const ws = ms.standings;
      const ids = Object.keys(ws ?? {});
      if (ids.length >= 2) {
        const [aId, bId] = ids;
        const a = ws[aId], b = ws[bId];
        counterEl.textContent = `Game ${ms.gameCount}  ${ms.running ? '● running' : '■ stopped'}`
          + `  ${a.w + a.d / 2} – ${b.w + b.d / 2}`;
      } else {
        counterEl.textContent = ms.gameCount > 0
          ? `${ms.gameCount} game${ms.gameCount !== 1 ? 's' : ''}`
          : '';
      }
    }

    // Engine list
    const engListEl = document.getElementById('sp-engine-list');
    if (engListEl && _spEngines.length) {
      const enabledSet = new Set(ms?.enabledEngines ?? []);
      const locked     = ms?.running ?? false;
      engListEl.classList.toggle('sp-eng-locked', locked);
      // Split into static engines and archive builds
      const staticEngines  = _spEngines.filter(e => e.archiveBuild == null);
      const archiveEngines = _spEngines.filter(e => e.archiveBuild != null);

      // Static engine cards
      engListEl.innerHTML = staticEngines.map(e => {
        const color   = SP_ENGINE_COLORS[e.badgeClass] ?? 'var(--muted)';
        const sel     = enabledSet.has(e.id) ? ' sp-eng-selected' : '';
        const unavail = e.available ? '' : ' sp-eng-unavail-card';
        return `<div class="sp-eng-card${sel}${unavail}" data-id="${_esc(e.id)}" style="--eng-color:${color}">
          <div class="sp-eng-body">
            <span class="sp-eng-name">${_esc(e.name)}</span>
            ${e.available ? '' : '<span class="sp-unavail-tag">!</span>'}
          </div>
          <div class="sp-eng-glow-bar"></div>
        </div>`;
      }).join('');

      // Archive builds — single collapsible multi-picker
      if (archiveEngines.length > 0) {
        const selCount    = archiveEngines.filter(e => enabledSet.has(e.id)).length;
        const caretCls    = _spArchOpen ? ' sp-arch-open' : '';
        const pickerDisp  = _spArchOpen ? '' : 'none';
        engListEl.insertAdjacentHTML('beforeend', `
          <div class="sp-arch-section" id="sp-arch-section">
            <div class="sp-arch-header${caretCls}" id="sp-arch-hdr">
              <span class="sp-arch-title"><span class="sp-badge sp-badge-hce">HCE</span> Archives</span>
              <span class="sp-arch-count" id="sp-arch-count">${selCount ? selCount + ' selected' : 'none'}</span>
              <span class="sp-arch-caret">&#9658;</span>
            </div>
            <div class="sp-arch-picker" id="sp-arch-picker" style="display:${pickerDisp}">
              <div class="sp-arch-quick-bar">
                <button class="sp-arch-qbtn" id="sp-arch-none-btn">None</button>
                <button class="sp-arch-qbtn" id="sp-arch-all-btn">All</button>
                <button class="sp-arch-qbtn" id="sp-arch-last5-btn">Last 5</button>
              </div>
              <div class="sp-arch-chips" id="sp-arch-chips">
                ${archiveEngines.map(e => {
                  const chipSel = enabledSet.has(e.id) ? ' sp-arch-sel' : '';
                  const chipUna = e.available ? '' : ' sp-arch-unavail';
                  return `<div class="sp-arch-chip${chipSel}${chipUna}" data-id="${_esc(e.id)}" title="${_esc(e.name)}">#${e.archiveBuild}</div>`;
                }).join('')}
              </div>
            </div>
          </div>
        `);

        const syncArchCount = () => {
          const n = engListEl.querySelectorAll('.sp-arch-chip.sp-arch-sel').length;
          const el = document.getElementById('sp-arch-count');
          if (el) el.textContent = n ? `${n} selected` : 'none';
        };

        document.getElementById('sp-arch-hdr')?.addEventListener('click', () => {
          const picker = document.getElementById('sp-arch-picker');
          const hdr    = document.getElementById('sp-arch-hdr');
          if (!picker) return;
          _spArchOpen = picker.style.display === 'none';
          picker.style.display = _spArchOpen ? '' : 'none';
          hdr?.classList.toggle('sp-arch-open', _spArchOpen);
        });

        if (!locked) {
          engListEl.querySelectorAll('.sp-arch-chip:not(.sp-arch-unavail)').forEach(chip => {
            chip.addEventListener('click', () => {
              chip.classList.toggle('sp-arch-sel');
              syncArchCount();
              const enabled = _collectEnabledEngines();
              if (_spMatchState) _spMatchState.enabledEngines = enabled;
              _spPatch({ enabledEngines: enabled });
            });
          });

          document.getElementById('sp-arch-none-btn')?.addEventListener('click', () => {
            engListEl.querySelectorAll('.sp-arch-chip').forEach(c => c.classList.remove('sp-arch-sel'));
            syncArchCount();
            const enabled = _collectEnabledEngines();
            if (_spMatchState) _spMatchState.enabledEngines = enabled;
            _spPatch({ enabledEngines: enabled });
          });
          document.getElementById('sp-arch-all-btn')?.addEventListener('click', () => {
            engListEl.querySelectorAll('.sp-arch-chip:not(.sp-arch-unavail)').forEach(c => c.classList.add('sp-arch-sel'));
            syncArchCount();
            const enabled = _collectEnabledEngines();
            if (_spMatchState) _spMatchState.enabledEngines = enabled;
            _spPatch({ enabledEngines: enabled });
          });
          document.getElementById('sp-arch-last5-btn')?.addEventListener('click', () => {
            engListEl.querySelectorAll('.sp-arch-chip').forEach(c => c.classList.remove('sp-arch-sel'));
            const chips = Array.from(engListEl.querySelectorAll('.sp-arch-chip:not(.sp-arch-unavail)'));
            chips.slice(-5).forEach(c => c.classList.add('sp-arch-sel'));
            syncArchCount();
            const enabled = _collectEnabledEngines();
            if (_spMatchState) _spMatchState.enabledEngines = enabled;
            _spPatch({ enabledEngines: enabled });
          });
        }
      }

      _applyEngineGlows();
      engListEl.querySelectorAll('.sp-eng-card:not(.sp-eng-unavail-card)').forEach(card => {
        card.addEventListener('click', () => {
          card.classList.toggle('sp-eng-selected');
          const enabled = _collectEnabledEngines();
          if (_spMatchState) _spMatchState.enabledEngines = enabled;
          _spPatch({ enabledEngines: enabled });
        });
      });
    }

    // Position list checkboxes — re-sync enabledPositions from state
    if (_spMatchState) {
      const enabledSet = new Set(_spMatchState.enabledPositions ?? []);
      document.querySelectorAll('.sp-pos-cb').forEach(cb => {
        if (!cb.disabled) cb.checked = enabledSet.has(cb.dataset.id);
      });
    }

    // Standings
    const standingsWrap = document.getElementById('sp-standings-wrap');
    const standingsBody = document.getElementById('sp-standings-body');
    if (standingsWrap && standingsBody) {
      const ws = ms?.standings ?? {};
      const rows = Object.entries(ws)
        .sort((a, b) => b[1].pts - a[1].pts)
        .map(([id, s]) => {
          const reg = _spEngines.find(e => e.id === id);
          const name = reg ? `<span class="sp-badge ${_esc(reg.badgeClass)}">${_esc(reg.badge)}</span> ${_esc(reg.name)}` : _esc(id);
          return `<tr>
            <td>${name}</td>
            <td>${s.w}</td><td>${s.d}</td><td>${s.l}</td>
            <td><b>${s.pts}</b></td>
          </tr>`;
        });
      standingsBody.innerHTML = rows.join('');
      standingsWrap.style.display = rows.length > 0 ? '' : 'none';
    }
  }


  /** Handle sp_game_start event. */
  function _onSpGameStart(data) {
    panel()._spCurrentGame = { whiteId: data.whiteId, blackId: data.blackId };
    _applyEngineGlows();
  }

  /** Handle sp_move event — no-op (board now in game tab). */
  function _onSpMove(_data) {}

  /** Handle sp_info event — no-op (eval now in game tab player rows). */
  function _onSpInfo(_data) {}

  /** Handle sp_game_end event. */
  function _onSpGameEnd(data) {
    if (data.standings) {
      if (_spMatchState) _spMatchState.standings = data.standings;
      const body = document.getElementById('sp-standings-body');
      const wrap = document.getElementById('sp-standings-wrap');
      if (body) {
        const rows = Object.entries(data.standings)
          .sort((a, b) => b[1].pts - a[1].pts)
          .map(([id, s]) => {
            const reg = _spEngines.find(e => e.id === id);
            const name = reg
              ? `<span class="sp-badge ${_esc(reg.badgeClass)}">${_esc(reg.badge)}</span> ${_esc(reg.name)}`
              : _esc(id);
            return `<tr><td>${name}</td><td>${s.w}</td><td>${s.d}</td><td>${s.l}</td><td><b>${s.pts}</b></td></tr>`;
          });
        body.innerHTML = rows.join('');
        if (wrap) wrap.style.display = '';
      }
    }
    panel()._spCurrentGame = null;
    _applyEngineGlows();
  }

  App.registerTab('seek', { show, onEvent });
  return { show, onEvent };
})();
