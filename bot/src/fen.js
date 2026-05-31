/**
 * fen.js — Lightweight FEN ↔ UCI-move applier.
 *
 * Given a FEN and a list of UCI moves, produces the resulting FEN.
 * Handles: basic movement, captures, castling, en passant, promotions,
 * castling rights updates, halfmove clock, fullmove counter.
 *
 * No validation — assumes legal moves (the Lichess server enforces legality).
 */
'use strict';

const STARTING_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';

/**
 * Apply a sequence of UCI moves to a FEN string.
 * @param {string} fen          starting FEN (or 'startpos')
 * @param {string[]} uciMoves   e.g. ['e2e4', 'd7d5', 'e4d5']
 * @returns {string}            resulting FEN
 */
function applyMoves(fen, uciMoves) {
  if (!fen || fen === 'startpos') fen = STARTING_FEN;
  let pos = parseFen(fen);
  for (const uci of uciMoves) {
    pos = applyMove(pos, uci);
  }
  return posToFen(pos);
}

/**
 * Apply a single UCI move to a parsed position object.
 */
function applyMove(pos, uci) {
  const board = pos.board.map(row => row.slice()); // deep copy rows
  const fromFile = uci.charCodeAt(0) - 97; // a=0
  const fromRank = uci.charCodeAt(1) - 49; // 1=0
  const toFile   = uci.charCodeAt(2) - 97;
  const toRank   = uci.charCodeAt(3) - 49;
  const promo    = uci.length > 4 ? uci[4] : null; // q, r, b, n

  const piece = board[fromRank][fromFile];
  const captured = board[toRank][toFile];
  const isWhite = piece === piece.toUpperCase();

  // For halfmove clock: reset on capture or pawn move
  const isPawn = piece.toLowerCase() === 'p';
  const isCapture = captured !== '.';
  let halfmove = (isPawn || isCapture) ? 0 : pos.halfmove + 1;
  let ep = '-';
  let castling = pos.castling;

  // ── En passant capture ──────────────────────────────────────────────
  if (isPawn && toFile !== fromFile && !isCapture) {
    // Diagonal pawn move with nothing on target → en passant
    board[fromRank][toFile] = '.'; // remove captured pawn
    halfmove = 0;
  }

  // ── Double pawn push → set en passant square ────────────────────────
  if (isPawn && Math.abs(toRank - fromRank) === 2) {
    const epRank = (fromRank + toRank) / 2;
    ep = String.fromCharCode(97 + fromFile) + (epRank + 1);
  }

  // ── Castling ─────────────────────────────────────────────────────────
  // Handles both standard UCI (king moves 2 files: e1g1) and
  // Chess960/Lichess UCI (king moves to rook square: e1h1).
  if (piece.toLowerCase() === 'k') {
    const ownRook     = isWhite ? 'R' : 'r';
    const stdCastle   = Math.abs(toFile - fromFile) === 2;
    const c960Castle  = toRank === fromRank && captured === ownRook;
    if (stdCastle || c960Castle) {
      const kingsideMove = toFile > fromFile;
      const kingToFile   = kingsideMove ? 6 : 2;
      const rookToFile   = kingsideMove ? 5 : 3;
      const rookFromFile = c960Castle ? toFile : (kingsideMove ? 7 : 0);
      board[fromRank][fromFile]   = '.';     // king leaves
      board[fromRank][rookFromFile] = '.';   // rook leaves
      board[fromRank][kingToFile] = piece;   // king arrives
      board[fromRank][rookToFile] = ownRook; // rook arrives
      const newCastling = updateCastling(castling, piece, fromFile, fromRank, toFile, toRank);
      const turn      = pos.turn === 'w' ? 'b' : 'w';
      const fullmove  = pos.turn === 'b' ? pos.fullmove + 1 : pos.fullmove;
      return { board, turn, castling: newCastling, ep: '-', halfmove: pos.halfmove + 1, fullmove };
    }
  }

  // ── Move the piece ──────────────────────────────────────────────────
  board[fromRank][fromFile] = '.';
  if (promo) {
    board[toRank][toFile] = isWhite ? promo.toUpperCase() : promo.toLowerCase();
  } else {
    board[toRank][toFile] = piece;
  }

  // ── Update castling rights ──────────────────────────────────────────
  castling = updateCastling(castling, piece, fromFile, fromRank, toFile, toRank);

  // ── Advance turn ────────────────────────────────────────────────────
  const turn = pos.turn === 'w' ? 'b' : 'w';
  const fullmove = pos.turn === 'b' ? pos.fullmove + 1 : pos.fullmove;

  return { board, turn, castling, ep, halfmove, fullmove };
}

