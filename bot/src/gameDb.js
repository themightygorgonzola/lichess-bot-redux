'use strict';

/**
 * gameDb.js — Persistent game history: annotated PGN files + SQLite index.
 *
 * Every completed game is saved to TWO places:
 *
 *   1. Monthly annotated PGN file: data/games/YYYY-MM.pgn
 *      - Append-only; one PGN record per game.
 *      - Our moves carry %eval, %depth, %nodes, %time annotations.
 *      - Directly usable by chess tools (Lichess, ChessBase, etc.)
 *      - Directly ingestible by our training pipeline's _iter_pgn path.
 *
 *   2. SQLite index table `game_history` in data/challenger.db
 *      - Fast structured queries (by date, opponent, result, service).
 *      - Contains every field needed for win-rate and trend analysis.
 *      - pgn_file column points to the monthly PGN for full game retrieval.
 *
 * Space cost: ~2–4 KB per raw PGN game.
 *   At 100 games/day → ~300 KB/day, ~100 MB/year.
 *   Monthly files compress to ~35 % of that (text + move notation).
 *
 * API:
 *   saveGame(record)              — async, fire-and-forget safe
 *   queryGames(opts)              — sync, returns rows from SQLite index
 *   pgnFilePath(monthKey)         — absolute path to monthly PGN file
 */

const fsSync = require('fs');
const path   = require('path');
const { db } = require('./botDb');
const { uciToSan, isInCheck, applyMoves, STARTING_FEN } = require('./fen');
const policies = require('./policies');

const GAMES_DIR = path.join(__dirname, '..', '..', 'data', 'games');
fsSync.mkdirSync(GAMES_DIR, { recursive: true });

// Capture startup timestamp once so every PGN knows which run played it.
let _botStartedAt  = null;
let _engineBuild   = null;
try {
  const v = JSON.parse(fsSync.readFileSync(path.join(__dirname, '..', 'version.json'), 'utf8'));
  _botStartedAt = v.startedAt ?? null;
  _engineBuild  = v.build     ?? null;
} catch (_) {}

// ── Schema migration ──────────────────────────────────────────────────────

let _migrated = false;
function _migrate() {
  if (_migrated) return;
  _migrated = true;
  db().exec(`
    CREATE TABLE IF NOT EXISTS game_history (
      id            TEXT    PRIMARY KEY,
      service       TEXT    NOT NULL DEFAULT 'lichess',
      date_utc      TEXT    NOT NULL,
      month_key     TEXT    NOT NULL,
      ts            INTEGER NOT NULL,
      our_color     TEXT,
      opponent      TEXT,
      speed         TEXT,
      rated         INTEGER DEFAULT 0,
      time_control  TEXT,
      our_elo       INTEGER,
      opp_elo       INTEGER,
      variant       TEXT    DEFAULT 'standard',
      result        TEXT,
      reason        TEXT,
      ply_count     INTEGER,
      duration_ms   INTEGER,
      pgn_file      TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_gh_month      ON game_history(month_key);
    CREATE INDEX IF NOT EXISTS idx_gh_ts         ON game_history(ts DESC);
    CREATE INDEX IF NOT EXISTS idx_gh_result     ON game_history(result);
    CREATE INDEX IF NOT EXISTS idx_gh_service    ON game_history(service);
  `);

  // Additive migrations for new columns (safe on existing databases)
  const cols = db().prepare('PRAGMA table_info(game_history)').all().map(c => c.name);
  if (!cols.includes('full_moves'))    db().exec('ALTER TABLE game_history ADD COLUMN full_moves    TEXT DEFAULT NULL');
  if (!cols.includes('initial_fen'))   db().exec('ALTER TABLE game_history ADD COLUMN initial_fen   TEXT DEFAULT NULL');
  if (!cols.includes('bot_result'))    db().exec('ALTER TABLE game_history ADD COLUMN bot_result    TEXT DEFAULT NULL');
  if (!cols.includes('engine_build'))  db().exec('ALTER TABLE game_history ADD COLUMN engine_build  INTEGER DEFAULT NULL');

  // Backfill bot_result for games saved before this column was added.
  // Uses result (PGN notation) + our_color to determine bot perspective.
  db().exec(`
    UPDATE game_history
    SET bot_result = CASE
      WHEN result = '1/2-1/2' THEN 'draw'
      WHEN (result = '1-0' AND our_color = 'white') OR (result = '0-1' AND our_color = 'black') THEN 'win'
      WHEN result IN ('1-0', '0-1') THEN 'loss'
      ELSE NULL
    END
    WHERE bot_result IS NULL AND result IS NOT NULL
  `);
}

