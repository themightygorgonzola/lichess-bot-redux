/**
 * tab-game.js — The "TV" view: board, eval bar, clocks, PV, move list.
 */
'use strict';

const TabGame = (() => {
  let _gameId    = null;
  let _rendered  = false;
  let _boardInst = null;   // Board.create() instance for the active game
  let _prevFen   = null;   // FEN the board is currently showing — used for capture detection
  let _activeCommsTab = 'chat'; // currently active comms sub-tab
  let _ourTurnStartMs = 0;  // performance.now() when our turn started (badge halo elapsed)
  let _oppTurnStartMs = 0;  // performance.now() when opp turn started

  // Selfplay engine-side map: { [engineId]: 'top' | 'bot' } — set on sp_game_start
  let _spGameMap = null;

  /** Format a selfplay sp_info event as a compact eval string for player rows. */
  function _fmtSpEval(info) {
    if (!info) return '';
    const score = info.score_mate != null
      ? (info.score_mate > 0 ? '+M' : '-M') + Math.abs(info.score_mate)
      : info.score_cp  != null
      ? (info.score_cp >= 0 ? '+' : '') + (info.score_cp / 100).toFixed(2)
      : '';
    const depth = info.depth != null ? ` d${info.depth}` : '';
    const nps   = info.nps   != null ? ` ${(info.nps / 1e6).toFixed(1)}M` : '';
    return score + depth + nps;
  }

  // Move queue — absorbs rapid move bursts so the board never falls behind.
  // Each entry: { g, uci, type } where type is 'move' | 'opponent_move'.
  const _moveQueue = [];
  let _moveRafPending = false;

  // search_info dirty flag — coalesces rapid search_info events into one rAF.
  let _searchDirty   = false;
  let _searchDirtyG  = null;
  let _searchRafId   = null;

  /* ── Arrow preferences (localStorage, no server round-trip) ────── */
  const ARROW_PREFS_KEY = 'hb-arrow-prefs';
  const ARROW_DEFAULTS  = { depth: 1, colorOurs: '#22c55e', colorOpp: '#3b82f6', opacity: 0.72 };

  function _getArrowPrefs() {
    try { return { ...ARROW_DEFAULTS, ...JSON.parse(localStorage.getItem(ARROW_PREFS_KEY) || '{}') }; }
    catch { return { ...ARROW_DEFAULTS }; }
  }

  /**
   * Build the engine PV arrow list for the current search state.
   * Returns [] when depth=0, during ponderhit ponder (wrong position),
   * or when no PV data is available.
   *
   * Ply colour assignment:
   *   - real search (no ponder): ply 0 = ours, ply 1 = opp, ply 2 = ours ...
   *   - parent-position ponder ('opponent'): ply 0 = opp, ply 1 = ours, ply 2 = opp ...
   *   - ponderhit ponder ('ours'): skip entirely (pv is in a future position)
   */
  function _buildArrows(g) {
    const prefs = _getArrowPrefs();
    if (!prefs.depth) return [];
    const live = g.searchLive;
    if (!live) return [];

    // Opacity falloff per ply index
    const opacityScale = [1.0, 0.60, 0.38];
    // Arrow width by ply index
    const widths = [8, 6, 5];

    // ── Ponderhit ponder ('ours'): engine is past the predicted reply. ─────
    // Show: [ponderMove = predicted opp reply] + [pv[0] = our reply] + ...
    // parity: slot 0 = opp, slot 1 = ours, slot 2 = opp, ...
    if (live.ponder === 'ours') {
      const pm = g._ponderMoveUci;
      if (!pm) return [];
      let pvArr = [];
      if (Array.isArray(live.pv))           pvArr = live.pv;
      else if (typeof live.pv === 'string') pvArr = live.pv.split(' ').filter(Boolean);
      else if (live.pv0)                    pvArr = [live.pv0];
      const arrows = [{
        uci:     pm,
        color:   prefs.colorOpp,
        opacity: prefs.opacity * (opacityScale[0] ?? 1.0),
        width:   widths[0] ?? 8,
      }];
      const count = Math.min(prefs.depth - 1, pvArr.length);
      for (let i = 0; i < count; i++) {
        // slot i+1: odd slots=ours, even slots=opp (slot 0 was opp)
        const isOurs = ((i + 1) % 2 === 1);
        arrows.push({
          uci:     pvArr[i],
          color:   isOurs ? prefs.colorOurs : prefs.colorOpp,
          opacity: prefs.opacity * (opacityScale[i + 1] ?? 0.30),
          width:   widths[i + 1] ?? 4,
        });
      }
      return arrows;
    }

    // ── Parent-position ponder ('opponent'): position = after our move, opp to move.
    // ── Real search (no ponder): position = current game position, we are to move.
    const isOppPonder = live.ponder === 'opponent';
    let pvArr = [];
    if (Array.isArray(live.pv))           pvArr = live.pv;
    else if (typeof live.pv === 'string') pvArr = live.pv.split(' ').filter(Boolean);
    else if (live.pv0)                    pvArr = [live.pv0];
    if (!pvArr.length) return [];

    const arrows = [];
    const count  = Math.min(prefs.depth, pvArr.length);
    for (let i = 0; i < count; i++) {
      const isOurs = isOppPonder ? (i % 2 === 1) : (i % 2 === 0);
      arrows.push({
        uci:     pvArr[i],
        color:   isOurs ? prefs.colorOurs : prefs.colorOpp,
        opacity: prefs.opacity * (opacityScale[i] ?? 0.30),
        width:   widths[i] ?? 4,
      });
    }
    return arrows;
  }

  const panel = () => document.getElementById('panel-game');

  /* ── lifecycle ──────────────────────────────────────────────────────── */

  function show() {
    const g = App.currentGame();
    if (g && g.id !== _gameId) { _gameId = g.id; _rendered = false; }
    if (!g) {
      _gameId = null;
      // Only re-render the idle shell if not already showing it
      if (!_rendered) _showIdle();
      return;
    }
    _fullRender(g);
  }

  function onEvent(type, data) {
    const g = App.currentGame();

    // Always feed logs into the console column, even when no game is active
    if (type === 'log') {
      _appendLogInGame(data);
    }

    // Forward seek-relevant events to the embedded seek panel
    if (type === 'challenge_sent' || type === 'challenge_declined' ||
        type === 'challenge_canceled' || type === 'game_start') {
      TabSeek.onEvent(type, data);
    }

    // Selfplay engine-side eval routing
    if (type === 'sp_game_start') {
      // White = bottom row (our bot), black = top row (opponent)
      _spGameMap = { [data.whiteId]: 'bot', [data.blackId]: 'top' };
      return;
    }
    if (type === 'sp_info') {
      const side = _spGameMap?.[data.engineId];
      if (side) {
        const el = document.getElementById(`sp-eval-${side}`);
        if (el) el.textContent = _fmtSpEval(data);
      }
      return;
    }

    if (!g) {
      // New game arrived while idle — swap to full render
      if (type === 'game_start' && App.activeTab() === 'game') {
        const newG = App.currentGame();
        if (newG) { _gameId = newG.id; _rendered = false; _fullRender(newG); }
      }
      return;
    }

    // Track new games
    if (type === 'game_start') {
      _gameId = g.id; _rendered = false;
      if (App.activeTab() === 'game') _fullRender(g);
      return;
    }

    if (g.id !== _gameId) { _gameId = g.id; _rendered = false; }
    if (App.activeTab() !== 'game') return;
    if (!_rendered) { _fullRender(g); return; }

    switch (type) {
      case 'snapshot':
      case 'game_meta':
        _fullRender(g); break;
      case 'fen_update':
        // fen_update always fires BEFORE move/opponent_move (see store.js / game.js).
        // The move handlers below drive the board update with animation once the
        // UCI is known.  Here we only need to refresh on fen changes that have no
        // associated move event (e.g. initial gameFull when opponent moves first).
        // Those cases are handled by _fullRender already, so this is a safe no-op.
        console.log('[board] fen_update (no-op) →', data.fen?.slice(0,30));
        break;
      case 'opponent_move':
        _ourTurnStartMs = performance.now();
        console.log('[board] opponent_move →', data.move);
        _enqueueMoveUpdate(g, data.move, 'opponent_move');
        break;
      case 'move':
        _oppTurnStartMs = performance.now();
        console.log('[board] move →', data.moveStat?.move);
        _enqueueMoveUpdate(g, data.moveStat?.move, 'move');
        break;
      case 'search_info':
        _scheduleSearchRender(g);
        break;
      case 'search_start':
        _updateEngineStrip(g); _updatePvCol(g); break;
      case 'search_end':
      case 'ponder_start':
      case 'ponder_end':
        _updateEngineStrip(g); _updatePvCol(g);
        _setBestMoveArrow(g);
        break;
      case 'game_end':
        _fullRender(g); break;
      case 'clock_update':
        /* handled by onClockTick */ break;
      case 'chat_line':
        _appendChatInGame(data); break;
    }

    // Forward engine events into the Eng comms sub-pane
    if (typeof TabEnginePane !== 'undefined') {
      if (type === 'search_info' || type === 'search_start' || type === 'search_end' ||
          type === 'ponder_start' || type === 'ponder_end' ||
          type === 'move' || type === 'opponent_move' ||
          type === 'snapshot' || type === 'game_end') {
        TabEnginePane.onEvent(type, data);
      }
    }
  }

  function onClockTick(g) {
    _tickClock('opp-clock', g, true);
    _tickClock('our-clock', g, false);
  }

  /* ── idle (no game) state ─────────────────────────────────────────── */

  function _showIdle() {
    panel().innerHTML = `
      <div class="game-comms-col">
        <div class="comms-tabs">
          <button class="comms-tab active" data-comms="chat">Chat</button>
          <button class="comms-tab" data-comms="seek">Seek</button>
          <button class="comms-tab" data-comms="console">Log</button>
          <button class="comms-tab" data-comms="ctrl">Ctrl</button>
          <button class="comms-tab" data-comms="eng">Eng</button>
        </div>
        <div class="comms-pane active" id="comms-pane-chat">
          <div class="chat-section">
            <div class="chat-section-hdr">Players</div>
            <div class="comms-body" id="game-chat-player"></div>
          </div>
          <div class="chat-section">
            <div class="chat-section-hdr">Spectators</div>
            <div class="comms-body" id="game-chat-spectator"></div>
          </div>
        </div>
        <div class="comms-pane" id="comms-pane-seek"></div>
        <div class="comms-pane" id="comms-pane-console">
          <div class="comms-body" id="game-log-feed"></div>
        </div>
        <div class="comms-pane" id="comms-pane-ctrl"></div>
        <div class="comms-pane" id="comms-pane-eng"></div>
      </div>
      <div class="game-body">
        <div class="game-main-row">
          <div class="game-left">
            <div class="player-row top">
              <span class="badge black">W</span>
              <span class="player-name" style="color:var(--muted)">Waiting…</span>
              <span class="player-clock" style="color:var(--muted)">-:--</span>
            </div>
            <div class="board-area">
              <div class="board-cluster">
                <div class="board-wrap board-idle"></div>
                <div class="eval-bar-col" id="eval-bar">
                  <div class="eval-bar-label top" id="eval-label-top"></div>
                  <div class="eval-bar-inner" id="eval-bar-fill" style="height:50%"></div>
                  <div class="eval-bar-label bot" id="eval-label-bot"></div>
                </div>
              </div>
              <div class="idle-overlay">
                <div class="idle-overlay-inner">♟ Waiting for a game…</div>
              </div>
            </div>
            <div class="player-row bottom">
              <span class="badge white">B</span>
              <span class="player-name" style="color:var(--muted)">Waiting…</span>
              <span class="player-clock" style="color:var(--muted)">-:--</span>
            </div>
          </div>
          <div class="game-right">
            <div class="engine-strip" id="engine-strip">–</div>
            <div class="game-right-body">
              <div class="pv-col" id="pv-col">
                <div class="pv-col-header">PV</div>
                <div class="pv-col-body" id="pv-list"></div>
              </div>
              <div class="move-list-wrap"><table class="move-list" id="move-list"><tbody></tbody></table></div>
            </div>
          </div>
        </div>
        <div class="game-sparkline" id="game-sparkline"></div>
      </div>
    `;
    _rendered = true;
    _prevFen = null;

    // Create board instance for the idle view (starting position, blurred)
    if (_boardInst) { _boardInst.destroy(); _boardInst = null; }
    const boardContainer = document.querySelector('#panel-game .board-wrap');
    if (boardContainer) {
      _boardInst = Board.create(boardContainer);
      _boardInst.update('rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1', { flip: false });
      _boardInst.svg.addEventListener('mousedown', e => {
        if (e.button === 0 && e.ctrlKey) { e.preventDefault(); _boardInst.clearUserAnnotations(); }
      });
    }

    // Populate console with existing log history
    for (const entry of App.logs) _appendLogInGame(entry);
    _bindCommsTabsClicks();
    if (typeof TabEnginePane !== 'undefined') TabEnginePane.show();
  }

  /* ── full render ────────────────────────────────────────────────────── */

  function _fullRender(g) {
    _ourTurnStartMs = 0; _oppTurnStartMs = 0;  // reset turn timers
    const isBlack = g.color === 'black';
    const oppName  = App.esc(g.opponentName || '?');
    const ourName  = g.ourName ?? g.opponentName ?? 'Bot';
    const oppRating = g.oppRating ? `(${g.oppRating})` : '';
    const ourRating = g.ourRating ? `(${g.ourRating})` : '';
    const tc = g.timeControl ? `${g.timeControl} ${(g.speed || '')}` : (g.speed || '');
    const ratedStr = g.rated ? 'rated' : 'casual';

    panel().innerHTML = `
      <div class="game-comms-col">
        <div class="comms-tabs">
          <button class="comms-tab active" data-comms="chat">Chat</button>
          <button class="comms-tab" data-comms="seek">Seek</button>
          <button class="comms-tab" data-comms="console">Log</button>
          <button class="comms-tab" data-comms="ctrl">Ctrl</button>
          <button class="comms-tab" data-comms="eng">Eng</button>
        </div>
        <div class="comms-pane active" id="comms-pane-chat">
          <div class="chat-section">
            <div class="chat-section-hdr">Players</div>
            <div class="comms-body" id="game-chat-player"></div>
          </div>
          <div class="chat-section">
            <div class="chat-section-hdr">Spectators</div>
            <div class="comms-body" id="game-chat-spectator"></div>
          </div>
          ${g.status !== 'finished' ? `
          <div class="game-action-bar" id="game-action-bar">
            <button class="game-action-btn" id="btn-abort"
              title="Abort game (only valid before any moves are played)">Abort</button>
            <button class="game-action-btn danger" id="btn-resign"
              title="Resign the current game">Resign</button>
          </div>` : ''}
        </div>
        <div class="comms-pane" id="comms-pane-seek"></div>
        <div class="comms-pane" id="comms-pane-console">
          <div class="comms-body" id="game-log-feed"></div>
        </div>
        <div class="comms-pane" id="comms-pane-ctrl"></div>
        <div class="comms-pane" id="comms-pane-eng"></div>
      </div>
      <div class="game-body">
        <div class="game-main-row">
          <div class="game-left">
            <div class="player-row top">
              <span class="badge ${isBlack ? 'white' : 'black'}" id="opp-badge"></span>
              <span class="player-name">${oppName}</span>
              <span class="player-rating">${oppRating}</span>
              ${g.service === 'selfplay' ? '<span class="sp-eval-line" id="sp-eval-top"></span>' : ''}
              <span class="muted" style="font-size:0.65rem;margin-left:auto;">${tc} ${ratedStr}</span>
              <span class="player-clock" id="opp-clock">–:––</span>
            </div>
            <div class="board-area">
              <div class="board-cluster">
                <div class="board-wrap"></div>
                <div class="eval-bar-col" id="eval-bar">
                  <div class="eval-bar-label top" id="eval-label-top"></div>
                  <div class="eval-bar-inner" id="eval-bar-fill" style="height:50%"></div>
                  <div class="eval-bar-label bot" id="eval-label-bot"></div>
                </div>
              </div>
              ${g.status === 'finished' ? _resultOverlay(g) : ''}
            </div>
            <div class="player-row bottom">
              <span class="badge ${g.color}" id="our-badge"></span>
              <span class="player-name">${ourName}</span>
              <span class="player-rating">${ourRating}</span>
              ${g.service === 'selfplay' ? '<span class="sp-eval-line" id="sp-eval-bot"></span>' : ''}
              <span class="player-clock" id="our-clock">–:––</span>
            </div>
          </div>
          <div class="game-right">
            <div class="engine-strip" id="engine-strip">–</div>
            <div class="game-right-body">
              <div class="pv-col" id="pv-col">
                <div class="pv-col-header">PV</div>
                <div class="pv-col-body" id="pv-list"></div>
              </div>
              <div class="move-list-wrap"><table class="move-list" id="move-list"><tbody></tbody></table></div>
            </div>
          </div>
        </div>
        <div class="game-sparkline" id="game-sparkline"></div>
      </div>
    `;

    _rendered = true;
    _prevFen = null;

    // Create a fresh board instance in the board-wrap container
    if (_boardInst) { _boardInst.destroy(); _boardInst = null; }
    const boardContainer = document.querySelector('#panel-game .board-wrap');
    if (boardContainer) {
      _boardInst = Board.create(boardContainer);
      _boardInst.svg.addEventListener('mousedown', e => {
        if (e.button === 0 && e.ctrlKey) { e.preventDefault(); _boardInst.clearUserAnnotations(); }
      });
    }

    _updateBoard(g);
    _updateEvalBar(g);
    _updateEngineStrip(g);
    _updatePvCol(g);
    _updateMoves(g);
    _updateSparkline(g);
    if (g.clock) onClockTick(g);

    // Populate comms feeds
    if (g.chat) {
      for (const c of g.chat) _appendChatInGame(c);
    }
    for (const entry of App.logs) _appendLogInGame(entry);
    _bindCommsTabsClicks();
    if (typeof TabEnginePane !== 'undefined') TabEnginePane.show();
  }

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

  function _resultOverlay(g) {
    let text = g.result || '?';
    let cls = '';
    if (App.isWin(g))       { text = 'Victory · ' + g.result; cls = 'win'; }
    else if (App.isLoss(g)) { text = 'Defeat · '  + g.result; cls = 'loss'; }
    else if (g.result === '1/2-1/2') { text = 'Draw · ½-½'; cls = 'draw'; }
    const reason = REASON_LABELS[g.resultReason] ?? g.resultReason ?? '';
    return `<div class="result-banner"><div class="result-text badge ${cls}" style="font-size:1.5rem;padding:0.5rem 1.5rem;">${text}</div><div class="result-reason">${App.esc(reason)}</div></div>`;
  }

  /* ── board ──────────────────────────────────────────────────────────── */

  /**
   * Push a move onto the queue and schedule a rAF drain if not already pending.
   * During fast sequences (self-play, engine-vs-engine) many moves can arrive
   * before the browser paints. We snap intermediate positions without animation
   * and only animate the final queued move, keeping the board in sync.
   */
  function _enqueueMoveUpdate(g, uci, type) {
    // Snapshot g.fen NOW — by the time the rAF fires, g.fen will be the final
    // position after ALL queued moves. Each entry needs its own target FEN so
    // intermediate snaps render the correct board state.
    _moveQueue.push({ g, uci, type, targetFen: g.fen });
    if (!_moveRafPending) {
      _moveRafPending = true;
      requestAnimationFrame(_drainMoveQueue);
    }
  }

  function _drainMoveQueue() {
    _moveRafPending = false;
    if (_moveQueue.length === 0) return;

    // Queue of 1-2: instant reply / premove scenario — snap first but still play sound.
    // Queue of 3+: alt-tab buildup — snap intermediate moves silently.
    const snapSound = _moveQueue.length <= 2;

    // Snap all queued moves except the last one.
    for (let i = 0; i < _moveQueue.length - 1; i++) {
      const { g, uci, type, targetFen } = _moveQueue[i];
      _doMoveUpdate(g, uci, /* animate */ false, targetFen, /* playSound */ snapSound);
      if (type === 'move') {
        _updateMoves(g); _updateEvalBar(g); _updatePvCol(g); _updateSparkline(g);
      } else {
        _updateMoves(g);
      }
    }

    // Animate the final (most recent) move — always play sound.
    const { g, uci, type, targetFen } = _moveQueue[_moveQueue.length - 1];
    _moveQueue.length = 0;
    _doMoveUpdate(g, uci, /* animate */ true, targetFen);
    if (type === 'move') {
      _updateMoves(g); _updateEvalBar(g); _updatePvCol(g); _updateSparkline(g);
    } else {
      _updateMoves(g);
    }
  }

  /**
   * Mark search_info state dirty and schedule a single rAF to flush it.
   * Rapid search_info bursts collapse into one render call per frame.
   */
  function _scheduleSearchRender(g) {
    _searchDirty  = true;
    _searchDirtyG = g;
    if (_searchRafId === null) {
      _searchRafId = requestAnimationFrame(() => {
        _searchRafId = null;
        if (_searchDirty && _searchDirtyG) {
          _searchDirty  = false;
          const tg = _searchDirtyG;
          _searchDirtyG = null;
          _updateEngineStrip(tg); _updatePvCol(tg); _updateEvalBar(tg);
          _setBestMoveArrow(tg);
        }
      });
    }
  }

  /**
   * Handle a real move (ours or opponent's).
   * By the time move/opponent_move fires, g.fen is already the post-move FEN
   * (fen_update fired first).  _boardInst.grid is still the pre-move state
   * because we let fen_update be a no-op, so the diff will produce the perfect
   * sliding animation.
   * _prevFen is the FEN we last rendered — also pre-move — used for capture detection.
   *
   * @param {boolean} [animate=true]   — pass false to snap without animation (queue drain)
   * @param {string|null} [targetFen]  — FEN to render; defaults to g.fen when null
   */
  function _doMoveUpdate(g, uci, animate = true, targetFen = null, playSound = null) {
    if (!uci) { console.warn('[board] _doMoveUpdate: no UCI, plain update'); _updateBoard(g); return; }
    // playSound defaults to animate when not explicitly specified
    const doSound = playSound ?? animate;
    console.log('[board] _doMoveUpdate uci=%s prevFen=%s targetFen=%s animate=%s sound=%s',
      uci, _prevFen?.slice(0,20) ?? 'null', (targetFen || g.fen)?.slice(0,20) ?? 'null', animate, doSound);

    // Capture / en-passant detection against the board's last-rendered FEN
    let isCapture = false, isEp = false, isWhiteMove = null;
    if (_prevFen) {
      try {
        const { board } = Board.parseFen(_prevFen);
        const toFile = uci.charCodeAt(2) - 97;
        const toRank = 8 - parseInt(uci[3]);
        isCapture = !!(board[toRank]?.[toFile]);
        if (!isCapture) {
          const fromFile = uci.charCodeAt(0) - 97;
          const piece = board[8 - parseInt(uci[1])]?.[fromFile] || '';
          isEp = piece.toLowerCase() === 'p' && fromFile !== toFile;
        }
      } catch (_) {}
    }

    if (doSound) {
      const fm = g.fullMoves || [];
      for (let i = fm.length - 1; i >= 0; i--) {
        if (fm[i] === uci) { isWhiteMove = (i % 2 === 0); break; }
      }
      if (isCapture || isEp) Sound.capture(isWhiteMove);
      else                   Sound.move(isWhiteMove);
    }

    // When animate=false (queue drain backlog) skip the slide to stay in sync.
    _updateBoard(g, { animateUci: animate ? uci : null }, targetFen);
  }

  /**
   * Update only the best-move arrow overlay without touching pieces or
   * cancelling any in-flight piece animation.
   */
  function _setBestMoveArrow(g) {
    if (!_boardInst) return;
    _boardInst.setOverlay(_buildArrows(g), []);
  }

  /**
   * Render the board to the current g.fen.
   * Pass animateUci to slide a piece rather than teleport.
   * Always updates _prevFen so capture detection stays in sync.
   */
  function _updateBoard(g, { animateUci } = {}, overrideFen = null) {
    if (!_boardInst) { console.warn('[board] _updateBoard: no _boardInst'); return; }
    // Use overrideFen when supplied — this is the per-entry snapshot from the move
    // queue so intermediate snaps render the correct position, not the final FEN.
    const fen = overrideFen || g.fen || '';
    console.log('[board] _updateBoard fen=%s animateUci=%s', fen.slice(0,30), animateUci ?? '—');

    const moves   = g.moves || [];
    const fm      = g.fullMoves || [];
    const lastOur = moves.length > 0 ? moves[moves.length - 1].move : null;

    // Identify opponent's last move for square highlights
    let lastOpp = null;
    for (let i = fm.length - 1; i >= 0; i--) {
      if (!fm[i]) continue;
      const isWhitePly = i % 2 === 0;
      const isOpp = (g.color === 'white' && !isWhitePly) || (g.color === 'black' && isWhitePly);
      if (isOpp) { lastOpp = fm[i]; break; }
    }

    _boardInst.update(fen, {
      flip:       g.color === 'black',
      lastMove:   lastOur,
      oppMove:    lastOpp,
      arrows:     _buildArrows(g),
      animateUci: animateUci || undefined,
    });

    // Remember what the board now shows (used for next move's capture detection)
    _prevFen = fen;
  }

  /* ── eval bar ──────────────────────────────────────────────────────── */

  function _updateEvalBar(g) {
    const fill = document.getElementById('eval-bar-fill');
    const topL = document.getElementById('eval-label-top');
    const botL = document.getElementById('eval-label-bot');
    if (!fill) return;

    // When bot plays black the board is flipped (black at bottom).
    // The .flipped CSS class re-anchors the white fill from bottom→top so it
    // stays visually attached to white's side of the board.
    const barEl = document.getElementById('eval-bar');
    if (barEl) barEl.classList.toggle('flipped', g.color === 'black');

    // Get the latest eval from search or last move
    let evalCp = null, mate = null, isPonder = false, ponderSide = 'opponent';
    if (g.searchLive) {
      evalCp = g.searchLive.eval_cp;
      mate = g.searchLive.mate;
      isPonder = !!g.searchLive.ponder;
      ponderSide = g.searchLive.ponder || 'opponent'; // 'ours' | 'opponent'
      // mate=0 from a ponder means the engine is sitting on a terminal (checkmated)
      // position — it carries no directional information, so ignore it and fall
      // through to the last committed move stat instead.
      if (isPonder && mate === 0) { evalCp = null; mate = null; }
    } else {
      const m = (g.moves || []).length > 0 ? g.moves[g.moves.length - 1] : null;
      if (m) { evalCp = m.eval_cp; mate = m.mate; }
    }

    // cpFromWhite: always from white's POV for bar display.
    // UCI engine reports eval from the side to move:
    //   - our search ('searching' state, or ponderhit ponder 'ours'):
    //       side to move = us  → negate when we are black
    //   - parent-position ponder ('opponent'):
    //       side to move = opponent → negate when opponent is black (= we are white)
    const flipForWhite = (isPonder && ponderSide === 'opponent')
      ? (g.color === 'white')
      : (g.color === 'black');

    let cpFromWhite = 0;
    let mateFromWhite = null;
    let labelText = '0.00';
    if (mate != null) {
      mateFromWhite = flipForWhite ? -mate : mate;
      cpFromWhite = mateFromWhite > 0 ? 9999 : -9999; // sentinel, not used for pct
      labelText = mate === 0 ? '#' : `M${Math.abs(mate)}`;
    } else if (evalCp != null) {
      cpFromWhite = flipForWhite ? -evalCp : evalCp;
      const pawn = cpFromWhite / 100;
      labelText = (pawn >= 0 ? '+' : '') + pawn.toFixed(2);
    }

    const pct = mateFromWhite != null
      ? App.evalPct(mateFromWhite, true)
      : App.evalPct(cpFromWhite, false);
    fill.style.height = pct + '%';

    // Labels: show eval on whichever side is winning.
    // When flipped the fill is anchored to the top (white's side), so the
    // label sides invert — white winning → topL, black winning → botL.
    if (topL && botL) {
      const isFlipped = g.color === 'black';
      if (!isFlipped) {
        if (cpFromWhite <= 0) { topL.textContent = labelText; botL.textContent = ''; }
        else                  { botL.textContent = labelText; topL.textContent = ''; }
      } else {
        if (cpFromWhite >= 0) { topL.textContent = labelText; botL.textContent = ''; }
        else                  { botL.textContent = labelText; topL.textContent = ''; }
      }
    }
  }

  /* ── engine strip ──────────────────────────────────────────────────── */

  function _esCell(label, value, dim = false) {
    return `<div class="es-cell"><span class="es-label">${label}</span><span class="es-value${dim ? ' dim' : ''}">${value}</span></div>`;
  }

  function _updateEngineStrip(g) {
    const el = document.getElementById('engine-strip');
    if (!el) return;

    let stateLabel, stateClass, depth, evalS, conf, nps, time;

    if (g.searchLive && !g.searchLive.ponder) {
      const s = g.searchLive;
      stateLabel = 'thinking'; stateClass = 'thinking';
      depth  = `${s.depth}/${s.seldepth ?? '–'}`;
      evalS  = s.mate != null ? `M${s.mate}` : s.eval_cp != null ? (s.eval_cp / 100).toFixed(2) : '–';
      conf   = s.confidence != null ? (s.confidence * 100).toFixed(0) + '%' : '–';
      nps    = App.fmtN(s.nps ?? 0);
      time   = App.fmtMs(s.elapsed);
    } else if (g.pondering || (g.searchLive && g.searchLive.ponder)) {
      const s = g.searchLive;
      const fromD = g._ponderFromDepth ?? 0;
      stateLabel = 'ponder'; stateClass = 'pondering';
      depth  = s ? `${s.depth}/${s.seldepth ?? '–'}` : `≥${fromD}`;
      evalS  = s && s.eval_cp != null ? (s.eval_cp / 100).toFixed(2) : s && s.mate != null ? `M${s.mate}` : '–';
      conf   = '–';
      nps    = App.fmtN(s ? (s.nps ?? 0) : 0);
      time   = '–';
    } else {
      const m = (g.moves || []).length > 0 ? g.moves[g.moves.length - 1] : null;
      stateLabel = '–'; stateClass = 'idle';
      depth  = m ? `${m.depth}/${m.seldepth ?? '–'}` : '–';
      evalS  = m ? App.evalStr(m) : '–';
      conf   = m && m.confidence != null ? (m.confidence * 100).toFixed(0) + '%' : '–';
      nps    = '–';
      time   = m ? App.fmtMs(m.time_ms) : '–';
    }

    el.innerHTML = `
      <div class="es-state ${stateClass}">${stateLabel}</div>
      ${_esCell('depth', depth)}
      ${_esCell('eval',  evalS)}
      ${_esCell('conf',  conf,  stateClass === 'pondering')}
      ${_esCell('nps',   nps,   stateClass === 'idle')}
      ${_esCell('time',  time,  stateClass === 'pondering')}
    `;
  }

  /* ── PV column ──────────────────────────────────────────────────────── */

  function _updatePvCol(g) {
    const el = document.getElementById('pv-list');
    if (!el) return;

    let pv = null;
    let isPonder = false;
    if (g.searchLive && g.searchLive.pv && g.searchLive.pv.length > 0) {
      pv = g.searchLive.pv;
      isPonder = !!g.searchLive.ponder;
    }

    if (!pv) {
      el.innerHTML = '<div class="pv-empty">–</div>';
      return;
    }

    // Convert UCI moves to SAN; replace piece letter with Unicode glyph
    const sanList = (typeof San !== 'undefined' && g.fen)
      ? San.buildSanList(g.fen, pv.slice(0, 24))
      : pv.slice(0, 24);
    const GLYPHS = { K:'♚', Q:'♛', R:'♜', B:'♝', N:'♞' };
    const toGlyph = s => s ? s.replace(/^([KQRBN])/, (_, p) => GLYPHS[p] || p) : s;

    el.innerHTML = sanList.slice(0, 24).map((mv, i) =>
      `<div class="pv-move${i === 0 ? ' pv-first' : ''}${isPonder && i === 0 ? ' pv-ponder' : ''}">${App.esc(toGlyph(mv))}</div>`
    ).join('');
  }

  /* ── move list ──────────────────────────────────────────────────────── */

  function _updateMoves(g) {
    const tbody = document.querySelector('#move-list tbody');
    if (!tbody) return;

    const fm = g.fullMoves || [];
    const ourMoves = g.moves || [];
    const isWhite = g.color === 'white';

    // Build SAN list for all UCI moves
    const sanList = (typeof San !== 'undefined')
      ? San.buildSanList(g.initialFen || null, fm)
      : fm;

    // Build lookup: ply → our move stat
    const statByPly = new Map();
    for (let i = 0; i < ourMoves.length; i++) {
      const m = ourMoves[i];
      const ply = m.ply != null ? m.ply - 1 : (isWhite ? i * 2 : i * 2 + 1);
      statByPly.set(ply, m);
    }

    let html = '';
    const maxPly = fm.length;
    for (let moveNum = 1; moveNum <= Math.ceil(maxPly / 2) + 1; moveNum++) {
      const wPly = (moveNum - 1) * 2;
      const bPly = wPly + 1;
      const wMove = fm[wPly] || null;
      const bMove = fm[bPly] || null;
      if (!wMove && !bMove) continue;

      const wSan = sanList[wPly] || wMove || '';
      const bSan = sanList[bPly] || bMove || '';

      const wStat = statByPly.get(wPly);
      const bStat = statByPly.get(bPly);
      const wOurs = (isWhite && wStat) ? ' ours' : '';
      const bOurs = (!isWhite && bStat) ? ' ours' : '';
      const wEval = wStat ? `<td class="eval-hint">${App.evalStr(wStat)}</td>` : '<td class="eval-hint"></td>';
      const bEval = bStat ? `<td class="eval-hint">${App.evalStr(bStat)}</td>` : '<td class="eval-hint"></td>';

      // Check if this is the current (latest) move row
      const isCurrent = (bMove && bPly === maxPly - 1) || (!bMove && wMove && wPly === maxPly - 1);

      html += `<tr${isCurrent ? ' class="current"' : ''}>
        <td class="mn">${moveNum}.</td>
        <td class="mw${wOurs}">${wSan}</td>
        ${wEval}
        <td class="mb${bOurs}">${bMove ? bSan : ''}</td>
        ${bEval}
      </tr>`;
    }

    tbody.innerHTML = html;
    // scroll to bottom
    const wrap = tbody.closest('.move-list-wrap');
    if (wrap) wrap.scrollTop = wrap.scrollHeight;
  }

  /* ── comms sub-tab switching ─────────────────────────────────────────── */

  function switchCommsTab(name) {
    _activeCommsTab = name;
    const col = document.querySelector('#panel-game .game-comms-col');
    if (!col) return;
    col.querySelectorAll('.comms-tab').forEach(b => b.classList.toggle('active', b.dataset.comms === name));
    col.querySelectorAll('.comms-pane').forEach(p => p.classList.toggle('active', p.id === `comms-pane-${name}`));
    if (name === 'seek') TabSeek.show();
    if (name === 'ctrl') TabControls.show();
    if (name === 'eng')  TabEnginePane.show();
  }

  function _bindCommsTabsClicks() {
    const col = document.querySelector('#panel-game .game-comms-col');
    if (!col) return;
    col.querySelectorAll('.comms-tab').forEach(btn => {
      btn.addEventListener('click', () => switchCommsTab(btn.dataset.comms));
    });
    // Restore the active sub-tab after a re-render (no-op if already 'chat')
    switchCommsTab(_activeCommsTab);
    _bindGameActionButtons();
  }

  function _bindGameActionButtons() {
    const g = App.currentGame();
    if (!g || g.status === 'finished') return;

    const abortBtn  = document.getElementById('btn-abort');
    const resignBtn = document.getElementById('btn-resign');
    if (!abortBtn || !resignBtn) return;

    // Abort is only meaningful before any moves have been played.
    const movesPlayed = (g.fullMoves || []).filter(Boolean).length;
    abortBtn.disabled = movesPlayed > 0;
    abortBtn.title = movesPlayed > 0
      ? 'Abort is only valid before any moves are played'
      : 'Abort game (no moves played yet)';

    const _doAction = async (btn, action) => {
      if (btn.disabled) return;
      const label = action === 'resign' ? 'Resign' : 'Abort';
      if (!confirm(`${label} this game?`)) return;
      btn.disabled = true;
      btn.textContent = '…';
      try {
        const r = await fetch(`/api/games/${g.id}/${action}`, { method: 'POST' });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) {
          alert(`${label} failed: ${data.error || r.status}`);
          btn.disabled = false;
          btn.textContent = label;
        }
        // On success the game_end SSE event will re-render the panel automatically.
      } catch (e) {
        alert(`${label} error: ${e.message}`);
        btn.disabled = false;
        btn.textContent = label;
      }
    };

    abortBtn.addEventListener('click',  () => _doAction(abortBtn,  'abort'));
    resignBtn.addEventListener('click', () => _doAction(resignBtn, 'resign'));
  }

  /* ── comms feeds ────────────────────────────────────────────────────── */

  function _appendChatInGame(data) {
    // Route to player or spectator section based on the Lichess room field
    const isSpectator = (data.room || 'player') === 'spectator';
    const feedId = isSpectator ? 'game-chat-spectator' : 'game-chat-player';
    const feed = document.getElementById(feedId);
    if (!feed) return;
    const g = App.currentGame();
    const who = data.who || data.username || '?';
    const isBotMsg = g && (who === g.ourName || who === g.opponentName ||
                     who.toLowerCase() === (g.opponentId || '').toLowerCase());
    const div = document.createElement('div');
    div.className = `chat-line${isBotMsg ? ' chat-bot' : ''}`;
    const ts = new Date(data.ts || Date.now()).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    div.innerHTML = `<span class="chat-ts">${ts}</span><span class="chat-who">${App.esc(who)}</span> ${App.esc(data.text || '')}`;
    feed.appendChild(div);
    feed.scrollTop = feed.scrollHeight;
    while (feed.children.length > 200) feed.removeChild(feed.firstChild);
  }

  /* ── Structured log renderer (shared helpers) ─────────────────────── */

  function _parseLogLine(msg) {
    let src = '?', srcType = 'default';
    let rest = (msg || '').trim();
    const tagM = /^\[([^\]]+)\]/.exec(rest);
    if (tagM) {
      const tag = tagM[1]; rest = rest.slice(tagM[0].length).trimStart();
      if      (tag.startsWith('game '))     { src = tag.slice(5).slice(0,10); srcType = 'game'; }
      else if (tag.startsWith('selfplay:')) { src = tag.slice(9).slice(0,8);  srcType = 'selfplay'; }
      else if (tag === 'engine')            { src = 'eng';   srcType = 'engine'; }
      else if (tag === 'board')             { src = 'brd';   srcType = 'board'; }
      else if (tag === 'gameDb')            { src = 'db';    srcType = 'db'; }
      else if (tag === 'lichess')           { src = 'api';   srcType = 'api'; }
      else if (tag === 'ctrl')              { src = 'ctrl';  srcType = 'ctrl'; }
      else if (tag === 'dashState')         { src = 'state'; srcType = 'ctrl'; }
      else                                  { src = tag.slice(0,7); srcType = 'default'; }
    }
    const kvRe = /\b([a-zA-Z_]\w*)=([-\w.]+)/g;
    const kvs = []; let km;
    while ((km = kvRe.exec(rest)) !== null) kvs.push([km[1], km[2]]);
    const firstM = /\b[a-zA-Z_]\w*=[-\w.]/.exec(rest);
    const event = (firstM ? rest.slice(0, firstM.index) : rest).replace(/[:\s,()]+$/, '').trim();
    return { src, srcType, event: event || rest.slice(0, 80), kvs };
  }

  function _kvCls(key, val) {
    if (key === 'eval')   { const n = parseFloat(val); return isNaN(n) ? 'll-v' : (n >= 0 ? 'll-v ll-v-pos' : 'll-v ll-v-neg'); }
    if (key === 'val')    { const n = parseFloat(val); return isNaN(n) ? 'll-v ll-v-depth' : (n >= 0 ? 'll-v ll-v-pos' : 'll-v ll-v-neg'); }
    if (key === 'effective') return 'll-v ll-v-conf';
    if (key === 'depth' || key === 'ply' || key === 'move' || key === 'nodes') return 'll-v ll-v-depth';
    if (key === 'elapsed' || key === 'time' || key === 'clock' || key === 'maxTime' || key === 'maxTimeMs') return 'll-v ll-v-time';
    if (key === 'conf')   return 'll-v ll-v-conf';
    return 'll-v';
  }

  function _appendLogInGame(data) {
    const feed = document.getElementById('game-log-feed');
    if (!feed) return;
    const lvl = (data.level || 'info').toLowerCase();
    const { src, srcType, event, kvs } = _parseLogLine(data.msg || data.message || JSON.stringify(data));
    const ts = data.ts ? new Date(data.ts).toLocaleTimeString() : '';
    const kvHtml = kvs.map(([k, v]) =>
      `<span class="ll-kv"><span class="ll-k">${k}:</span><span class="${_kvCls(k, v)}">${App.esc(v)}</span></span>`
    ).join('');
    const card = document.createElement('div');
    card.className = `ll-card ll-${lvl}`;
    card.title = ts;
    card.innerHTML =
      `<div class="ll-row1"><span class="ll-src ll-src-${srcType}">${App.esc(src)}</span>` +
      `<span class="ll-ev">${App.esc(event)}</span></div>` +
      (kvHtml ? `<div class="ll-row2">${kvHtml}</div>` : '');
    feed.appendChild(card);
    feed.scrollTop = feed.scrollHeight;
    while (feed.children.length > 500) feed.removeChild(feed.firstChild);
  }

  /* ── sparkline ──────────────────────────────────────────────────────── */

  function _updateSparkline(g) {
    const el = document.getElementById('game-sparkline');
    if (!el || (g.moves || []).length < 2) return;
    Chart.renderEval(el, g);
  }

  /* ── clock display ──────────────────────────────────────────────────── */

  function _tickClock(elId, g, isOpp) {
    const el = document.getElementById(elId);
    if (!el || !g.clock) return;

    const isWhite = g.color === 'white';
    // opp is the other color
    let ms;
    let isTurn;
    if (isOpp) {
      ms = isWhite ? (g._bDisplay ?? g.clock.btime) : (g._wDisplay ?? g.clock.wtime);
      isTurn = isWhite ? g._turn === 'b' : g._turn === 'w';
    } else {
      ms = isWhite ? (g._wDisplay ?? g.clock.wtime) : (g._bDisplay ?? g.clock.btime);
      isTurn = isWhite ? g._turn === 'w' : g._turn === 'b';
    }

    el.textContent = App.fmtClock(ms);
    el.classList.toggle('active', isTurn && g.status === 'active');
    el.classList.toggle('low', ms < 30000 && isTurn);

    // ── badge halo ──
    const badgeEl = document.getElementById(isOpp ? 'opp-badge' : 'our-badge');
    if (badgeEl) {
      const startMs    = isOpp ? _oppTurnStartMs : _ourTurnStartMs;
      const elapsedSec = startMs > 0 ? (performance.now() - startMs) / 1000 : 0;
      let state = 'idle';
      if (g.status === 'active' && isTurn) {
        if (ms < 15000 || elapsedSec > 20) state = 'hot';
        else if (ms < 45000 || elapsedSec > 8) state = 'warm';
        else state = 'calm';
      }
      badgeEl.classList.remove('badge-halo-idle', 'badge-halo-calm', 'badge-halo-warm', 'badge-halo-hot');
      badgeEl.classList.add('badge-halo-' + state);
    }
  }

  /* ── PV preview helpers (called by TabEnginePane on row hover) ─────── */

  let _pvPreviewTimeout = null;

  /**
   * Minimal UCI-move FEN applier for PV replay — visual only.
   * Does not track castling availability or en-passant square precisely.
   */
  function _applyUci(fen, uci) {
    if (!fen || !uci || uci.length < 4) return fen;
    const parts = fen.split(' ');
    const turn  = parts[1] || 'w';
    const FILES = 'abcdefgh';
    // Expand FEN board to 8×8
    const b = parts[0].split('/').map(rank => {
      const row = [];
      for (const ch of rank) {
        if (/\d/.test(ch)) for (let i = 0; i < +ch; i++) row.push('');
        else row.push(ch);
      }
      return row;
    });
    const f1 = FILES.indexOf(uci[0]), r1 = 8 - +uci[1];
    const f2 = FILES.indexOf(uci[2]), r2 = 8 - +uci[3];
    if (f1 < 0 || f2 < 0) return fen;
    const promo = uci[4] ? (turn === 'w' ? uci[4].toUpperCase() : uci[4].toLowerCase()) : null;
    const piece = b[r1]?.[f1];
    if (!piece) return fen;
    // En passant: pawn captures diagonally to an empty square
    if ((piece === 'P' || piece === 'p') && f1 !== f2 && !b[r2][f2]) {
      b[r1][f2] = '';
    }
    b[r2][f2] = promo ?? piece;
    b[r1][f1] = '';
    // Castling: move the rook too
    if ((piece === 'K' || piece === 'k') && Math.abs(f2 - f1) === 2) {
      if (f2 > f1) { b[r1][5] = b[r1][7]; b[r1][7] = ''; }
      else         { b[r1][3] = b[r1][0]; b[r1][0] = ''; }
    }
    const newBoard = b.map(row => {
      let s = '', e = 0;
      for (const sq of row) { if (!sq) e++; else { if (e) { s += e; e = 0; } s += sq; } }
      if (e) s += e;
      return s;
    }).join('/');
    return `${newBoard} ${turn === 'w' ? 'b' : 'w'} - - 0 1`;
  }

  /** Step through a PV sequence on the main board (called by TabEnginePane). */
  function previewPv(moves) {
    restoreBoard(); // cancel any prior preview
    const g = App.currentGame();
    if (!g || !_boardInst || !moves?.length) return;
    const flip = g.color === 'black';
    let fen = g.fen;
    let i   = 0;
    function step() {
      if (i >= moves.length) return;
      const move = moves[i++];
      try {
        fen = _applyUci(fen, move);
        _boardInst.update(fen, { lastMove: move, animateUci: move, flip });
      } catch (_) { return; }
      _pvPreviewTimeout = setTimeout(step, 500);
    }
    step();
  }

  /** Restore the board to current game state, cancelling any PV preview. */
  function restoreBoard() {
    if (_pvPreviewTimeout) { clearTimeout(_pvPreviewTimeout); _pvPreviewTimeout = null; }
    if (!_boardInst) return;
    const g = App.currentGame();
    if (g) {
      const lastMove = g.moves?.length ? g.moves[g.moves.length - 1]?.move : null;
      _boardInst.update(g.fen, { lastMove, flip: g.color === 'black' });
    }
  }

  /* ── register ──────────────────────────────────────────────────────── */

  App.registerTab('game', { show, onEvent, onClockTick });
  function redraw() {
    if (_boardInst) _boardInst.refreshPieces();
    const g = App.currentGame();
    if (g) _updateBoard(g);
  }
  function refreshArrows() {
    const g = App.currentGame();
    if (g && _boardInst) _boardInst.setOverlay(_buildArrows(g), []);
  }

  return { show, onEvent, onClockTick, redraw, refreshArrows, switchCommsTab, previewPv, restoreBoard };
})();