/**
 * Update castling rights after a move.
 */
function updateCastling(castling, piece, ff, fr, tf, tr) {
  if (castling === '-') return '-';

  let rights = castling;

  // King moved → remove both sides
  if (piece === 'K')  rights = rights.replace(/[KQ]/g, '');
  if (piece === 'k')  rights = rights.replace(/[kq]/g, '');

  // Rook moved from starting square → remove that side
  if (fr === 0 && ff === 0)  rights = rights.replace('Q', ''); // white QR
  if (fr === 0 && ff === 7)  rights = rights.replace('K', ''); // white KR
  if (fr === 7 && ff === 0)  rights = rights.replace('q', ''); // black QR
  if (fr === 7 && ff === 7)  rights = rights.replace('k', ''); // black KR

  // Rook captured on starting square → remove that side
  if (tr === 0 && tf === 0)  rights = rights.replace('Q', '');
  if (tr === 0 && tf === 7)  rights = rights.replace('K', '');
  if (tr === 7 && tf === 0)  rights = rights.replace('q', '');
  if (tr === 7 && tf === 7)  rights = rights.replace('k', '');

  return rights || '-';
}

/**
 * Parse FEN string into a position object.
 * board[0] = rank 1 (white back rank), board[7] = rank 8 (black back rank)
 */
function parseFen(fen) {
  const parts = fen.split(' ');
  const ranks = parts[0].split('/');
  const board = [];

  // FEN ranks go 8→1 (top→bottom), we store 1→8 (index 0 = rank 1)
  for (let r = 7; r >= 0; r--) {
    const row = [];
    for (const ch of ranks[7 - r]) {
      if (ch >= '1' && ch <= '8') {
        for (let i = 0; i < parseInt(ch, 10); i++) row.push('.');
      } else {
        row.push(ch);
      }
    }
    board[r] = row;
  }

  return {
    board,
    turn:     parts[1] || 'w',
    castling: parts[2] || '-',
    ep:       parts[3] || '-',
    halfmove: parseInt(parts[4], 10) || 0,
    fullmove: parseInt(parts[5], 10) || 1,
  };
}

/**
 * Convert position object back to FEN string.
 */
function posToFen(pos) {
  const ranks = [];
  for (let r = 7; r >= 0; r--) {
    let rank = '';
    let empties = 0;
    for (let f = 0; f < 8; f++) {
      if (pos.board[r][f] === '.') {
        empties++;
      } else {
        if (empties > 0) { rank += empties; empties = 0; }
        rank += pos.board[r][f];
      }
    }
    if (empties > 0) rank += empties;
    ranks.push(rank);
  }
  return `${ranks.join('/')} ${pos.turn} ${pos.castling} ${pos.ep} ${pos.halfmove} ${pos.fullmove}`;
}

// ── SAN generation ────────────────────────────────────────────────────────

/**
 * Convert a UCI move to Standard Algebraic Notation (SAN).
 * @param {string} fenStr  FEN of the position BEFORE the move
 * @param {string} uci     UCI move string  e.g. 'e2e4', 'g1f3', 'e7e8q'
 * @returns {string}       SAN  e.g. 'e4', 'Nf3', 'e8=Q+'
 */
