'use strict';

/**
 * store.js — In-memory game state + SSE broadcast hub.
 *
 * All active and recently finished games live here.
 * Dashboard clients register as SSE listeners and receive
 * push events via `emit()`.
 *
 * SSE event types:
 *   snapshot      — full state on connect
 *   game_start    — new game created
 *   game_end      — game over (result, reason, duration)
 *   move          — our engine's move + stats
 *   opponent_move — opponent's move (UCI string, ply)
 *   clock_update  — latest clocks { gameId, wtime, btime }
 *   search_start  — engine begins thinking { gameId, profile }
 *   search_info   — per-depth info line { gameId, ...info, confidence, elapsed }
 *   search_end    — engine returns bestmove { gameId, move, ponderMove, elapsed, finalConf }
 *   ponder_start  — ponder begins { gameId, ponderMove }
 *   ponder_end    — ponder resolved { gameId, hit }
 *   chat_line     — chat event { gameId, room, who, text, ts }
 *   log           — structured log line { level, msg, ts, gameId? }
 */

const gameDb = require('./gameDb');

// gameId → GameRecord
const games = new Map();

// Set of active SSE response objects (dashboard clients)
const sseClients = new Set();

// Log ring buffer — sent to new clients in the snapshot so they see history
const _logBuffer = [];
const MAX_LOG_BUFFER = 500;

// search_info throttle — emit at most once per SEARCH_INFO_THROTTLE_MS per game
const SEARCH_INFO_THROTTLE_MS = 100;
// gameId → { lastEmitMs, pendingData, timerId }
const _searchInfoThrottle = new Map();

/**
 * @typedef {Object} MoveStat
 * @property {string}  move        UCI move string
 * @property {number}  depth       search depth reached
 * @property {number}  seldepth
 * @property {number}  nodes
 * @property {number}  nps
 * @property {number}  time_ms     wall clock ms for this move
 * @property {number|null} eval_cp centipawn score (null if mate)
 * @property {number|null} mate    mate in N (null if cp)
 * @property {number}  ply         half-move number
 * @property {string|null} stop_reason  reason search was stopped (mate_found|budget|budget65|confident)
 */

/**
 * @typedef {Object} ChatLine
 * @property {string}  room    'player' | 'spectator'
 * @property {string}  who     username
 * @property {string}  text
 * @property {number}  ts      epoch ms
 */

/**
 * @typedef {Object} GameRecord
 * @property {string}   id
 * @property {string}   color        'white' | 'black'
 * @property {string}   opponentId
 * @property {string}   opponentName
 * @property {boolean}  opponentIsBot
 * @property {string}   variant
 * @property {string}   speed         bullet/blitz/rapid/classical/correspondence
 * @property {string}   status        'active' | 'finished'
 * @property {string|null} result     '1-0' | '0-1' | '1/2-1/2' | null
 * @property {string|null} resultReason
 * @property {MoveStat[]} moves       our engine's moves + stats
 * @property {string[]} fullMoves     ALL moves in game order (ours + opponent)
 * @property {string}   fen           current position FEN
 * @property {{ wtime:number, btime:number }|null} clock  latest clocks
 * @property {ChatLine[]} chat        full chat history both rooms
 * @property {string|null} ponderMove  move engine is currently pondering
 * @property {object|null} searchLive  last search_info snapshot
 * @property {number|null} confidence  last computed confidence (0–1)
 * @property {number}   startedAt    epoch ms
 * @property {number|null} endedAt
 */

function createGame(gameId, meta) {
  const record = {
    id:           gameId,
    color:        meta.color,
    opponentId:   meta.opponentId   ?? '?',
    opponentName: meta.opponentName ?? '?',
    opponentIsBot:meta.opponentIsBot ?? false,
    variant:      meta.variant      ?? 'standard',
    speed:        meta.speed        ?? 'unknown',
    rated:        meta.rated        ?? false,
    ourName:      meta.ourName      ?? null,
    engineBuild:  meta.engineBuild  ?? null,
    ourRating:    null,
    oppRating:    null,
    timeControl:  null,
    service:      meta.service      ?? 'unknown',
    status:       'active',
    result:       null,
    resultReason: null,
    moves:        [],
    fullMoves:    [],
    initialFen:   meta.initialFen ?? 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
    fen:          meta.initialFen ?? 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
    clock:        null,
    chat:         [],
    ponderMove:   null,
    searchLive:   null,
    confidence:   null,
    startedAt:    Date.now(),
    endedAt:      null,
  };
  games.set(gameId, record);
  emit('game_start', sanitize(record));
  return record;
}

function recordMove(gameId, moveStat) {
  const g = games.get(gameId);
  if (!g) return;
  g.moves.push(moveStat);
  emit('move', { gameId, moveStat });
}

/**
 * Retroactively stamp SF's eval onto the most recently recorded bot move.
 * Called by SelfPlayAdapter when SF finishes thinking about the position
 * that the bot just created — so result.eval_cp is the correct sf_eval.
 */
