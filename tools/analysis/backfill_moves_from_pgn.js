#!/usr/bin/env node
/**
 * backfill_moves_from_pgn.js
 *
 * Reads all monthly PGN files in data/games/, parses each game's moves,
 * converts SAN → UCI using the same fen.js logic the engine uses,
 * then writes full_moves + initial_fen + bot_result into the SQLite DB
 * for any row where full_moves is currently NULL.
 *
 * Usage:
 *   node tools/backfill_moves_from_pgn.js
 *   node tools/backfill_moves_from_pgn.js --force   # overwrite even if full_moves already set
 */
'use strict';

const fs   = require('fs');
const path = require('path');

const ROOT     = path.join(__dirname, '..', '..');
const GAMES_DIR = path.join(ROOT, 'data', 'games');

// Load bot modules
const { db } = require(path.join(ROOT, 'bot', 'src', 'botDb'));
const { parseFen, applyMove, posToFen, uciToSan, STARTING_FEN } =
  require(path.join(ROOT, 'bot', 'src', 'fen'));

// ── SAN → UCI converter ──────────────────────────────────────────────────────

/**
 * Convert a SAN move to UCI given the current FEN.
 * Uses a round-trip: generate candidate UCI moves, verify via uciToSan().
 * @returns {string|null} UCI string or null if conversion fails
 */