function uciToSan(fenStr, uci) {
  const pos      = parseFen(fenStr);
  const fromFile = uci.charCodeAt(0) - 97;
  const fromRank = uci.charCodeAt(1) - 49;
  const toFile   = uci.charCodeAt(2) - 97;
  const toRank   = uci.charCodeAt(3) - 49;
  const promo    = uci.length > 4 ? uci[4].toUpperCase() : null;
  const toSq     = String.fromCharCode(97 + toFile) + (toRank + 1);
  const piece    = pos.board[fromRank][fromFile];

  // Guard: from-square is empty — the engine played an illegal move.
  // Fall back to the raw UCI string rather than producing garbled notation.
  if (!piece || piece === '.') return uci;

  const captured = pos.board[toRank][toFile];
  // en passant: pawn moves diagonally to an empty square
  const isCapture = captured !== '.' || (piece.toLowerCase() === 'p' && toFile !== fromFile);

  const newPos = applyMove(pos, uci);
  const checkSuffix = isInCheck(newPos) ? '+' : '';

  // ── Pawn ──────────────────────────────────────────────────────────────
  if (piece.toLowerCase() === 'p') {
    let san = isCapture
      ? String.fromCharCode(97 + fromFile) + 'x' + toSq
      : toSq;
    if (promo) san += '=' + promo;
    return san + checkSuffix;
  }

  // ── King (including castling — standard and Chess960) ───────────────────
  if (piece.toLowerCase() === 'k') {
    const ownRook    = piece === 'K' ? 'R' : 'r';
    const stdCastle  = Math.abs(toFile - fromFile) === 2;
    const c960Castle = toRank === fromRank && captured === ownRook;
    if (stdCastle || c960Castle) {
      return (toFile > fromFile ? 'O-O' : 'O-O-O') + checkSuffix;
    }
    return 'K' + (isCapture ? 'x' : '') + toSq + checkSuffix;
  }

  // ── Other pieces (with disambiguation) ───────────────────────────────
  const pieceUpper = piece.toUpperCase();
  const ambig      = _ambigSquares(pos, piece, fromFile, fromRank, toFile, toRank);
  let disambig = '';
  if (ambig.length > 0) {
    const fl       = String.fromCharCode(97 + fromFile);
    const sameFile = ambig.some(([f]) => f === fromFile);
    const sameRank = ambig.some(([, r]) => r === fromRank);
    if (!sameFile)       disambig = fl;
    else if (!sameRank)  disambig = String(fromRank + 1);
    else                 disambig = fl + (fromRank + 1);
  }
  return pieceUpper + disambig + (isCapture ? 'x' : '') + toSq + checkSuffix;
}

// ── Check detection ───────────────────────────────────────────────────────

/**
 * Returns true if the side-to-move's king is currently under attack.
 * Works on a parsed position object (from parseFen).
 */
function isInCheck(pos) {
  const stm  = pos.turn;            // side to move (might be in check)
  const king = stm === 'w' ? 'K' : 'k';

  // isAttacker(p): true if p belongs to the OPPONENT (attacker)
  const isAttacker = (p) => {
    if (!p || p === '.') return false;
    return stm === 'w' ? p === p.toLowerCase() : p === p.toUpperCase();
  };

  let kf = -1, kr = -1;
  outer: for (let r = 0; r < 8; r++) {
    for (let f = 0; f < 8; f++) {
      if (pos.board[r][f] === king) { kf = f; kr = r; break outer; }
    }
  }
  if (kf === -1) return false;

  // Rook / Queen — straight rays
  for (const [df, dr] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
    let f = kf + df, r = kr + dr;
    while (f >= 0 && f < 8 && r >= 0 && r < 8) {
      const p = pos.board[r][f];
      if (p !== '.') {
        if (isAttacker(p) && (p.toLowerCase() === 'r' || p.toLowerCase() === 'q')) return true;
        break;
      }
      f += df; r += dr;
    }
  }

  // Bishop / Queen — diagonal rays
  for (const [df, dr] of [[1, 1], [1, -1], [-1, 1], [-1, -1]]) {
    let f = kf + df, r = kr + dr;
    while (f >= 0 && f < 8 && r >= 0 && r < 8) {
      const p = pos.board[r][f];
      if (p !== '.') {
        if (isAttacker(p) && (p.toLowerCase() === 'b' || p.toLowerCase() === 'q')) return true;
        break;
      }
      f += df; r += dr;
    }
  }

  // Knights
  for (const [df, dr] of [[2, 1], [2, -1], [-2, 1], [-2, -1], [1, 2], [1, -2], [-1, 2], [-1, -2]]) {
    const f = kf + df, r = kr + dr;
    if (f < 0 || f >= 8 || r < 0 || r >= 8) continue;
    const p = pos.board[r][f];
    if (isAttacker(p) && p.toLowerCase() === 'n') return true;
  }

  // Pawns — attacker direction depends on who we're checking
  // If stm='w' (white king in check): black pawns attack from rank+1
  // If stm='b' (black king in check): white pawns attack from rank-1
  const pawnDir = stm === 'w' ? 1 : -1;
  for (const df of [-1, 1]) {
    const f = kf + df, r = kr + pawnDir;
    if (f < 0 || f >= 8 || r < 0 || r >= 8) continue;
    const p = pos.board[r][f];
    if (isAttacker(p) && p.toLowerCase() === 'p') return true;
  }

  // Enemy king adjacency
  for (const [df, dr] of [[1, 0], [-1, 0], [0, 1], [0, -1], [1, 1], [1, -1], [-1, 1], [-1, -1]]) {
    const f = kf + df, r = kr + dr;
    if (f < 0 || f >= 8 || r < 0 || r >= 8) continue;
    const p = pos.board[r][f];
    if (isAttacker(p) && p.toLowerCase() === 'k') return true;
  }

  return false;
}