function updateLastMoveSfEval(gameId, sfEval, sfDepth) {
  const g = games.get(gameId);
  if (!g || g.moves.length === 0) return;
  const last = g.moves[g.moves.length - 1];
  if (last) { last.sf_eval = sfEval ?? null; last.sf_depth = sfDepth ?? null; }
}

/** Retroactively stamp eval_vec on the last recorded move (patched in after getEvalVec). */
function updateLastMoveEvalVec(gameId, evalVec) {
  const g = games.get(gameId);
  if (!g || g.moves.length === 0) return;
  const last = g.moves[g.moves.length - 1];
  if (last) last.eval_vec = evalVec ?? null;
}

/** Record any move (ours or opponent) in the full timeline. */
function recordFullMove(gameId, uciMove, ply) {
  const g = games.get(gameId);
  if (!g) return;
  // Extend the array to the right length (opponent moves may arrive first)
  while (g.fullMoves.length < ply) g.fullMoves.push(null);
  g.fullMoves[ply] = uciMove;
}

/** Record opponent's move and push SSE event. */
function recordOpponentMove(gameId, uciMove, ply) {
  recordFullMove(gameId, uciMove, ply);
  emit('opponent_move', { gameId, move: uciMove, ply });
}

/** Update FEN for the current position. */
function updateFen(gameId, fen) {
  const g = games.get(gameId);
  if (!g) return;
  g.fen = fen;
  emit('fen_update', { gameId, fen });
}

/** Push clock update to dashboard. */
function updateClock(gameId, wtime, btime) {
  const g = games.get(gameId);
  if (!g) return;
  g.clock = { wtime, btime };
  emit('clock_update', { gameId, wtime, btime });
}

/** Record a chat line and push to dashboard. */
function recordChat(gameId, room, who, text) {
  const g = games.get(gameId);
  if (!g) return;
  const line = { room, who, text, ts: Date.now() };
  g.chat.push(line);
  emit('chat_line', { gameId, ...line });
}

/**
 * Ponder info line — passes depth/eval data to dashboard during opponent's turn.
 * @param {string} gameId
 * @param {object} info
 * @param {'ours'|'opponent'} [ponderSide='opponent']
 *   'opponent' = parent-position ponder (engine scores from opponent's POV)
 *   'ours'     = ponderhit ponder (engine already past the predicted reply;
 *                scores are from our POV, same as a real search)
 */
function ponderInfo(gameId, info, ponderSide = 'opponent') {
  const g = games.get(gameId);
  if (!g) return;
  // Re-use searchLive slot — ponderSide tells the eval bar which way to flip.
  g.searchLive = { ...info, ponder: ponderSide };
  emit('search_info', { gameId, ...info, ponder: ponderSide });
}

/** Engine has started thinking. */
// seedInfo: optional last-known info snapshot (from previous move's search or ponder).
// Emitted immediately after search_start so the dashboard shows something useful
// while waiting for the engine's first depth line (which can take >5s at high depth).
function searchStart(gameId, profile, seedInfo) {
  const g = games.get(gameId);
  if (!g) return;
  g.searchLive = null;
  g.confidence = null;
  emit('search_start', { gameId, profile });
  if (seedInfo) {
    // Emit as a regular search_info tagged stale=true.  The dashboard ingests it
    // normally so depth/eval/PV are visible immediately; fresh engine lines
    // replace it as soon as they arrive.  elapsed=0 since clock has just reset.
    g.searchLive = { ...seedInfo, confidence: null, elapsed: 0, stale: true };
    emit('search_info', { gameId, ...seedInfo, confidence: null, elapsed: 0, stale: true });
  }
}

/** Per-depth info line from engine — throttled to SEARCH_INFO_THROTTLE_MS per game. */
function searchInfo(gameId, info, confidence, elapsed) {
  const g = games.get(gameId);
  if (!g) return;
  // Always update in-memory state immediately so snapshots are current.
  g.searchLive = { ...info, confidence, elapsed };
  g.confidence = confidence;

  const now = Date.now();
  let state = _searchInfoThrottle.get(gameId);
  if (!state) {
    state = { lastEmitMs: 0, pendingData: null, timerId: null };
    _searchInfoThrottle.set(gameId, state);
  }

  const payload = { gameId, ...info, confidence, elapsed };
  const msSinceLast = now - state.lastEmitMs;

  if (msSinceLast >= SEARCH_INFO_THROTTLE_MS) {
    // Enough time has passed — emit immediately.
    if (state.timerId !== null) { clearTimeout(state.timerId); state.timerId = null; }
    state.pendingData = null;
    state.lastEmitMs = now;
    emit('search_info', payload);
  } else {
    // Too soon — queue the latest payload; the pending timer will flush it.
    state.pendingData = payload;
    if (state.timerId === null) {
      state.timerId = setTimeout(() => {
        state.timerId = null;
        if (state.pendingData) {
          state.lastEmitMs = Date.now();
          emit('search_info', state.pendingData);
          state.pendingData = null;
        }
      }, SEARCH_INFO_THROTTLE_MS - msSinceLast);
    }
  }
}