function sanToUci(fenStr, san) {
  // Strip check/checkmate/annotation suffixes
  const clean = san.replace(/[+#!?]/g, '').trim();
  if (!clean) return null;

  const pos = parseFen(fenStr);
  const stm = pos.turn; // 'w' | 'b'

  // ── Castling ──
  if (clean === 'O-O') {
    const rank = stm === 'w' ? '1' : '8';
    return `e${rank}g${rank}`;
  }
  if (clean === 'O-O-O') {
    const rank = stm === 'w' ? '1' : '8';
    return `e${rank}c${rank}`;
  }

  // ── Promotion ── e.g. "e8=Q", "e8Q", "exd8=Q"
  let promoChar = null;
  let work = clean;
  const promoM = work.match(/=?([QRBN])$/i);
  // Only treat as promotion if the move ends with a major/minor piece letter
  // and the target rank is 1 or 8 (we'll detect that from target square below)
  if (promoM) {
    // tentatively strip it; if target rank is 1/8 it's a promo
    const stripped = work.slice(0, work.length - promoM[0].length);
    const targetSq = stripped.slice(-2);
    const targetRank = parseInt(targetSq[1], 10);
    if (targetRank === 1 || targetRank === 8) {
      promoChar = promoM[1].toLowerCase();
      work = stripped;
    }
  }

  // Target square: last 2 chars
  const toSq   = work.slice(-2);
  const toFile = toSq.charCodeAt(0) - 97;
  const toRank = toSq.charCodeAt(1) - 49;
  if (toFile < 0 || toFile > 7 || toRank < 0 || toRank > 7) return null;

  // Piece type and disambiguation
  const rest = work.slice(0, -2); // e.g. 'N', 'Nb', 'N1', 'Nb1', 'x', 'Rx', 'Bx', 'exd'→ we handle 'x' as capture
  let pieceChar, disambig = '';

  if (rest === '' || rest === 'x') {
    // Pawn advance or capture with no explicit pawn letter
    // For capture, file comes from the SAN like "exd5" → rest before 'x' = 'e'
    if (work.includes('x')) {
      // "exd5" → work = "exd5" → rest = "ex" → the file is rest[0]
      const xIdx = work.indexOf('x');
      const fileChar = work[0];
      if (fileChar >= 'a' && fileChar <= 'h') disambig = fileChar;
    }
    pieceChar = stm === 'w' ? 'P' : 'p';
  } else if (rest[0] >= 'A' && rest[0] <= 'Z') {
    // Named piece
    pieceChar = stm === 'w' ? rest[0] : rest[0].toLowerCase();
    disambig = rest.slice(1).replace('x', '');
  } else {
    // Pawn capture: "exd5" → work="exd5", rest="ex" → fileChar = rest[0]
    disambig = rest.replace('x', '');
    pieceChar = stm === 'w' ? 'P' : 'p';
  }

  // Collect candidates: all squares holding pieceChar
  const candidates = [];
  for (let r = 0; r < 8; r++) {
    for (let f = 0; f < 8; f++) {
      if (pos.board[r][f] !== pieceChar) continue;
      // Apply disambiguation filter
      if (disambig.length >= 1) {
        const dc = disambig[0];
        if (dc >= 'a' && dc <= 'h') {
          if (f !== dc.charCodeAt(0) - 97) continue;
          if (disambig.length >= 2) {
            const dr = parseInt(disambig[1], 10) - 1;
            if (r !== dr) continue;
          }
        } else if (dc >= '1' && dc <= '8') {
          if (r !== parseInt(dc, 10) - 1) continue;
        }
      }
      // Pawn legality: reject candidates that can't legally reach the target square
      if (pieceChar.toLowerCase() === 'p') {
        const dir = pieceChar === 'P' ? 1 : -1; // white moves up (+1), black moves down (-1)
        if (f !== toFile) {
          // Diagonal capture: must be exactly one rank forward
          if (r + dir !== toRank) continue;
        } else {
          // Straight push: 1 square forward, or 2 from home rank
          const steps = (toRank - r) * dir;
          if (steps < 1 || steps > 2) continue;
          if (steps === 2 && r !== (pieceChar === 'P' ? 1 : 6)) continue;
        }
      }
      const fromSq = String.fromCharCode(97 + f) + (r + 1);
      const uci = fromSq + toSq + (promoChar || '');
      candidates.push(uci);
    }
  }

  // Verify each candidate via round-trip uciToSan
  for (const uci of candidates) {
    try {
      const derived = uciToSan(fenStr, uci).replace(/[+#!?]/g, '');
      if (derived === clean) return uci;
      // Accept Chess960 castling stored with wrong SAN in old PGNs
      // (e.g., 'Kxh1' stored when correct SAN is 'O-O')
      if (derived === 'O-O' || derived === 'O-O-O') return uci;
    } catch (_) {
      // illegal move candidate — skip
    }
  }

  return null;
}

// ── PGN parser ───────────────────────────────────────────────────────────────

/**
 * Parse a PGN string containing one or more games.
 * Returns array of { headers: Map<string,string>, moves: string[], moveText: string }
 */
function parsePgn(text) {
  const games = [];
  // Split on blank line followed by [ at start of line (next game header)
  // Safer: split into header-block + body-block pairs
  const records = text.split(/\n(?=\[Event\s)/);

  for (const rec of records) {
    if (!rec.trim()) continue;
    const headers = new Map();
    const headerRegex = /^\[(\w+)\s+"([^"]*)"\]/gm;
    let m;
    while ((m = headerRegex.exec(rec)) !== null) {
      headers.set(m[1], m[2]);
    }
    if (!headers.has('Event')) continue;

    // Extract move text: everything after the last header line
    const lastHeaderEnd = rec.lastIndexOf('\n[');
    // Find closing ] of last header
    const bodyStart = rec.indexOf('\n', rec.lastIndexOf(']'));
    let moveText = bodyStart >= 0 ? rec.slice(bodyStart + 1) : '';

    // Strip { ... } comments (including nested content)
    moveText = moveText.replace(/\{[^}]*\}/g, '');
    // Strip ( ... ) recursive annotations
    moveText = moveText.replace(/\([^)]*\)/g, '');
    // Strip game termination markers
    moveText = moveText.replace(/\b(1-0|0-1|1\/2-1\/2|\*)\s*$/, '');
    // Strip move numbers like "1." or "1..." or "10..."
    moveText = moveText.replace(/\d+\.+/g, '');
    // Strip $nnn NAG annotations
    moveText = moveText.replace(/\$\d+/g, '');

    const moves = moveText.trim().split(/\s+/).filter(t =>
      t && !/^\d/.test(t) && t !== '–' && t !== '-' && t !== '*'
    );

    if (headers.has('GameId') && moves.length > 0) {
      games.push({ headers, moves });
    }
  }
  return games;
}

// ── bot_result helper ────────────────────────────────────────────────────────

function botResult(result, ourColor) {
  if (!result || result === '*') return null;
  if (result === '1/2-1/2') return 'draw';
  if ((result === '1-0' && ourColor === 'white') || (result === '0-1' && ourColor === 'black')) return 'win';
  if (result === '1-0' || result === '0-1') return 'loss';
  return null;
}

// ── main ─────────────────────────────────────────────────────────────────────

const forceUpdate = process.argv.includes('--force');

// Ensure schema migration has run
db().exec(`
  CREATE TABLE IF NOT EXISTS game_history (
    id TEXT PRIMARY KEY, service TEXT, date_utc TEXT, month_key TEXT, ts INTEGER,
    our_color TEXT, opponent TEXT, speed TEXT, rated INTEGER, time_control TEXT,
    our_elo INTEGER, opp_elo INTEGER, variant TEXT, result TEXT, reason TEXT,
    ply_count INTEGER, duration_ms INTEGER, pgn_file TEXT
  )
`);
for (const col of ['full_moves', 'initial_fen', 'bot_result']) {
  try { db().exec(`ALTER TABLE game_history ADD COLUMN ${col} TEXT DEFAULT NULL`); } catch (_) {}
}

const pgnFiles = fs.readdirSync(GAMES_DIR)
  .filter(f => f.endsWith('.pgn'))
  .sort()
  .map(f => path.join(GAMES_DIR, f));

if (pgnFiles.length === 0) {
  console.log('No PGN files found in', GAMES_DIR);
  process.exit(0);
}

const updateStmt = db().prepare(`
  UPDATE game_history
  SET full_moves = ?, initial_fen = ?, bot_result = ?
  WHERE id = ?
`);

let totalGames = 0, updated = 0, skipped = 0, failed = 0;

for (const pgnFile of pgnFiles) {
  console.log(`\nParsing ${path.basename(pgnFile)}…`);
  const text = fs.readFileSync(pgnFile, 'utf8');
  const games = parsePgn(text);
  console.log(`  Found ${games.length} game records`);

  for (const { headers, moves } of games) {
    totalGames++;
    const gameId = headers.get('GameId');

    // Check if already filled
    if (!forceUpdate) {
      const row = db().prepare('SELECT full_moves FROM game_history WHERE id = ?').get(gameId);
      if (!row) { skipped++; continue; } // not in DB at all
      if (row.full_moves !== null && row.full_moves !== '') { skipped++; continue; }
    }

    // Determine starting FEN
    const fenHeader = headers.get('FEN');
    const startFen  = (fenHeader && fenHeader !== STARTING_FEN) ? fenHeader : STARTING_FEN;

    // Convert SAN → UCI
    let pos = parseFen(startFen);
    const uciMoves = [];
    let convErr = false;

    for (const san of moves) {
      const fenStr = posToFen(pos);
      const uci = sanToUci(fenStr, san);
      if (!uci) {
        console.warn(`  [WARN] ${gameId}: failed to convert SAN "${san}" at ply ${uciMoves.length + 1} (fen: ${fenStr})`);
        convErr = true;
        break;
      }
      uciMoves.push(uci);
      pos = applyMove(pos, uci);
    }

    // On conversion error: store partial moves (better than nothing)
    if (convErr) { failed++; }

    // Skip entirely if we have zero moves and the game errored early
    if (convErr && uciMoves.length === 0) continue;

    // Determine our_color: from DB or from White == 'Bot'
    const dbRow = db().prepare('SELECT our_color, result FROM game_history WHERE id = ?').get(gameId);
    const ourColor = dbRow?.our_color
      ?? (headers.get('White') === 'Bot' ? 'white' : 'black');
    const result = dbRow?.result ?? headers.get('Result') ?? null;

    const info = updateStmt.run(
      JSON.stringify(uciMoves),
      startFen === STARTING_FEN ? null : startFen,
      botResult(result, ourColor),
      gameId
    );

    if (info.changes > 0) {
      updated++;
      console.log(`  ✓ ${gameId}  (${uciMoves.length} plies)`);
    } else {
      skipped++;
      console.log(`  – ${gameId}  (not in DB, skipped)`);
    }
  }
}

console.log(`\nDone.  ${updated} updated,  ${skipped} skipped,  ${failed} failed  (${totalGames} total PGN records)`);