/**
 * Returns true if the side-to-move's king is in check.
 * Convenience wrapper that accepts a FEN string.
 */
function posInCheck(fenStr) {
  return isInCheck(parseFen(fenStr));
}

/**
 * Returns whether the current position is checkmate or stalemate.
 * @param {string} fenStr
 * @returns {{ checkmate: boolean, stalemate: boolean }}
 */
function terminalStatus(fenStr) {
  const pos = parseFen(fenStr);
  const inCheck = isInCheck(pos);
  const hasMove = _hasAnyLegalMove(pos);
  if (hasMove) return { checkmate: false, stalemate: false };
  return { checkmate: inCheck, stalemate: !inCheck };
}

/**
 * Returns true if the side to move has at least one legal move.
 * Generates pseudo-legal moves for each piece then verifies the king
 * is not left in check after the move.
 */
function _hasAnyLegalMove(pos) {
  const stm    = pos.turn;
  const isOwn  = (p) => p !== '.' && (stm === 'w' ? p === p.toUpperCase() : p === p.toLowerCase());
  const isEnemy = (p) => p !== '.' && !isOwn(p);

  for (let fr = 0; fr < 8; fr++) {
    for (let ff = 0; ff < 8; ff++) {
      const piece = pos.board[fr][ff];
      if (!isOwn(piece)) continue;
      const pl = piece.toLowerCase();

      const targets = []; // [toFile, toRank, promoSuffix]

      if (pl === 'k') {
        for (const [df, dr] of [[1,0],[-1,0],[0,1],[0,-1],[1,1],[1,-1],[-1,1],[-1,-1]]) {
          const tf = ff+df, tr = fr+dr;
          if (tf >= 0 && tf < 8 && tr >= 0 && tr < 8 && !isOwn(pos.board[tr][tf]))
            targets.push([tf, tr, '']);
        }
      } else if (pl === 'n') {
        for (const [df, dr] of [[2,1],[2,-1],[-2,1],[-2,-1],[1,2],[1,-2],[-1,2],[-1,-2]]) {
          const tf = ff+df, tr = fr+dr;
          if (tf >= 0 && tf < 8 && tr >= 0 && tr < 8 && !isOwn(pos.board[tr][tf]))
            targets.push([tf, tr, '']);
        }
      } else if (pl === 'p') {
        const dir       = stm === 'w' ? 1 : -1;
        const startRank = stm === 'w' ? 1 : 6;
        const promoRank = stm === 'w' ? 7 : 0;
        // Forward one square
        if (fr + dir >= 0 && fr + dir < 8 && pos.board[fr+dir][ff] === '.')
          targets.push([ff, fr+dir, fr+dir === promoRank ? 'q' : '']);
        // Double push from start rank
        if (fr === startRank && pos.board[fr+dir][ff] === '.' && pos.board[fr+2*dir][ff] === '.')
          targets.push([ff, fr+2*dir, '']);
        // Diagonal captures (including en passant)
        for (const df of [-1, 1]) {
          const tf = ff+df, tr = fr+dir;
          if (tf < 0 || tf >= 8 || tr < 0 || tr >= 8) continue;
          const epSq = `${String.fromCharCode(97+tf)}${tr+1}`;
          if (isEnemy(pos.board[tr][tf]) || (pos.ep !== '-' && pos.ep === epSq))
            targets.push([tf, tr, tr === promoRank ? 'q' : '']);
        }
      } else {
        // Sliding pieces: R, B, Q
        const rays = [];
        if (pl === 'r' || pl === 'q') rays.push([1,0],[-1,0],[0,1],[0,-1]);
        if (pl === 'b' || pl === 'q') rays.push([1,1],[1,-1],[-1,1],[-1,-1]);
        for (const [df, dr] of rays) {
          let tf = ff+df, tr = fr+dr;
          while (tf >= 0 && tf < 8 && tr >= 0 && tr < 8) {
            if (isOwn(pos.board[tr][tf])) break;
            targets.push([tf, tr, '']);
            if (isEnemy(pos.board[tr][tf])) break;
            tf += df; tr += dr;
          }
        }
      }

      // Verify legality: apply move, check our king is not in check
      for (const [tf, tr, suf] of targets) {
        const uci = `${String.fromCharCode(97+ff)}${fr+1}${String.fromCharCode(97+tf)}${tr+1}${suf}`;
        try {
          const newPos  = applyMove(pos, uci);
          // After our move, check that OUR king is safe (turn reverted to stm)
          const checkPos = { ...newPos, turn: stm };
          if (!isInCheck(checkPos)) return true;
        } catch (_) {}
      }
    }
  }
  return false;
}