/** Engine returned bestmove. */
function searchEnd(gameId, move, ponderMove, elapsed, wallElapsed, finalConf) {
  const g = games.get(gameId);
  if (!g) return;
  g.searchLive  = null;
  g.ponderMove  = ponderMove ?? null;
  g.confidence  = finalConf;
  // Cancel any pending throttled search_info for this game.
  const ts = _searchInfoThrottle.get(gameId);
  if (ts) { if (ts.timerId !== null) clearTimeout(ts.timerId); _searchInfoThrottle.delete(gameId); }
  emit('search_end', { gameId, move, ponderMove, elapsed, wallElapsed, finalConf });
}

/** Ponder started/ended. */
function ponderStart(gameId, fromDepth, ponderMove) {
  const g = games.get(gameId);
  if (!g) return;
  g.ponderMove   = ponderMove ?? null;
  g.ponderFromDepth = fromDepth ?? 0;
  emit('ponder_start', { gameId, fromDepth: g.ponderFromDepth, ponderMove: g.ponderMove });
}

function ponderEnd(gameId, hit) {
  const g = games.get(gameId);
  if (!g) return;
  g.ponderMove = null;
  emit('ponder_end', { gameId, hit });
}

/** Update game metadata (ratings, time control, rated flag). */
function updateGameMeta(gameId, meta) {
  const g = games.get(gameId);
  if (!g) return;
  if (meta.ourRating   != null) g.ourRating   = meta.ourRating;
  if (meta.oppRating   != null) g.oppRating   = meta.oppRating;
  if (meta.rated       != null) g.rated       = meta.rated;
  if (meta.timeControl != null) g.timeControl = meta.timeControl;
  if (meta.initialFen  != null) {
    g.initialFen = meta.initialFen;
    // Sync g.fen to the real start FEN before any moves are made so the
    // dashboard board doesn't flash the default starting position for
    // custom/odds positions (e.g. all-knights).
    if (g.fullMoves.length === 0) g.fen = meta.initialFen;
  }
  emit('game_meta', { gameId, ...meta });
}

function endGame(gameId, result, reason) {
  const g = games.get(gameId);
  if (!g) return;
  g.status      = 'finished';
  g.result      = result  ?? null;
  g.resultReason= reason  ?? null;
  g.endedAt     = Date.now();
  g.searchLive  = null;
  g.ponderMove  = null;
  emit('game_end', { gameId, result, reason, durationMs: g.endedAt - g.startedAt });

  // Persist the completed game asynchronously (fire-and-forget).
  gameDb.saveGame(g).catch(err =>
    console.error('[gameDb] save error for', gameId, ':', err.message)
  );

  // Evict oldest finished games beyond the cap to prevent unbounded growth.
  const MAX_FINISHED = 20;
  const finished = [];
  for (const [id, rec] of games) {
    if (rec.status === 'finished') finished.push([id, rec]);
  }
  if (finished.length > MAX_FINISHED) {
    finished.sort((a, b) => a[1].endedAt - b[1].endedAt);
    for (let i = 0; i < finished.length - MAX_FINISHED; i++) {
      games.delete(finished[i][0]);
    }
  }
}

function addSseClient(res) {
  sseClients.add(res);
  // Send full state snapshot on connect
  const snapshot = {
    type: 'snapshot',
    data: {
      games: [...games.values()].map(sanitize),
      logs:  [..._logBuffer],
    },
  };
  res.write(`data: ${JSON.stringify(snapshot)}\n\n`);
}

function removeSseClient(res) {
  sseClients.delete(res);
}

function emit(type, data) {
  // Buffer log events so late-connecting clients see history in the snapshot.
  if (type === 'log') {
    _logBuffer.push(data);
    if (_logBuffer.length > MAX_LOG_BUFFER) _logBuffer.shift();
  }
  if (sseClients.size === 0) return;
  const msg = `data: ${JSON.stringify({ type, data })}\n\n`;
  for (const res of sseClients) {
    try {
      res.write(msg);
      if (res.flush) res.flush(); // prevent TCP Nagle buffering on search_info bursts
    } catch (_) {
      sseClients.delete(res);
    }
  }
}

/** Strip internal fields for the wire */
function sanitize(g) {
  return { ...g };
}

function getGame(gameId) {
  return games.get(gameId);
}

function activeCount() {
  let n = 0;
  for (const g of games.values()) if (g.status === 'active') n++;
  return n;
}

module.exports = {
  games,
  createGame,
  recordMove,
  updateLastMoveSfEval,
  updateLastMoveEvalVec,
  recordFullMove,
  recordOpponentMove,
  updateFen,
  updateClock,
  recordChat,
  searchStart,
  searchInfo,
  searchEnd,
  ponderStart,
  ponderInfo,
  ponderEnd,
  updateGameMeta,
  endGame,
  addSseClient,
  removeSseClient,
  emit,
  getGame,
  activeCount,
};