// ── Helpers ──────────────────────────────────────────────────────────────

/**
 * Compute the game result from the bot's perspective: 'win' | 'loss' | 'draw' | null.
 */
function _botResult(record) {
  const r = record.result;
  const c = record.color; // 'white' | 'black'
  if (!r || r === '*') return null;
  if (r === '1/2-1/2') return 'draw';
  if ((r === '1-0' && c === 'white') || (r === '0-1' && c === 'black')) return 'win';
  return 'loss';
}

// ── PGN generation ────────────────────────────────────────────────────────

/**
 * Format an %eval annotation string for a bot move.
 * Uses white-relative eval convention (standard PGN).
 */
function _evalComment(ms, botIsWhite) {
  const parts = [];
  if (ms.eval_cp != null) {
    const cp = botIsWhite ? ms.eval_cp : -ms.eval_cp;
    parts.push(`%eval ${(cp / 100).toFixed(2)}`);
  } else if (ms.mate != null) {
    const m = botIsWhite ? ms.mate : -ms.mate;
    // Standard PGN mate format: #3  or  #-3  (no + prefix for positive)
    parts.push(`%eval #${m}`);
  }
  if (ms.depth       > 0)  parts.push(`%depth ${ms.depth}`);
  if (ms.nodes       > 0)  parts.push(`%nodes ${ms.nodes}`);
  if (ms.time_ms     > 0)  parts.push(`%time ${(ms.time_ms / 1000).toFixed(3)}`);
  if (ms.stop_reason)      parts.push(`%stop ${ms.stop_reason}`);
  return parts.length ? `{ ${parts.join(' ')} }` : null;
}

/** Word-wrap PGN move token list to ≤79 chars/line. */
function _wrap(tokens) {
  const lines = [];
  let cur = '';
  for (const t of tokens) {
    const sep = cur ? ' ' : '';
    if (cur.length + sep.length + t.length > 79) {
      if (cur) lines.push(cur);
      cur = t;
    } else {
      cur += sep + t;
    }
  }
  if (cur) lines.push(cur);
  return lines.join('\n');
}

/**
 * Build the full PGN text for a finished game record.
 */
