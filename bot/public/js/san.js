/**
 * san.js — UCI → SAN converter for the browser.
 *
 * Board layout matches fen.js:
 *   board[rank][file]  rank 0 = rank 1 (white back rank), rank 7 = rank 8
 *   file 0 = a … file 7 = h
 *   Uppercase = white, lowercase = black, '.' = empty
 *
 * Exports: San.uciToSan(fen, uci) → "Nf3", "exd5+", "O-O", "e8=Q#" …
 *          San.buildSanList(startFen, uciMoves) → string[]
 */
'use strict';

const San = (() => {

  const START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';

  /* ── FEN parser (mirrors fen.js) ───────────────────────────────────── */

  function parseFen(fen) {
    if (!fen || fen === 'startpos') fen = START_FEN;
    const parts = fen.split(' ');
    const ranks = parts[0].split('/');
    const board = [];
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

  /* ── Move applier (mirrors fen.js) ─────────────────────────────────── */

  function applyMove(pos, uci) {
    const board = pos.board.map(r => r.slice());
    const ff = uci.charCodeAt(0) - 97;
    const fr = uci.charCodeAt(1) - 49;
    const tf = uci.charCodeAt(2) - 97;
    const tr = uci.charCodeAt(3) - 49;
    const promo = uci.length > 4 ? uci[4] : null;
    const piece = board[fr][ff];
    const isWhite = piece !== '.' && piece === piece.toUpperCase();
    const isPawn = piece.toLowerCase() === 'p';
    const isCapture = board[tr][tf] !== '.';

    let ep = '-';
    let castling = pos.castling;

    // En passant capture
    if (isPawn && tf !== ff && !isCapture) {
      board[fr][tf] = '.';
    }
    // Double push → set EP square
    if (isPawn && Math.abs(tr - fr) === 2) {
      ep = String.fromCharCode(97 + ff) + ((fr + tr) / 2 + 1);
    }
    // Castling: move rook
    if (piece.toLowerCase() === 'k' && Math.abs(tf - ff) === 2) {
      if (tf > ff) { board[fr][5] = board[fr][7]; board[fr][7] = '.'; }
      else         { board[fr][3] = board[fr][0]; board[fr][0] = '.'; }
    }
    // Move piece
    board[fr][ff] = '.';
    board[tr][tf] = promo ? (isWhite ? promo.toUpperCase() : promo.toLowerCase()) : piece;

    // Update castling rights
    if (castling !== '-') {
      let c = castling;
      if (piece === 'K') c = c.replace(/[KQ]/g, '');
      if (piece === 'k') c = c.replace(/[kq]/g, '');
      if (fr === 0 && ff === 0) c = c.replace('Q', '');
      if (fr === 0 && ff === 7) c = c.replace('K', '');
      if (fr === 7 && ff === 0) c = c.replace('q', '');
      if (fr === 7 && ff === 7) c = c.replace('k', '');
      if (tr === 0 && tf === 0) c = c.replace('Q', '');
      if (tr === 0 && tf === 7) c = c.replace('K', '');
      if (tr === 7 && tf === 0) c = c.replace('q', '');
      if (tr === 7 && tf === 7) c = c.replace('k', '');
      castling = c || '-';
    }

    return {
      board,
      turn:     pos.turn === 'w' ? 'b' : 'w',
      castling,
      ep,
      halfmove: (isPawn || isCapture) ? 0 : pos.halfmove + 1,
      fullmove: pos.turn === 'b' ? pos.fullmove + 1 : pos.fullmove,
    };
  }

  /* ── Attack detection ───────────────────────────────────────────────── */

  /**
   * Is square (rank, file) attacked by pieces of the given colour?
   */
  function isAttackedBy(board, rank, file, byWhite) {
    const P = byWhite ? 'P' : 'p';
    const N = byWhite ? 'N' : 'n';
    const B = byWhite ? 'B' : 'b';
    const R = byWhite ? 'R' : 'r';
    const Q = byWhite ? 'Q' : 'q';
    const K = byWhite ? 'K' : 'k';

    // Pawn: white pawns attack upward (from lower rank), black downward
    const pr = byWhite ? rank - 1 : rank + 1;
    if (pr >= 0 && pr < 8) {
      if (file > 0 && board[pr][file - 1] === P) return true;
      if (file < 7 && board[pr][file + 1] === P) return true;
    }

    // Knight
    for (const [dr, df] of [[-2,-1],[-2,1],[-1,-2],[-1,2],[1,-2],[1,2],[2,-1],[2,1]]) {
      const nr = rank + dr, nf = file + df;
      if (nr >= 0 && nr < 8 && nf >= 0 && nf < 8 && board[nr][nf] === N) return true;
    }

    // King
    for (const [dr, df] of [[-1,-1],[-1,0],[-1,1],[0,-1],[0,1],[1,-1],[1,0],[1,1]]) {
      const nr = rank + dr, nf = file + df;
      if (nr >= 0 && nr < 8 && nf >= 0 && nf < 8 && board[nr][nf] === K) return true;
    }

    // Diagonals (bishop / queen)
    for (const [dr, df] of [[-1,-1],[-1,1],[1,-1],[1,1]]) {
      let r = rank + dr, f = file + df;
      while (r >= 0 && r < 8 && f >= 0 && f < 8) {
        const p = board[r][f];
        if (p !== '.') { if (p === B || p === Q) return true; break; }
        r += dr; f += df;
      }
    }

    // Straights (rook / queen)
    for (const [dr, df] of [[-1,0],[1,0],[0,-1],[0,1]]) {
      let r = rank + dr, f = file + df;
      while (r >= 0 && r < 8 && f >= 0 && f < 8) {
        const p = board[r][f];
        if (p !== '.') { if (p === R || p === Q) return true; break; }
        r += dr; f += df;
      }
    }

    return false;
  }

  /** Is the king of `colour` ('w'|'b') in check in this position? */
  function inCheck(board, colour) {
    const king = colour === 'w' ? 'K' : 'k';
    for (let r = 0; r < 8; r++) {
      for (let f = 0; f < 8; f++) {
        if (board[r][f] === king) {
          return isAttackedBy(board, r, f, colour !== 'w');
        }
      }
    }
    return false;
  }

  /* ── Pseudo-legal reach (for disambiguation) ─────────────────────── */

  function canReach(board, piece, fr, ff, tr, tf) {
    const t = piece.toLowerCase();
    const dr = tr - fr, df = tf - ff;

    if (t === 'n') {
      return (Math.abs(dr) === 2 && Math.abs(df) === 1) ||
             (Math.abs(dr) === 1 && Math.abs(df) === 2);
    }
    if (t === 'k') {
      return Math.abs(dr) <= 1 && Math.abs(df) <= 1;
    }

    // Sliding pieces: check clear path
    const isDiag  = Math.abs(dr) === Math.abs(df) && dr !== 0;
    const isStraight = (dr === 0) !== (df === 0);

    const useDiag    = t === 'b' || t === 'q';
    const useStraight = t === 'r' || t === 'q';

    if (isDiag && useDiag) {
      const sr = dr > 0 ? 1 : -1, sf = df > 0 ? 1 : -1;
      let r = fr + sr, f = ff + sf;
      while (r !== tr || f !== tf) {
        if (board[r][f] !== '.') return false;
        r += sr; f += sf;
      }
      return true;
    }
    if (isStraight && useStraight) {
      const sr = Math.sign(dr), sf = Math.sign(df);
      let r = fr + sr, f = ff + sf;
      while (r !== tr || f !== tf) {
        if (board[r][f] !== '.') return false;
        r += sr; f += sf;
      }
      return true;
    }
    return false;
  }

  /* ── Disambiguation string ──────────────────────────────────────────── */

  function disambig(board, piece, fr, ff, tr, tf) {
    const rivals = [];
    for (let r = 0; r < 8; r++) {
      for (let f = 0; f < 8; f++) {
        if ((r !== fr || f !== ff) && board[r][f] === piece && canReach(board, piece, r, f, tr, tf)) {
          rivals.push([r, f]);
        }
      }
    }
    if (rivals.length === 0) return '';
    const sameFile = rivals.some(([, f]) => f === ff);
    const sameRank = rivals.some(([r]) => r === fr);
    if (!sameFile) return 'abcdefgh'[ff];
    if (!sameRank) return String(fr + 1);
    return 'abcdefgh'[ff] + (fr + 1);
  }

  /* ── Main converter ─────────────────────────────────────────────────── */

  /**
   * Convert a single UCI move to SAN, given the FEN before the move.
   * Returns the UCI string unchanged on any error.
   */
  function uciToSan(fen, uci) {
    if (!uci || uci.length < 4) return uci || '';
    try {
      const pos = parseFen(fen);
      const board = pos.board;
      const ff = uci.charCodeAt(0) - 97;
      const fr = uci.charCodeAt(1) - 49;
      const tf = uci.charCodeAt(2) - 97;
      const tr = uci.charCodeAt(3) - 49;
      const promo = uci.length > 4 ? uci[4].toUpperCase() : null;

      const piece = board[fr][ff];
      if (!piece || piece === '.') return uci;

      const isWhite = piece === piece.toUpperCase();
      const pType = piece.toLowerCase();
      const captured = board[tr][tf];
      const isCapture = captured !== '.';
      const toSq = 'abcdefgh'[tf] + (tr + 1);

      // En passant: diagonal pawn move to empty square matching EP target
      const isEP = pType === 'p' && tf !== ff && !isCapture
                   && pos.ep === toSq;

      // Castling: king moves 2 files
      const isCastle = pType === 'k' && Math.abs(tf - ff) === 2;

      let san;
      if (isCastle) {
        san = tf > ff ? 'O-O' : 'O-O-O';
      } else if (pType === 'p') {
        if (isCapture || isEP) {
          san = 'abcdefgh'[ff] + 'x' + toSq;
        } else {
          san = toSq;
        }
        if (promo) san += '=' + promo;
      } else {
        const cap = isCapture ? 'x' : '';
        const dis = disambig(board, piece, fr, ff, tr, tf);
        san = piece.toUpperCase() + dis + cap + toSq;
      }

      // Check / checkmate symbol
      const newPos = applyMove(pos, uci);
      if (inCheck(newPos.board, newPos.turn)) {
        // Detect mate: does the checked side have any legal escape?
        san += isMate(newPos) ? '#' : '+';
      }

      return san;
    } catch (_) {
      return uci;
    }
  }

  /**
   * Very rough mate check: the side to move has no move that escapes check.
   * Pseudo-legal only (ignores pins on non-king pieces), but good enough for #.
   */
  function isMate(pos) {
    const side = pos.turn;
    const board = pos.board;
    const isWhite = side === 'w';

    // Try all moves for all pieces of `side` and see if any leaves them not in check
    for (let fr = 0; fr < 8; fr++) {
      for (let ff = 0; ff < 8; ff++) {
        const piece = board[fr][ff];
        if (piece === '.') continue;
        if ((isWhite ? piece.toUpperCase() : piece.toLowerCase()) !== piece) continue;

        // Generate pseudo-legal destinations
        const dests = destinations(board, piece, fr, ff, pos);
        for (const [tr, tf] of dests) {
          const uci = 'abcdefgh'[ff] + (fr + 1) + 'abcdefgh'[tf] + (tr + 1);
          const after = applyMove(pos, uci);
          if (!inCheck(after.board, side)) return false; // found escape
        }
      }
    }
    return true; // no escape → mate
  }

  function destinations(board, piece, fr, ff, pos) {
    const t = piece.toLowerCase();
    const dests = [];

    if (t === 'p') {
      const isWhite = piece === 'P';
      const dir = isWhite ? 1 : -1;
      const startRank = isWhite ? 1 : 6;
      // Forward
      if (board[fr + dir]?.[ff] === '.') {
        dests.push([fr + dir, ff]);
        if (fr === startRank && board[fr + 2 * dir]?.[ff] === '.') dests.push([fr + 2 * dir, ff]);
      }
      // Captures
      for (const df of [-1, 1]) {
        const nr = fr + dir, nf = ff + df;
        if (nf >= 0 && nf < 8 && nr >= 0 && nr < 8) {
          const target = board[nr][nf];
          const epTarget = 'abcdefgh'[nf] + (nr + 1);
          if ((target !== '.' && target !== target[isWhite ? 'toUpperCase' : 'toLowerCase']()) ||
              (pos.ep !== '-' && pos.ep === epTarget)) {
            dests.push([nr, nf]);
          }
        }
      }
      return dests;
    }

    if (t === 'n') {
      for (const [dr, df] of [[-2,-1],[-2,1],[-1,-2],[-1,2],[1,-2],[1,2],[2,-1],[2,1]]) {
        const nr = fr + dr, nf = ff + df;
        if (nr >= 0 && nr < 8 && nf >= 0 && nf < 8) {
          const tgt = board[nr][nf];
          if (tgt === '.' || (piece === 'N' ? tgt === tgt.toLowerCase() : tgt === tgt.toUpperCase())) {
            dests.push([nr, nf]);
          }
        }
      }
      return dests;
    }

    if (t === 'k') {
      for (const [dr, df] of [[-1,-1],[-1,0],[-1,1],[0,-1],[0,1],[1,-1],[1,0],[1,1]]) {
        const nr = fr + dr, nf = ff + df;
        if (nr >= 0 && nr < 8 && nf >= 0 && nf < 8) {
          const tgt = board[nr][nf];
          if (tgt === '.' || (piece === 'K' ? tgt === tgt.toLowerCase() : tgt === tgt.toUpperCase())) {
            dests.push([nr, nf]);
          }
        }
      }
      return dests;
    }

    // Sliding
    const rays = [];
    if (t === 'b' || t === 'q') rays.push([-1,-1],[-1,1],[1,-1],[1,1]);
    if (t === 'r' || t === 'q') rays.push([-1,0],[1,0],[0,-1],[0,1]);
    const isMyPiece = piece === piece.toUpperCase()
      ? (p) => p !== '.' && p === p.toUpperCase()
      : (p) => p !== '.' && p === p.toLowerCase();

    for (const [dr, df] of rays) {
      let r = fr + dr, f = ff + df;
      while (r >= 0 && r < 8 && f >= 0 && f < 8) {
        const tgt = board[r][f];
        if (tgt !== '.') {
          if (!isMyPiece(tgt)) dests.push([r, f]);
          break;
        }
        dests.push([r, f]);
        r += dr; f += df;
      }
    }
    return dests;
  }

  /* ── Batch converter ────────────────────────────────────────────────── */

  /**
   * Walk the whole game from startFen, converting each UCI move to SAN.
   * Returns an array of SAN strings parallel to uciMoves.
   */
  function buildSanList(startFen, uciMoves) {
    let pos = parseFen(startFen || START_FEN);
    const sanList = [];
    for (const uci of uciMoves) {
      if (!uci) { sanList.push(''); continue; }
      // Rebuild a FEN string from pos for uciToSan
      const fen = posToFen(pos);
      sanList.push(uciToSan(fen, uci));
      pos = applyMove(pos, uci);
    }
    return sanList;
  }

  function posToFen(pos) {
    let s = '';
    for (let r = 7; r >= 0; r--) {
      let empty = 0;
      for (let f = 0; f < 8; f++) {
        const p = pos.board[r][f];
        if (p === '.') { empty++; }
        else { if (empty) { s += empty; empty = 0; } s += p; }
      }
      if (empty) s += empty;
      if (r > 0) s += '/';
    }
    return `${s} ${pos.turn} ${pos.castling} ${pos.ep} ${pos.halfmove} ${pos.fullmove}`;
  }

  return { uciToSan, buildSanList, parseFen, applyMove, posToFen };
})();