// ── Disambiguation helpers ────────────────────────────────────────────────

/** Other squares holding the same piece that can also reach (toFile, toRank). */
function _ambigSquares(pos, piece, fromFile, fromRank, toFile, toRank) {
  const result = [];
  for (let r = 0; r < 8; r++) {
    for (let f = 0; f < 8; f++) {
      if (f === fromFile && r === fromRank) continue;
      if (pos.board[r][f] !== piece) continue;
      if (_pieceCanReach(pos, piece.toLowerCase(), f, r, toFile, toRank)) {
        result.push([f, r]);
      }
    }
  }
  return result;
}

function _pieceCanReach(pos, pieceLow, fromF, fromR, toF, toR) {
  const df = toF - fromF, dr = toR - fromR;
  switch (pieceLow) {
    case 'n':
      return (Math.abs(df) === 1 && Math.abs(dr) === 2) || (Math.abs(df) === 2 && Math.abs(dr) === 1);
    case 'b':
      return Math.abs(df) === Math.abs(dr) && df !== 0 && _pathClear(pos, fromF, fromR, toF, toR);
    case 'r':
      return (df === 0 || dr === 0) && (df !== 0 || dr !== 0) && _pathClear(pos, fromF, fromR, toF, toR);
    case 'q':
      return (df === 0 || dr === 0 || Math.abs(df) === Math.abs(dr)) &&
             (df !== 0 || dr !== 0) && _pathClear(pos, fromF, fromR, toF, toR);
    default:
      return false;
  }
}

function _pathClear(pos, fromF, fromR, toF, toR) {
  const sf = Math.sign(toF - fromF), sr = Math.sign(toR - fromR);
  let f = fromF + sf, r = fromR + sr;
  while (f !== toF || r !== toR) {
    if (pos.board[r][f] !== '.') return false;
    f += sf; r += sr;
  }
  return true;
}

module.exports = { applyMoves, parseFen, applyMove, posToFen, STARTING_FEN, uciToSan, isInCheck, posInCheck, terminalStatus };