function _buildPgn(record) {
  const botIsWhite = record.color === 'white';
  const ourLabel = record.ourName ?? 'Bot';
  const white  = botIsWhite ? ourLabel : (record.opponentName ?? '?');
  const black  = botIsWhite ? (record.opponentName ?? '?') : ourLabel;
  const wElo   = String(botIsWhite ? (record.ourRating ?? '?') : (record.oppRating ?? '?'));
  const bElo   = String(botIsWhite ? (record.oppRating ?? '?') : (record.ourRating ?? '?'));
  const dateStr = new Date(record.startedAt || Date.now())
    .toISOString().slice(0, 10).replace(/-/g, '.');
  const resultStr = record.result ?? '*';

  // Non-standard start (odds games, From Position, etc.)
  const isStd     = !record.initialFen ||
                    record.initialFen === 'startpos' ||
                    record.initialFen === STARTING_FEN;
  const startFen  = isStd ? null : record.initialFen;

  const headers = [
    `[Event "Bot Game"]`,
    `[Site "${record.service === 'lichess' ? 'lichess.org' : 'localhost'}"]`,
    `[Date "${dateStr}"]`,
    `[Round "-"]`,
    `[White "${white}"]`,
    `[Black "${black}"]`,
    `[Result "${resultStr}"]`,
    `[WhiteElo "${wElo}"]`,
    `[BlackElo "${bElo}"]`,
  ];
  if (record.timeControl)                    headers.push(`[TimeControl "${record.timeControl}"]`);
  if (record.speed)                          headers.push(`[Speed "${record.speed}"]`);
  if (record.variant && record.variant !== 'standard') {
    headers.push(`[Variant "${record.variant}"]`);
  }
  if (startFen) {
    headers.push(`[SetUp "1"]`);
    headers.push(`[FEN "${startFen}"]`);
  }
  headers.push(`[Annotator "Bot Engine"]`);
  headers.push(`[Service "${record.service ?? 'unknown'}"]`);
  headers.push(`[GameId "${record.id}"]`);
  if (_botStartedAt) headers.push(`[BotStartedAt "${_botStartedAt}"]`);
  const _effectiveBuild = record.engineBuild ?? _engineBuild;
  if (_effectiveBuild != null) headers.push(`[EngineBuild "${_effectiveBuild}"]`);

  // Build 1-based ply → moveStat lookup for our moves (evals come from our searches)
  const evalByPly = new Map();
  for (const ms of (record.moves ?? [])) {
    if (ms?.ply != null) evalByPly.set(ms.ply, ms);
  }

  const allMoves  = (record.fullMoves ?? []).filter(Boolean);
  let currentFen  = startFen ?? STARTING_FEN;
  const tokens    = [];

  for (let i = 0; i < allMoves.length; i++) {
    const uci  = allMoves[i];
    const fenParts = currentFen.split(' ');
    const turn     = fenParts[1];
    const fullMove = parseInt(fenParts[5] ?? '1', 10);

    // ── Move number token ────────────────────────────────────────────
    if (turn === 'w') {
      tokens.push(`${fullMove}.`);
    } else if (i === 0) {
      // Game starts with black to move (unusual FEN)
      tokens.push(`${fullMove}...`);
    } else {
      // After a comment, PGN requires repeating the move number with ...
      const prev = tokens[tokens.length - 1];
      if (prev && prev.startsWith('{')) {
        tokens.push(`${fullMove}...`);
      }
    }

    // ── SAN conversion ───────────────────────────────────────────────
    let san;
    try {
      san = uciToSan(currentFen, uci);
    } catch (_) {
      san = uci; // fallback to UCI if SAN generation fails
    }

    // Final move in a checkmate: promote '+' → '#'
    if (i === allMoves.length - 1 && record.resultReason === 'mate' && san.endsWith('+')) {
      san = san.slice(0, -1) + '#';
    }
    tokens.push(san);

    // ── Eval annotation (our moves only) ────────────────────────────
    const ms = evalByPly.get(i + 1); // ply is 1-based
    if (ms) {
      const comment = _evalComment(ms, botIsWhite);
      if (comment) tokens.push(comment);
    }

    // ── Advance position ─────────────────────────────────────────────
    try {
      currentFen = applyMoves(currentFen, [uci]);
    } catch (_) {
      break; // broken FEN — stop here, partial game still saved
    }
  }

  tokens.push(resultStr);

  return headers.join('\n') + '\n\n' + _wrap(tokens) + '\n\n';
}

// ── Eval trace sidecar ───────────────────────────────────────────────────

const TRACE_DIR = path.join(GAMES_DIR, 'trace');

/**
 * Write per-move eval trace data as a sidecar JSON file.
 * Only writes if at least one move has eval_vec data.
 */
function _writeTrace(record) {
  const tracedMoves = (record.moves ?? []).filter(m => m?.eval_vec);
  if (tracedMoves.length === 0) return;

  try {
    fsSync.mkdirSync(TRACE_DIR, { recursive: true });
    // Snapshot active policy settings at save time so traces are self-describing.
    const _defaults  = policies.DEFAULTS;
    const _overrides = policies.getOverrides();
    const policiesSnap = {
      thoughtfulness: _overrides.thoughtfulness ?? _defaults.thoughtfulness,
      evalInfluence:  _overrides.evalInfluence  ?? _defaults.evalInfluence,
    };

    const traceData = {
      id:           record.id,
      initialFen:   record.initialFen   ?? null,
      color:        record.color        ?? null,
      result:       record.result       ?? null,
      reason:       record.resultReason ?? null,
      build:        _engineBuild,
      speed:        record.speed        ?? null,
      time_control: record.timeControl  ?? null,
      service:      record.service      ?? null,
      rated:        record.rated        ?? null,
      policies:     policiesSnap,
      moves: (record.moves ?? []).map(m => ({
        ply:           m.ply,
        move:          m.move,
        fen:           m.fen           ?? null,
        eval_cp:       m.eval_cp       ?? null,
        mate:          m.mate          ?? null,
        depth:         m.depth         ?? null,
        seldepth:      m.seldepth      ?? null,
        nodes:         m.nodes         ?? null,
        nps:           m.nps           ?? null,
        time_ms:       m.time_ms       ?? null,
        min_ms:        m.min_ms        ?? null,
        max_ms:        m.max_ms        ?? null,
        conf_thresh:   m.conf_thresh   ?? null,
        emergency:     m.emergency     ?? null,
        clock_before:  m.clock_before  ?? null,
        stop_reason:   m.stop_reason   ?? null,
        confidence:    m.confidence    ?? null,
        eval_vec:      m.eval_vec      ?? null,
        depth_history: m.depth_history ?? null,
        sf_eval:       m.sf_eval       ?? null,
        sf_depth:      m.sf_depth      ?? null,
      })),
    };
    const tracePath = path.join(TRACE_DIR, `${record.id}.json`);
    fsSync.writeFileSync(tracePath, JSON.stringify(traceData), 'utf8');
    console.log(`[gameDb] trace → trace/${record.id}.json  (${tracedMoves.length} traced moves)`);
  } catch (err) {
    console.warn('[gameDb] trace write failed (ignored):', err.message ?? err);
  }
}

// ── Public API ────────────────────────────────────────────────────────────

/**
 * Save a completed game to the monthly PGN file and the SQLite index.
 * Idempotent — calling twice with the same game ID is safe.
 *
 * @param {object} record  The game record from store.js
 */
async function saveGame(record) {
  if (!record?.id) return;
  _migrate();

  // Skip if already indexed
  const existing = db().prepare('SELECT id FROM game_history WHERE id = ?').get(record.id);
  if (existing) return;

  // ── Generate PGN ─────────────────────────────────────────────────────
  const pgn = _buildPgn(record);

  // ── Monthly PGN file ─────────────────────────────────────────────────
  const ts       = record.startedAt || Date.now();
  const d        = new Date(ts);
  const yy       = d.getUTCFullYear();
  const mm       = String(d.getUTCMonth() + 1).padStart(2, '0');
  const dd       = String(d.getUTCDate()).padStart(2, '0');
  const monthKey = `${yy}-${mm}`;
  const dateUtc  = `${yy}-${mm}-${dd}`;
  const pgnFile  = `games/${monthKey}.pgn`; // relative to data/
  const pgnPath  = path.join(GAMES_DIR, `${monthKey}.pgn`);

  fsSync.appendFileSync(pgnPath, pgn, 'utf8');

  // -- Sidecar eval trace JSON (when eval_vec data is available) --
  _writeTrace(record);

  // ── SQLite index ─────────────────────────────────────────────────────
  const allMoves = (record.fullMoves ?? []).filter(Boolean);
  const dur      = record.endedAt && record.startedAt
    ? record.endedAt - record.startedAt : null;

  db().prepare(`
    INSERT OR IGNORE INTO game_history
      (id, service, date_utc, month_key, ts, our_color, opponent, speed, rated,
       time_control, our_elo, opp_elo, variant, result, reason, ply_count, duration_ms, pgn_file,
       full_moves, initial_fen, bot_result, engine_build)
    VALUES (?,?,?,?,?,?,?,?,?, ?,?,?,?,?,?,?,?,?, ?,?,?,?)
  `).run(
    record.id,
    record.service      ?? 'unknown',
    dateUtc,
    monthKey,
    ts,
    record.color        ?? null,
    record.opponentName ?? record.opponentId ?? null,
    record.speed        ?? null,
    record.rated        ? 1 : 0,
    record.timeControl  ?? null,
    record.ourRating    ?? null,
    record.oppRating    ?? null,
    record.variant      ?? 'standard',
    record.result       ?? null,
    record.resultReason ?? null,
    allMoves.length,
    dur,
    pgnFile,
    JSON.stringify(allMoves),
    record.initialFen   ?? null,
    _botResult(record),
    _engineBuild,
  );

  console.log(`[gameDb] saved game ${record.id} → ${pgnFile}  (${allMoves.length} plies, result=${record.result ?? '*'})`);
}

/**
 * Query the game index.
 *
 * @param {object} opts
 * @param {string=}  opts.service    filter by service id
 * @param {string=}  opts.month      'YYYY-MM'
 * @param {string=}  opts.result     '1-0' | '0-1' | '1/2-1/2'
 * @param {string=}  opts.opponent   substring match on opponent name
 * @param {number=}  opts.limit      default 50, max 500
 * @param {number=}  opts.offset     default 0
 * @returns {object[]}
 */
function queryGames({ service, month, result, opponent, bot_result, limit = 50, offset = 0 } = {}) {
  _migrate();
  const clauses = [], params = [];
  if (service)    { clauses.push('service = ?');      params.push(service); }
  if (month)      { clauses.push('month_key = ?');    params.push(month); }
  if (result)     { clauses.push('result = ?');       params.push(result); }
  if (opponent)   { clauses.push('opponent LIKE ?');  params.push(`%${opponent}%`); }
  if (bot_result) { clauses.push('bot_result = ?');   params.push(bot_result); }
  const where = clauses.length ? `WHERE ${clauses.join(' AND ')}` : '';
  params.push(Math.min(parseInt(limit, 10) || 50, 500), parseInt(offset, 10) || 0);
  return db().prepare(
    `SELECT * FROM game_history ${where} ORDER BY ts DESC LIMIT ? OFFSET ?`
  ).all(...params);
}

/**
 * Return aggregate statistics from the game index.
 * @param {string=} service  optional service filter
 */
function getStats(service) {
  _migrate();
  const where  = service ? 'WHERE service = ?' : '';
  const params = service ? [service] : [];
  const rows   = db().prepare(
    `SELECT result, COUNT(*) as n FROM game_history ${where} GROUP BY result`
  ).all(...params);
  const out = { total: 0, wins: 0, losses: 0, draws: 0, unfinished: 0 };
  for (const r of rows) {
    out.total += r.n;
    if      (r.result === '1-0')       out.wins      += r.n;  // white's POV in PGN
    else if (r.result === '0-1')       out.losses     += r.n;
    else if (r.result === '1/2-1/2')   out.draws      += r.n;
    else                               out.unfinished += r.n;
  }
  // Bot-perspective (color-aware) win/loss/draw
  const botRows = db().prepare(
    `SELECT bot_result, COUNT(*) as n FROM game_history ${where} GROUP BY bot_result`
  ).all(...params);
  out.botWins   = 0;
  out.botLosses = 0;
  out.botDraws  = 0;
  for (const r of botRows) {
    if      (r.bot_result === 'win')  out.botWins   += r.n;
    else if (r.bot_result === 'loss') out.botLosses += r.n;
    else if (r.bot_result === 'draw') out.botDraws  += r.n;
  }
  return out;
}

/**
 * Fetch a single game by id.
 * Returns the game_history row with full_moves parsed as an array, or null.
 * @param {string} id
 * @returns {object|null}
 */
function getGame(id) {
  _migrate();
  const row = db().prepare('SELECT * FROM game_history WHERE id = ?').get(id);
  if (!row) return null;
  try { row.full_moves = JSON.parse(row.full_moves || '[]'); } catch (_) { row.full_moves = []; }
  return row;
}

/**
 * Extract the PGN text for a single game from its monthly file.
 * @param {string} id  game id
 * @returns {string|null}
 */
function getGamePgn(id) {
  _migrate();
  const row = db().prepare('SELECT month_key FROM game_history WHERE id = ?').get(id);
  if (!row) return null;
  const filePath = pgnFilePath(row.month_key);
  if (!fsSync.existsSync(filePath)) return null;
  const content = fsSync.readFileSync(filePath, 'utf8');
  const needle  = `[GameId "${id}"]`;
  const tagIdx  = content.indexOf(needle);
  if (tagIdx === -1) return null;
  // Walk back to the [Event ...] header that starts this game
  const evtIdx = content.lastIndexOf('\n[Event ', tagIdx);
  const start  = evtIdx === -1 ? 0 : evtIdx + 1;
  // Walk forward past the blank line between headers and moves, then to end of game
  const bodyStart = content.indexOf('\n\n', tagIdx);
  if (bodyStart === -1) return content.slice(start).trim() + '\n\n';
  const bodyEnd = content.indexOf('\n\n', bodyStart + 2);
  const end     = bodyEnd === -1 ? content.length : bodyEnd + 2;
  return content.slice(start, end);
}

/**
 * Absolute path to a monthly PGN file.
 * @param {string} monthKey  'YYYY-MM'
 * @returns {string}
 */
function pgnFilePath(monthKey) {
  return path.join(GAMES_DIR, `${monthKey}.pgn`);
}

module.exports = { saveGame, queryGames, getStats, getGame, getGamePgn, pgnFilePath };
