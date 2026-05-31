'use strict';

/**
 * SelfPlayAdapter â€” ServiceAdapter implementation for local self-play.
 *
 * Registers several virtual Stockfish opponents at different strength levels
 * that the auto-challenger and manual-challenge endpoints can target exactly
 * like real Lichess bots.  The user selects a subset via the dashboard;
 * the challenger cycles through only the selected ones.
 *
 * Preset opponents (9 virtual bots, each with 5 odds levels):
 *   SF                — standard chess, odds levels: equal / pawn / knight / rook / queen
 *   SF-Knights        — back rank all knights + pawns,   5 symmetric removal levels
 *   SF-Bishops        — back rank all bishops + pawns,   5 removal levels
 *   SF-Rooks          — back rank all rooks   + pawns,   5 removal levels
 *   SF-Queens         — back rank all queens  + pawns,   5 removal levels
 *   SF-KnightsNP      — two ranks of knights (no pawns), 5 removal levels
 *   SF-BishopsNP      — two ranks of bishops (no pawns), 5 removal levels
 *   SF-RooksNP        — two ranks of rooks   (no pawns), 5 removal levels
 *   SF-QueensNP       — two ranks of queens  (no pawns), 5 removal levels
 *   Redux-HCE         — bot-vs-bot self-play (standard chess, equal terms)
 *
 * Each preset exposes oddsLevels: { sfWhite[5], sfBlack[5], labels[5] }.
 * sfWhite[n] is used when SF plays white; sfBlack[n] when SF plays black.
 * Level 0 = equal position; higher levels remove pieces from SF's side.
 *
 * .env keys:
 *   SELFPLAY_ENABLED=true          activates this service (required)
 *   SELFPLAY_ENGINE=<abs-path>     path to Stockfish binary (auto-detected if omitted)
 *   SELFPLAY_SF_THREADS=4          threads for SF-Max (default: 4)
 *   SELFPLAY_MOVETIME=2000         ms/move for SF-Max (default: 2000)
 *   SELFPLAY_BOT_ENGINE=<abs-path> path to redux-hce.exe for bot-vs-bot (auto-detected)
 *   SELFPLAY_BOT_THREADS=4         threads for bot-vs-bot opponent (default: 4)
 *   SELFPLAY_BOT_HASH=128          hash MB for bot-vs-bot opponent (default: 128)
 */

const path = require('path');
const fs   = require('fs');
const ServiceAdapter               = require('./ServiceAdapter');
const positionStore                = require('../positionStore');
const { Engine }                   = require('../engine');
const { applyMoves, parseFen, STARTING_FEN, posInCheck, terminalStatus } = require('../fen');
const store    = require('../store');
const policies = require('../policies');
const dashState = require('../dashState');

// â”€â”€ Async push queue â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class AsyncQueue {
  constructor() {
    this._buf     = [];
    this._waiters = [];
    this._closed  = false;
  }
  push(item) {
    if (this._waiters.length) this._waiters.shift()(item);
    else                      this._buf.push(item);
  }
  end() {
    this._closed = true;
    for (const w of this._waiters) w(null);
    this._waiters = [];
  }
  async *[Symbol.asyncIterator]() {
    while (true) {
      if (this._buf.length) { yield this._buf.shift(); continue; }
      if (this._closed)       return;
      const item = await new Promise(r => this._waiters.push(r));
      if (item === null) return;
      yield item;
    }
  }
}

// â”€â”€ Speed helper â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

// Mirrors board.cpp is_draw() insufficient-material cases.
// Returns true when the position cannot produce checkmate by any sequence of
// legal moves: K vs K, K+N vs K, K+B vs K (any colour combination).
function isInsufficientMaterial(fen) {
  try {
    const pieces = fen.split(' ')[0].replace(/[^a-zA-Z]/g, '').replace(/[Kk]/g, '');
    if (pieces.length === 0) return true;                      // K vs K
    if (pieces.length === 1 && /^[BbNn]$/.test(pieces)) return true; // K+minor vs K
  } catch (_) {}
  return false;
}

function classifySpeed(timeLimitSec, incSec = 0) {
  if (!timeLimitSec) return 'rapid';
  const est = timeLimitSec + incSec * 40;
  if (est <  180) return 'bullet';   // < 3 min  (Lichess bullet)
  if (est <  480) return 'blitz';    // < 8 min  (Lichess blitz)
  if (est < 1800) return 'rapid';    // < 30 min (Lichess rapid)
  return 'classical';
}

/** Return current FEN given starting FEN + move list. */
function currentFen(startFen, moves) {
  try { return applyMoves(startFen, moves); } catch (_) { return startFen; }
}

// ── Preset definitions ─────────────────────────────────────────────────────────────────────────

// ── Engine match registry ──────────────────────────────────────────────────
// Scan archives/ for all build-NN directories, sorted ascending by number.
function _findAllBuildDirs(root) {
  try {
    const archivesDir = path.join(root, 'archives');
    const entries = fs.readdirSync(archivesDir).filter(e => /^build-\d+$/.test(e));
    entries.sort((a, b) => parseInt(a.replace('build-', ''), 10) - parseInt(b.replace('build-', ''), 10));
    return entries.map(e => ({ num: parseInt(e.replace('build-', ''), 10), dir: path.join(archivesDir, e) }));
  } catch (_) { return []; }
}

/**
 * Build the engine registry: all engines the match orchestrator can use.
 * Returns an array of engine descriptor objects.
 */
function _buildEngineRegistry(root) {
  const allBuilds = _findAllBuildDirs(root);
  const latestBuild = allBuilds.length ? allBuilds[allBuilds.length - 1] : null;

  const botThreads = parseInt(process.env.SELFPLAY_BOT_THREADS ?? '4', 10);
  const botHash    = parseInt(process.env.SELFPLAY_BOT_HASH    ?? '128', 10);

  const engines = [
    {
      id:             'sf',
      name:           'Stockfish 17',
      badge:          'SF',
      badgeClass:     'sp-badge-sf',
      path:           path.join(root, 'engines', 'stockfish-17.1', 'stockfish', 'stockfish-windows-x86-64-avx2.exe'),
      evalFile:       null,
      threads:        parseInt(process.env.SELFPLAY_SF_THREADS ?? '4', 10),
      hash:           128,
      extraOptions:   [],
      canPonder:      false,
      evalType:       'nnue',
    },
    {
      id:             'redux-hce',
      name:           'Redux HCE (latest)',
      badge:          'HCE',
      badgeClass:     'sp-badge-hce',
      path:           latestBuild ? path.join(latestBuild.dir, 'redux-hce.exe') : null,
      evalFile:       null,
      threads:        botThreads,
      hash:           botHash,
      extraOptions:   [],
      canPonder:      true,
      evalType:       'hce',
    },
    {
      id:             'redux-nnue',
      name:           'Redux NNUE (latest)',
      badge:          'NNUE',
      badgeClass:     'sp-badge-nnue',
      path:           latestBuild ? path.join(latestBuild.dir, 'redux-nnue.exe') : null,
      evalFile:       null,
      threads:        botThreads,
      hash:           botHash,
      extraOptions:   [],
      canPonder:      true,
      evalType:       'nnue',
    },
    {
      id:             'berserk',
      name:           'Berserk 13',
      badge:          'BK',
      badgeClass:     'sp-badge-berserk',
      path:           path.join(root, 'engines', 'berserk', 'berserk.exe'),
      evalFile:       path.join(root, 'engines', 'berserk', 'berserk-d43206fe90e4.nn'),
      threads:        2,
      hash:           64,
      extraOptions:   [],
      canPonder:      false,
      evalType:       'nnue',
    },
    {
      id:             'obsidian',
      name:           'Obsidian 16',
      badge:          'OB',
      badgeClass:     'sp-badge-obsidian',
      path:           path.join(root, 'engines', 'obsidian', 'obsidian.exe'),
      evalFile:       null,
      threads:        2,
      hash:           64,
      extraOptions:   [],
      canPonder:      false,
      evalType:       'nnue',
    },
    {
      id:             'stormphrax',
      name:           'Stormphrax 7',
      badge:          'SP',
      badgeClass:     'sp-badge-stormphrax',
      path:           path.join(root, 'engines', 'stormphrax', 'stormphrax.exe'),
      evalFile:       null,
      threads:        2,
      hash:           64,
      extraOptions:   [['EnableWeirdTCs', 'true']],
      canPonder:      false,
      evalType:       'nnue',
    },
    {
      id:             'clover',
      name:           'Clover 9.1',
      badge:          'CV',
      badgeClass:     'sp-badge-clover',
      path:           path.join(root, 'engines', 'clover', 'clover.exe'),
      evalFile:       null,
      threads:        2,
      hash:           64,
      extraOptions:   [],
      canPonder:      false,
      evalType:       'nnue',
    },
  ];

  // Add every archived build as a selectable engine (skips builds with no hce exe)
  for (const { num, dir } of allBuilds) {
    const hcePath = path.join(dir, 'redux-hce.exe');
    if (!fs.existsSync(hcePath)) continue;
    engines.push({
      id:           `redux-hce-build-${num}`,
      name:         `Redux HCE #${num}`,
      badge:        `#${num}`,
      badgeClass:   'sp-badge-hce',
      path:         hcePath,
      evalFile:     null,
      threads:      botThreads,
      hash:         botHash,
      extraOptions: [],
      canPonder:    true,
      evalType:     'hce',
      archiveBuild: num,
    });
  }

  // Annotate availability (binary exists on disk)
  for (const e of engines) {
    e.available = e.path ? (() => { try { return fs.existsSync(e.path); } catch (_) { return false; } })() : false;
  }

  return engines;
}

// ── Position pools (now loaded from positionStore / data/positions.json) ────
// The hardcoded FEN arrays below are kept only as reference; the live data
// is read from positionStore at runtime so the dashboard can edit them.

/* legacy reference — not used at runtime
const OPENINGS_FENS = [
  'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1',         // 1.e4
  'rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq - 0 1',         // 1.d4
  'rnbqkbnr/pppppppp/8/8/2P5/8/PP1PPPPP/RNBQKBNR b KQkq - 0 1',         // 1.c4
  'rnbqkbnr/pppppppp/8/8/5P2/8/PPPPP1PP/RNBQKBNR b KQkq - 0 1',         // 1.f4 (Bird)
  'rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2',       // 1.e4 e5
  'rnbqkbnr/pppp1ppp/8/4p3/3PP3/8/PPP2PPP/RNBQKBNR b KQkq - 0 2',       // 1.e4 e5 2.d4 (Centre Game)
  'r1bqkbnr/pppp1ppp/2n5/4p3/4PP2/8/PPPP2PP/RNBQKBNR b KQkq - 0 3',    // 1.e4 e5 2.f4 Nc6 (KGA)
  'rnbqkb1r/pppp1ppp/5n2/4p3/2B1P3/8/PPPP1PPP/RNBQK1NR b KQkq - 2 3',  // Italian Game
  'r1bqkb1r/pppp1ppp/2n2n2/4p3/4PP2/2N5/PPPP2PP/R1BQKBNR b KQkq - 0 4', // Four Knights
  'rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2',       // 1.e4 d5 (Scandinavian)
  'rnbqkbnr/pp1ppppp/2p5/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2',       // 1.e4 c6 (Caro-Kann)
  'rnbqkbnr/pp2pppp/3p4/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 3',      // Sicilian Dragon (early)
  'rnbqkb1r/pp2pppp/3p1n2/2p5/3PP3/2N5/PPP2PPP/R1BQKBNR w KQkq - 0 4', // Sicilian Scheveningen
  'rnbqkbnr/pp3ppp/4p3/2pp4/3PP3/2N5/PPP2PPP/R1BQKBNR w KQkq - 0 4',   // French Defence
  'rnbqkbnr/pppp1ppp/8/4p3/3P4/8/PPP1PPPP/RNBQKBNR w KQkq - 0 2',      // 1.d4 e5 (Budapest?)
  'rnbqkb1r/ppp1pppp/5n2/3p4/3P4/4P3/PPP2PPP/RNBQKBNR w KQkq - 1 3',   // QGD
  'rnbqkbnr/ppp1pppp/8/3p4/2PP4/8/PP2PPPP/RNBQKBNR b KQkq - 0 2',       // QGA
  'rnbqkb1r/ppp1pppp/5n2/3p4/2PP4/2N5/PP2PPPP/R1BQKBNR b KQkq - 0 3',  // Nimzo-Indian
  'rnbqkb1r/pppp1ppp/5n2/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3',  // Ruy Lopez (pre-Bb5)
  'r1bqkb1r/pppp1ppp/2n2n2/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 4 4', // Spanish Four Knights
];

// ── Vanity position pool FENs ───────────────────────────────────────────────

// Piece-rich complex middlegame positions — lots of material, sharp structures.
const CHAOS_FENS = [
  'r1bq1rk1/pp2ppbp/2np1np1/3p4/2PP4/2N2NP1/PP2PPBP/R1BQ1RK1 w - - 0 8',   // KID tabiya
  'r1bqr1k1/ppp2ppp/2np1n2/4p3/2B1P3/2NP1N2/PPP2PPP/R1BQR1K1 w - - 2 8',   // Italian complex
  'r2q1rk1/pp1nbppp/2n1p3/3pP3/3P4/2PB1N2/PP2QPPP/R1B2RK1 w - - 0 10',    // Nimzo middlegame
  'r1bq1rk1/2p1bppp/p1np1n2/1p2p3/4PP2/1BNP1N2/PPP3PP/R1BQ1RK1 w - - 0 9', // Ruy Lopez complex
  'r1bqkb1r/pp3ppp/2n1pn2/3p4/2PP4/5NP1/PP2PP1P/RNBQKB1R w KQkq - 0 7',   // Grünfeld tabiya
  'r2q1rk1/ppp1bppp/2np1n2/4p3/2B1P3/P1NP1N2/1PP2PPP/R1BQR1K1 w - - 0 9', // English complex
];

// All-knight back ranks (symmetric) — pure knight chaos with pawns.
const KNIGHT_FENS = [
  'nnnnknnn/pppppppp/8/8/8/8/PPPPPPPP/NNNNKNNN w - - 0 1',
  'nnnnknnn/pppp1ppp/4p3/8/4P3/8/PPPP1PPP/NNNNKNNN w - - 0 3',
  'nnnnknnn/pp1p1ppp/2p1p3/8/3PP3/8/PP3PPP/NNNNKNNN w - - 0 4',
];

// Standard material with all pawns removed — pure piece play.
const PAWNLESS_FENS = [
  'rnbqkbnr/8/8/8/8/8/8/RNBQKBNR w KQkq - 0 1',
  'r1bqk1nr/8/8/8/8/8/8/R1BQK1NR w KQkq - 0 1',
  'r1bqkb1r/8/8/8/8/8/8/R1BQKB1R w KQkq - 0 1',
  'rn1qk2r/8/8/8/8/8/8/RN1QK2R w KQkq - 0 1',
];

// Simplified endgame starting positions — forces engines to show technique.
const ENDGAME_FENS = [
  '3r4/3k4/3p4/8/8/3P4/3K4/3R4 w - - 0 1',     // K+R+P symmetric
  '1r6/1k6/2p5/8/8/2P5/1K6/1R6 w - - 0 1',     // K+R+P offset
  '8/3k1ppp/8/8/8/8/PPP1K3/8 w - - 0 1',        // K+3P symmetric (staggered)
  '8/p2kp3/3p4/8/8/3P4/P2KP3/8 w - - 0 1',     // K+3P diagonal
  '8/8/4k3/5r2/5R2/4K3/8/8 w - - 0 1',         // Pure rook ending
  '8/5k2/8/5q2/5Q2/8/5K2/8 w - - 0 1',         // Pure queen ending
];

// All-rook back ranks (symmetric) — hyper-aggressive rook positions.
const ROOK_FENS = [
  'rrrrkrrr/pppppppp/8/8/8/8/PPPPPPPP/RRRRKRRR w - - 0 1',
  'rrrrkrrr/pppp1ppp/4p3/8/4P3/8/PPPP1PPP/RRRRKRRR w - - 0 3',
  'rrrrkrrr/ppp2ppp/3pp3/8/3PP3/8/PPP2PPP/RRRRKRRR w - - 0 4',
];
*/

function _buildPositionPools() { return []; } // stub — replaced by positionStore

function _buildPositionPools() {
  return [
    {
      id:   'startpos',
      name: 'Start Position',
      fens: [STARTING_FEN],
    },
    {
      id:   'openings',
      name: 'Opening Variety',
      fens: OPENINGS_FENS,
    },
    {
      id:   'chaos',
      name: 'Piece-Rich Chaos',
      fens: CHAOS_FENS,
    },
    {
      id:   'knights',
      name: 'Knight Fortress',
      fens: KNIGHT_FENS,
    },
    {
      id:   'pawnless',
      name: 'Pawnless',
      fens: PAWNLESS_FENS,
    },
    {
      id:   'endgames',
      name: 'Endgame Gauntlet',
      fens: ENDGAME_FENS,
    },
    {
      id:   'rooks',
      name: 'Rook City',
      fens: ROOK_FENS,
    },
  ];
}

// Each preset maps to a named virtual opponent.  movetime is ms/move for SF.
// ratings are rough approximations shown in the bot table; not used for matching.

// ── Standard chess odds levels ───────────────────────────────────────────────
// Level 0 = equal; levels 1-4 remove pieces from SF's side.
const STANDARD_ODDS = {
  sfWhite: [
    'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',         // L0 equal
    'rnbqkbnr/pppppppp/8/8/8/8/PPPPP1PP/RNBQKBNR w KQkq - 0 1',         // L1 pawn (f2)
    'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKB1R w KQkq - 0 1',         // L2 knight (g1)
    'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/1NBQKBNR w Kkq - 0 1',          // L3 rook (a1)
    'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNB1KBNR w KQkq - 0 1',         // L4 queen (d1)
  ],
  sfBlack: [
    'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',         // L0 equal
    'rnbqkbnr/ppppp1pp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',         // L1 pawn (f7)
    'rnbqkb1r/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',         // L2 knight (g8)
    '1nbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQk - 0 1',          // L3 rook (a8)
    'rnb1kbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',         // L4 queen (d8)
  ],
  labels: ['Equal', 'Pawn odds', 'Knight odds', 'Rook odds', 'Queen odds'],
};

// ── Themed odds FEN builder ───────────────────────────────────────────────────
//
// Generates 5 FEN pairs for a themed position (all-one-piece back rank).
//
//   piece   : 'N' | 'B' | 'R' | 'Q'
//   kingPos : 'e'  — king on e-file (knights/bishops, and rooks/queens WITH pawns)
//             'a'  — king on a-file (rooks/queens WITHOUT pawns)
//   hasPawns: true / false — determines rank-2 content
//
// Odds are applied to SF's back rank only (rank 1 when SF=white, rank 8 when
// SF=black).  The opponent's position is always the full Level-0 layout.
// Level 0 is the symmetric equal position; level 4 removes 4 pieces from SF.
function _themedOdds(piece, kingPos, hasPawns) {
  const P = piece.toUpperCase();
  const p = piece.toLowerCase();

  // Back-rank strings (uppercase = white side, lowercase = black side)
  let sfBackRanks;
  if (kingPos === 'e') {
    // King on e-file: PPPPKPPP pattern
    sfBackRanks = [
      `${P}${P}${P}${P}K${P}${P}${P}`,  // L0 equal
      `${P}${P}${P}${P}K${P}${P}1`,      // L1 −h
      `1${P}${P}${P}K${P}${P}1`,          // L2 −h,−a
      `1${P}${P}${P}K${P}2`,              // L3 −h,−a,−g
      `2${P}${P}K${P}2`,                  // L4 −h,−a,−g,−b
    ];
  } else {
    // King on a-file: KPPPPPPP pattern (rooks/queens no-pawns)
    sfBackRanks = [
      `K${P}${P}${P}${P}${P}${P}${P}`,  // L0 equal
      `K${P}${P}${P}${P}${P}${P}1`,      // L1 −h
      `K${P}${P}${P}${P}${P}2`,           // L2 −h,−g
      `K${P}${P}${P}${P}3`,               // L3 −h,−g,−f
      `K${P}${P}${P}4`,                   // L4 −h,−g,−f,−e
    ];
  }
  const sfBackRanksLc = sfBackRanks.map(r => r.toLowerCase()); // SF plays black

  // Fixed (non-SF) back rank is always the full L0 rank
  const oppoBackW = sfBackRanks[0];     // white full back rank (uppercase)
  const oppoBackB = sfBackRanksLc[0];   // black full back rank (lowercase)

  // Second-rank content
  const rank2W = hasPawns ? 'PPPPPPPP' : `${P}${P}${P}${P}${P}${P}${P}${P}`;
  const rank2B = hasPawns ? 'pppppppp' : `${p}${p}${p}${p}${p}${p}${p}${p}`;

  // sfWhite[n]: SF plays white → remove from white's back rank (bottom in FEN = rank 1)
  const sfWhite = sfBackRanks.map(sfRank =>
    `${oppoBackB}/${rank2B}/8/8/8/8/${rank2W}/${sfRank} w - - 0 1`
  );

  // sfBlack[n]: SF plays black → remove from black's back rank (top in FEN = rank 8)
  const sfBlack = sfBackRanksLc.map(sfRank =>
    `${sfRank}/${rank2B}/8/8/8/8/${rank2W}/${oppoBackW} w - - 0 1`
  );

  return { sfWhite, sfBlack };
}

const _buildPresets = () => {
  const maxThreads  = parseInt(process.env.SELFPLAY_SF_THREADS ?? '4',   10);
  const maxMovetime = parseInt(process.env.SELFPLAY_MOVETIME   ?? '2000', 10);
  // movetime is only used when no game clock is active.
  const TL = ['Equal', '\u22121 piece', '\u22122 pieces', '\u22123 pieces', '\u22124 pieces'];
  return [
    {
      username:   'SF',
      movetime:   maxMovetime, threads:   maxThreads,
      ratings:    { bullet: 3200, blitz: 3300, rapid: 3500 },
      oddsLevels: STANDARD_ODDS,
    },
    {
      username:   'SF-Knights',
      movetime:   maxMovetime, threads:   maxThreads,
      ratings:    { bullet: 3000, blitz: 3100, rapid: 3200 },
      oddsLevels: { ..._themedOdds('N', 'e', true),  labels: TL },
    },
    {
      username:   'SF-Bishops',
      movetime:   maxMovetime, threads:   maxThreads,
      ratings:    { bullet: 3000, blitz: 3100, rapid: 3200 },
      oddsLevels: { ..._themedOdds('B', 'e', true),  labels: TL },
    },
    {
      username:   'SF-Rooks',
      movetime:   maxMovetime, threads:   maxThreads,
      ratings:    { bullet: 3000, blitz: 3100, rapid: 3200 },
      oddsLevels: { ..._themedOdds('R', 'e', true),  labels: TL },
    },
    {
      username:   'SF-Queens',
      movetime:   maxMovetime, threads:   maxThreads,
      ratings:    { bullet: 3000, blitz: 3100, rapid: 3200 },
      oddsLevels: { ..._themedOdds('Q', 'e', true),  labels: TL },
    },
    {
      username:   'SF-KnightsNP',
      movetime:   maxMovetime, threads:   maxThreads,
      ratings:    { bullet: 3000, blitz: 3100, rapid: 3200 },
      oddsLevels: { ..._themedOdds('N', 'e', false), labels: TL },
    },
    {
      username:   'SF-BishopsNP',
      movetime:   maxMovetime, threads:   maxThreads,
      ratings:    { bullet: 3000, blitz: 3100, rapid: 3200 },
      oddsLevels: { ..._themedOdds('B', 'e', false), labels: TL },
    },
    {
      username:   'SF-RooksNP',
      movetime:   maxMovetime, threads:   maxThreads,
      ratings:    { bullet: 3000, blitz: 3100, rapid: 3200 },
      oddsLevels: { ..._themedOdds('R', 'e', false), labels: TL },
    },
    {
      username:   'SF-QueensNP',
      movetime:   maxMovetime, threads:   maxThreads,
      ratings:    { bullet: 3000, blitz: 3100, rapid: 3200 },
      oddsLevels: { ..._themedOdds('Q', 'e', false), labels: TL },
    },
    {
      username:   'Redux-HCE',
      isBotVsBot: true,
      movetime:   maxMovetime,
      threads:    parseInt(process.env.SELFPLAY_BOT_THREADS ?? '4',   10),
      hash:       parseInt(process.env.SELFPLAY_BOT_HASH   ?? '128', 10),
      ratings:    { bullet: 1500, blitz: 1500, rapid: 1500 },
      oddsLevels: STANDARD_ODDS,
    },
  ];
};

// â”€â”€ SelfPlayAdapter â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

// King-in-check detection is provided by fen.posInCheck — see fen.js.

class SelfPlayAdapter extends ServiceAdapter {
  constructor() {
    super();

    // Locate Stockfish: env override â†’ known project path
    const root = path.resolve(__dirname, '..', '..', '..'); // workspace root
    this._sfAbsPath = process.env.SELFPLAY_ENGINE
      ?? path.join(root, 'engines', 'stockfish-17.1', 'stockfish',
                   'stockfish-windows-x86-64-avx2.exe');

    // Locate Redux-HCE binary for bot-vs-bot self-play
    this._botAbsPath = process.env.SELFPLAY_BOT_ENGINE
      ?? path.join(root, 'bot', 'engine', 'redux-hce.exe');

    // Build preset list (reads env at construction time, after dotenv loaded)
    this._presets = _buildPresets();
    // Fast lookup: username â†’ preset config
    this._presetMap = new Map(this._presets.map(p => [p.username, p]));

    /** Global event stream â€” index.js iterates this via streamEvents() */
    this._eventQueue = new AsyncQueue();

    /** Active games: gameId â†’ game object */
    this._games = new Map();
    // ── Match orchestrator ──────────────────────────────────────────────
    this._root            = root;
    this._engineRegistry  = _buildEngineRegistry(root);
    /** Live Engine processes kept alive across games: engineId → Engine */
    this._engineProcesses = new Map();

    // Load persisted config
    const savedCfg = dashState.get('selfplayConfig');
    // Migrate legacy enabledPools → enabledPositions if needed
    let enabledPositions = savedCfg.enabledPositions ?? null;
    if (!enabledPositions && savedCfg.enabledPools?.length) {
      // Convert old pool IDs to new per-position IDs via positionStore
      const allCats = positionStore.getCategories();
      enabledPositions = allCats
        .filter(c => savedCfg.enabledPools.includes(c.id))
        .flatMap(c => c.positions.map(p => p.id));
    }
    if (!enabledPositions || !enabledPositions.length) {
      enabledPositions = ['startpos-0'];
    }
    this._matchState = {
      running:          false,
      mode:             savedCfg.mode           ?? 'loop',
      gameCount:        0,
      currentGame:      null,
      enabledEngines:   savedCfg.enabledEngines ?? [],
      enabledPositions,
      tc:               savedCfg.tc             ?? { initial: 180000, increment: 2000 },
      ponder:           savedCfg.ponder         ?? false,
      tmMode:           savedCfg.tmMode         ?? 'clock',
      movetimeMs:       savedCfg.movetimeMs     ?? 2000,
      standings:        {},
    };  }

  name()  { return 'Self-play'; }
  id()    { return 'selfplay'; }

  /** Return the raw preset config for a username, or null if unknown. */
  getPreset(username) { return this._presetMap.get(username) ?? null; }

  // â”€â”€ Auth / account â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

  async authenticate(_token) {
    return { ok: true, status: 200, username: 'self', isBot: true,
             data: { id: 'self', username: 'self', title: 'BOT' } };
  }

  async getAccount(_token) {
    return { ok: true, status: 200,
             data: { id: 'self', username: 'self', title: 'BOT' } };
  }

  profileUrl(_username) { return '#'; }

  normalizeAccount(_data) {
    return { username: 'self', ratings: {} };
  }

  // â”€â”€ Bot discovery â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

  async fetchOnlineBots(_nb, _token) {
    // Return raw objects shaped like the Lichess perfs format so normalizeBot works
    return this._presets.map(p => ({
      username: p.username,
      title:    null,
      perfs: {
        bullet: { rating: p.ratings.bullet },
        blitz:  { rating: p.ratings.blitz  },
        rapid:  { rating: p.ratings.rapid  },
      },
    }));
  }

  normalizeBot(raw) {
    const preset = this._presetMap.get(raw.username) ?? this._presets[0];
    return {
      username:   raw.username,
      service:    'selfplay',
      ratings:    { ...preset.ratings },
      profileUrl: '#',
    };
  }

  async getUser(username, _token) {
    const preset = this._presetMap.get(username);
    if (!preset) return { ok: false, status: 404, data: { error: 'unknown self-play bot' } };
    return { ok: true, status: 200, data: {
      id: username.toLowerCase(), username,
      perfs: {
        bullet: { rating: preset.ratings.bullet },
        blitz:  { rating: preset.ratings.blitz  },
        rapid:  { rating: preset.ratings.rapid  },
      },
    }};
  }

  // â”€â”€ Challenge â†’ create a game â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

  async challengeUser(username, _token, opts = {}) {
    const preset = this._presetMap.get(username);
    if (!preset) return { ok: false, status: 404, data: { error: 'unknown self-play bot' } };

    const gameId   = `sp_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
    const inputColor = opts.color;
    const botColor = inputColor && inputColor !== 'random'
      ? inputColor
      : (Math.random() < 0.5 ? 'white' : 'black');
    const sfColor  = botColor === 'white' ? 'black' : 'white';

    const timeLimitSec = opts.timeLimit  ?? null;
    const incSec       = opts.increment  ?? 0;
    const speed        = classifySpeed(timeLimitSec, incSec);

    // Compute starting FEN from the preset's odds level.
    // oddsLevel 0 = equal position; 1–4 = progressively more pieces removed from SF's side.
    const oddsLevel = Math.max(0, Math.min(4, opts.oddsLevel ?? 0));
    const startFen = preset.oddsLevels
      ? (sfColor === 'white'
          ? preset.oddsLevels.sfWhite[oddsLevel]
          : preset.oddsLevels.sfBlack[oddsLevel])
      : STARTING_FEN;

    const opponentPath = preset.isBotVsBot ? this._botAbsPath : this._sfAbsPath;
    const opponentHash = preset.isBotVsBot ? (preset.hash ?? 128) : 64;
    const sfEngine = new Engine(opponentPath, { threads: preset.threads, hash: opponentHash });
    await sfEngine.init();

    const tcMs  = timeLimitSec ? timeLimitSec * 1000 : null;
    const incMs = incSec * 1000;

    this._games.set(gameId, {
      moves:       [],
      queue:       new AsyncQueue(),
      sfEngine,
      preset,
      startFen,
      botColor,
      sfColor,
      speed,
      tc:          tcMs ? { wtime: tcMs, btime: tcMs, winc: incMs, binc: incMs } : null,
      sfThinking:  false,
      _turnStart:  Date.now(),
      _watchdogId: null,
      _posCount:   new Map(),   // normalized-FEN → occurrence count (threefold detection)
    });

    // Seed the initial position.
    try {
      const initKey = startFen.split(' ').slice(0, 4).join(' ');
      this._games.get(gameId)._posCount.set(initKey, 1);
    } catch (_) {}

    this._eventQueue.push({
      type: 'gameStart',
      game: {
        gameId,
        color:    botColor,
        opponent: { id: username.toLowerCase(), username, aiLevel: null },
        variant:  { key: opts.variant ?? 'standard' },
        speed,
        fen:      'startpos',
      },
    });

    return { ok: true, status: 200, data: { challenge: { id: gameId } } };
  }

  // â”€â”€ Event stream â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

  async *streamEvents(_token) {
    yield* this._eventQueue;
  }

  // â”€â”€ Game stream â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

  async *streamGame(gameId, _token) {
    const game = this._games.get(gameId);
    if (!game) return;

    const whiteName = game.botColor === 'white' ? 'Bot'                    : game.preset.username;
    const blackName = game.botColor === 'black' ? 'Bot'                    : game.preset.username;
    const whiteId   = game.botColor === 'white' ? 'self'                   : game.preset.username.toLowerCase();
    const blackId   = game.botColor === 'black' ? 'self'                   : game.preset.username.toLowerCase();

    yield {
      type:       'gameFull',
      id:         gameId,
      speed:      game.speed,
      variant:    { key: 'standard' },
      initialFen: game.startFen,
      // Mirror the real Lichess API: clock.initial (ms) and clock.increment (ms).
      // Without this field, game.js _onGameFull never sets _clock, leaving the
      // engine on the no-clock fallback profile (minTimeMs ≈ 225ms) regardless
      // of the actual time control.
      clock:      game.tc ? { initial: game.tc.wtime, increment: game.tc.winc } : null,
      white:      { id: whiteId, name: whiteName },
      black:      { id: blackId, name: blackName },
      state: {
        type:   'gameState',
        moves:  '',
        status: 'started',
        wtime:  game.tc?.wtime ?? 0,
        btime:  game.tc?.btime ?? 0,
        winc:   game.tc?.winc  ?? 0,
        binc:   game.tc?.binc  ?? 0,
      },
    };

    if (game.botColor === 'black') {
      this._sfThink(gameId).catch(e =>
        console.error(`[selfplay:${gameId}] SF initial move error:`, e.message)
      );
    }

    yield* game.queue;
  }

  // â”€â”€ Our bot submits a move â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

  async makeMove(gameId, move, _token, _opts) {
    const game = this._games.get(gameId);
    if (!game) return { ok: false, status: 404, data: { error: 'game not found' } };

    if (!move || move === '(none)') {
      // Distinguish checkmate (bot's king in check) from stalemate.
      let endStatus = 'stalemate', winner = null;
      try {
        const posFen = applyMoves(game.startFen, game.moves);
        if (posInCheck(posFen)) { endStatus = 'mate'; winner = game.sfColor; }
      } catch (_) { endStatus = 'mate'; winner = game.sfColor; }
      this._endGame(gameId, winner, endStatus);
      return { ok: true, status: 200, data: {} };
    }

    // Clear the per-turn watchdog as soon as we get a response from the bot.
    if (game._watchdogId != null) { clearTimeout(game._watchdogId); game._watchdogId = null; }

    if (game.tc && game._turnStart != null) {
      const elapsed  = Date.now() - game._turnStart;
      const timeKey  = game.botColor === 'white' ? 'wtime' : 'btime';
      const incKey   = game.botColor === 'white' ? 'winc'  : 'binc';
      game.tc[timeKey] = Math.max(0, game.tc[timeKey] - elapsed + game.tc[incKey]);
      game._turnStart = null;
      if (game.tc[timeKey] === 0) {
        this._endGame(gameId, game.sfColor, 'outoftime');
        return { ok: true, status: 200, data: {} };
      }
      // Immediately push the corrected clock to the dashboard so the display
      // doesn't snap back to the pre-deduction value once the FEN switches to
      // the opponent's turn (before the next gameState arrives from _sfThink).
      store.updateClock(gameId, game.tc.wtime, game.tc.btime);
    }

    game.moves.push(move);

    // Track this position for threefold repetition.
    try {
      const posFen = applyMoves(game.startFen, game.moves);
      const key    = posFen.split(' ').slice(0, 4).join(' ');
      const count  = (game._posCount.get(key) ?? 0) + 1;
      game._posCount.set(key, count);
      if (count >= 3) {
        this._endGame(gameId, null, 'repetition');
        return { ok: true, status: 200, data: {} };
      }
      if (isInsufficientMaterial(posFen)) {
        this._endGame(gameId, null, 'insufficientMaterial');
        return { ok: true, status: 200, data: {} };
      }
    } catch (_) {}

    this._sfThink(gameId).catch(e =>
      console.error(`[selfplay:${gameId}] SF think error:`, e.message)
    );

    return { ok: true, status: 200, data: {} };
  }

  // â”€â”€ Stockfish thinks and pushes the next gameState â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

  async _sfThink(gameId) {
    const game = this._games.get(gameId);
    if (!game || game.sfThinking) return;
    game.sfThinking = true;

    const sfMoveStart = Date.now();

    // Use the same time-management formula as the main bot (policies.getSearchProfile)
    // so both sides of a self-play game operate on equal terms.
    let maxTimeMs;
    let movetime = null;
    if (game.tc) {
      const opponentTimeMs = game.sfColor === 'white' ? game.tc.btime : game.tc.wtime;
      const profile = policies.getSearchProfile(
        game.speed, game.tc, game.sfColor, game.moves.length, {}, opponentTimeMs,
      );
      maxTimeMs = profile.maxTimeMs;
      movetime  = profile.movetime ?? null;  // non-null = emergency mode
    } else {
      maxTimeMs = game.preset.movetime;
    }

    let result;
    try {
      result = await game.sfEngine.thinkDynamic(game.startFen, game.moves.slice(), {
        maxTimeMs,
        movetime,
        onInfo: () => ({}),
      });
    } catch (err) {
      // "Engine returned no move" means SF has no legal moves: checkmate or stalemate.
      // Determine which by checking whether the side to move's king is in check.
      const g2 = this._games.get(gameId);
      if (!g2) return;
      g2.sfThinking = false;
      let endStatus = 'stalemate';
      let winner    = null;
      try {
        const posFen    = applyMoves(g2.startFen, g2.moves);
        const inCheck   = posInCheck(posFen);
        if (inCheck) { endStatus = 'mate'; winner = g2.botColor; }
        else         { endStatus = 'stalemate'; winner = null; }
      } catch (_) {
        // If FEN parsing fails, assume checkmate (most likely after M1 line)
        endStatus = 'mate'; winner = g2.botColor;
      }
      console.log(`[selfplay:${gameId}] SF has no legal moves → ${endStatus}`);
      this._endGame(gameId, winner, endStatus);
      return;
    }

    const g = this._games.get(gameId);
    if (!g) return;
    g.sfThinking = false;

    // Retroactively stamp SF's eval of the position the bot just created.
    // This _sfThink() call searched that position, so result.eval_cp is correct.
    store.updateLastMoveSfEval(gameId, result.eval_cp ?? null, result.depth ?? null);

    if (g.tc) {
      const elapsed   = Date.now() - sfMoveStart;
      const sfTimeKey = g.sfColor === 'white' ? 'wtime' : 'btime';
      const sfIncKey  = g.sfColor === 'white' ? 'winc'  : 'binc';
      g.tc[sfTimeKey] = Math.max(0, g.tc[sfTimeKey] - elapsed + g.tc[sfIncKey]);
      if (g.tc[sfTimeKey] === 0) {
        this._endGame(gameId, g.botColor, 'outoftime');
        return;
      }
    }

    if (!result.move || result.move === '(none)') {
      // Determine checkmate vs stalemate
      let endStatus = 'mate', winner = g.botColor;
      try {
        const posFen = applyMoves(g.startFen, g.moves);
        if (!posInCheck(posFen)) { endStatus = 'stalemate'; winner = null; }
      } catch (_) {}
      this._endGame(gameId, winner, endStatus);
      return;
    }

    g.moves.push(result.move);

    // Threefold repetition check.
    try {
      const posFen = applyMoves(g.startFen, g.moves);
      const key    = posFen.split(' ').slice(0, 4).join(' ');
      const count  = (g._posCount.get(key) ?? 0) + 1;
      g._posCount.set(key, count);
      if (count >= 3) {
        this._endGame(gameId, null, 'repetition');
        return;
      }
    } catch (_) {}

    try {
      const posFen   = applyMoves(g.startFen, g.moves);
      const halfmove = parseInt(posFen.split(' ')[4] ?? '0', 10);
      if (halfmove >= 100) {
        this._endGame(gameId, null, 'draw');
        return;
      }
      if (isInsufficientMaterial(posFen)) {
        this._endGame(gameId, null, 'insufficientMaterial');
        return;
      }
    } catch (_) {}

    g._turnStart = Date.now();

    // Per-turn watchdog: if the bot doesn't respond within 3× its budget (min 30s),
    // something has gone wrong on the bot side.  Force a draw so the game loop can
    // advance to the next game rather than hanging indefinitely.
    if (g._watchdogId != null) clearTimeout(g._watchdogId);
    const watchdogMs = Math.max(maxTimeMs ? maxTimeMs * 3 : 30_000, 30_000);
    g._watchdogId = setTimeout(() => {
      const still = this._games.get(gameId);
      if (!still) return;
      console.warn(`[selfplay:${gameId}] bot watchdog fired after ${watchdogMs}ms — ending game`);
      this._endGame(gameId, null, 'draw');
    }, watchdogMs);

    g.queue.push({
      type:   'gameState',
      moves:  g.moves.join(' '),
      status: 'started',
      wtime:  g.tc?.wtime ?? 0,
      btime:  g.tc?.btime ?? 0,
      winc:   g.tc?.winc  ?? 0,
      binc:   g.tc?.binc  ?? 0,
    });
  }

  // â”€â”€ End a game â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

  _endGame(gameId, winner, status) {
    const game = this._games.get(gameId);
    if (!game) return;
    // Clear any pending watchdog so it doesn't fire on an already-ended game.
    if (game._watchdogId != null) { clearTimeout(game._watchdogId); game._watchdogId = null; }
    console.log(`[selfplay:${gameId}] game over â€” status=${status} winner=${winner ?? 'draw'}`);
    game.queue.push({
      type:   'gameState',
      moves:  game.moves.join(' '),
      status,
      winner: winner ?? null,
      wtime:  game.tc?.wtime ?? 0,
      btime:  game.tc?.btime ?? 0,
      winc:   0,
      binc:   0,
    });
    game.queue.end();
    game.sfEngine.quit().catch(() => {});
    this._games.delete(gameId);
  }

  // ── Match orchestrator ────────────────────────────────────────────────────

  /** Return a deep copy of the engine registry with availability annotated. */
  getEngineRegistry() {
    return this._engineRegistry.map(e => ({ ...e }));
  }

  /** Return all position categories (full data — categories + positions). */
  getPositionCategories() {
    return positionStore.getCategories();
  }

  /** Return current match state (running, standings, config). */
  getMatchState() {
    return {
      running:          this._matchState.running,
      mode:             this._matchState.mode,
      gameCount:        this._matchState.gameCount,
      currentGame:      this._matchState.currentGame
        ? { ...this._matchState.currentGame }
        : null,
      enabledEngines:   this._matchState.enabledEngines.slice(),
      enabledPositions: this._matchState.enabledPositions.slice(),
      tc:               { ...this._matchState.tc },
      ponder:           this._matchState.ponder,
      tmMode:           this._matchState.tmMode,
      movetimeMs:       this._matchState.movetimeMs,
      standings:        JSON.parse(JSON.stringify(this._matchState.standings)),
    };
  }

  /** Merge config changes into matchState and persist. */
  configureMatch(cfg) {
    if (cfg.mode             !== undefined) this._matchState.mode             = cfg.mode;
    if (cfg.enabledEngines   !== undefined) this._matchState.enabledEngines   = cfg.enabledEngines;
    if (cfg.enabledPositions !== undefined) this._matchState.enabledPositions = cfg.enabledPositions;
    if (cfg.tc               !== undefined) this._matchState.tc               = { ...this._matchState.tc, ...cfg.tc };
    if (cfg.ponder           !== undefined) this._matchState.ponder           = cfg.ponder;
    if (cfg.tmMode           !== undefined) this._matchState.tmMode           = cfg.tmMode;
    if (cfg.movetimeMs       !== undefined) this._matchState.movetimeMs       = cfg.movetimeMs;
    dashState.save('selfplayConfig', {
      mode:             this._matchState.mode,
      enabledEngines:   this._matchState.enabledEngines,
      enabledPositions: this._matchState.enabledPositions,
      tc:               this._matchState.tc,
      ponder:           this._matchState.ponder,
      tmMode:           this._matchState.tmMode,
      movetimeMs:       this._matchState.movetimeMs,
    });
  }

  /** Start the match loop (if not already running). */
  async startMatch() {
    if (this._matchState.running) return { ok: false, error: 'already running' };
    const engines = this._matchState.enabledEngines;
    if (engines.length < 2) return { ok: false, error: 'select at least 2 engines' };

    this._matchState.running   = true;
    this._matchState.standings = {};
    this._matchState.gameCount = 0;

    // Initialise standings entries
    for (const id of engines) this._matchState.standings[id] = { w: 0, d: 0, l: 0, pts: 0 };

    store.emit('sp_state', { matchState: this.getMatchState() });

    // Launch detached loop
    this._matchLoop().catch(err => {
      console.error('[selfplay-match] fatal loop error:', err.message);
      this._matchState.running = false;
      store.emit('sp_state', { matchState: this.getMatchState() });
    });

    return { ok: true };
  }

  /** Stop the match loop after the current game finishes. */
  async stopMatch() {
    this._matchState.running = false;
    // Cancel any ponders on live engines
    for (const eng of this._engineProcesses.values()) {
      try { if (eng.isPondering()) await eng.cancelPonder(); } catch (_) {}
    }
    store.emit('sp_state', { matchState: this.getMatchState() });
    return { ok: true };
  }

  /** Initialise (or retrieve) a match engine process. Engines stay alive across games. */
  async _initMatchEngine(reg) {
    if (this._engineProcesses.has(reg.id)) {
      const existing = this._engineProcesses.get(reg.id);
      if (existing.isReady()) return existing;
      // Process crashed — remove and reinit below
      this._engineProcesses.delete(reg.id);
    }
    const eng = new Engine(reg.path, {
      threads:  reg.threads,
      hash:     reg.hash,
      evalFile: reg.evalFile ?? null,
    });
    await eng.init();
    if (reg.extraOptions?.length) eng.sendOptions(reg.extraOptions);
    this._engineProcesses.set(reg.id, eng);
    return eng;
  }

  /** Internal: pick a random FEN from the enabled position IDs. */
  _pickFen() {
    const enabled = this._matchState.enabledPositions ?? [];
    if (!enabled.length) return STARTING_FEN;
    const posMap = positionStore.getAllPositionMap();
    const fens = enabled.map(id => posMap.get(id)).filter(Boolean);
    if (!fens.length) return STARTING_FEN;
    return fens[Math.floor(Math.random() * fens.length)];
  }

  /**
   * Run one engine-vs-engine game.
   * Emits sp_game_start, sp_move, sp_info, sp_game_end via store.
   * @param {string} whiteId  engine registry id for white
   * @param {string} blackId  engine registry id for black
   * @param {string} startFen starting position
   * @param {string} [primaryId]  which engine is "ours" for DB attribution (defaults to whiteId)
   * @returns {Promise<'white'|'black'|'draw'>}
   */
  async _runGame(whiteId, blackId, startFen, primaryId = null) {
    const gameId   = `smatch_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`;
    const whiteReg = this._engineRegistry.find(e => e.id === whiteId);
    const blackReg = this._engineRegistry.find(e => e.id === blackId);
    const tc       = this._matchState.tc ?? {};
    const initial  = tc.initial  ?? 60_000;
    const inc      = tc.increment ?? 2_000;
    const useClock = this._matchState.tmMode !== 'movetime';
    let wTimeMs    = initial;
    let bTimeMs    = initial;

    let whiteEng, blackEng;
    try {
      [whiteEng, blackEng] = await Promise.all([
        this._initMatchEngine(whiteReg),
        this._initMatchEngine(blackReg),
      ]);
    } catch (err) {
      console.error('[selfplay-match] engine init failed:', err.message);
      return 'draw';
    }

    const gameCount  = this._matchState.gameCount + 1;
    const whiteName  = whiteReg?.name ?? whiteId;
    const blackName  = blackReg?.name ?? blackId;

    // Determine which engine is "ours" for DB attribution so that when colors
    // are flipped across games the same engine is always recorded as the subject.
    // primaryId defaults to whiteId (preserving old behaviour if not passed).
    const effectivePrimary = primaryId ?? whiteId;
    const ourIsWhite = (effectivePrimary === whiteId);
    const ourReg     = ourIsWhite ? whiteReg : blackReg;
    const oppReg     = ourIsWhite ? blackReg : whiteReg;
    const ourName    = ourReg?.name  ?? (ourIsWhite ? whiteId : blackId);
    const oppName    = oppReg?.name  ?? (ourIsWhite ? blackId : whiteId);
    const oppId      = ourIsWhite ? blackId : whiteId;

    // Create a proper store game record so standard game events (game_start,
    // move, opponent_move, search_info, clock_update, game_end) fire naturally
    // to all SSE clients — no translation shim needed in app.js.
    store.createGame(gameId, {
      color:         ourIsWhite ? 'white' : 'black',  // primary engine's colour
      opponentId:    oppId,
      opponentName:  oppName,
      opponentIsBot: true,
      speed:         'selfplay',
      rated:         false,
      initialFen:    startFen,
      service:       'selfplay',
      ourName,        // included in game_start event so dashboard shows correct name
      engineBuild:   ourReg?.archiveBuild ?? null,
    });
    store.updateGameMeta(gameId, {
      timeControl: `${Math.round(initial / 60000)}+${Math.round(inc / 1000)}`,
    });

    // Also emit sp_game_start for the sidebar standings / config display.
    store.emit('sp_game_start', {
      gameId,
      whiteId, blackId,
      whiteName, blackName,
      startFen,
      tc:        { ...this._matchState.tc },
      gameCount,
    });

    this._matchState.currentGame = {
      gameId, whiteId, blackId,
      whiteName, blackName,
      startFen, fen: startFen, ply: 0,
      moves: [],
      wtime: initial, btime: initial,
      tc: { initial, increment: inc },
    };

    const moves    = [];
    const posCount = new Map();
    try {
      const initKey = startFen.split(' ').slice(0, 4).join(' ');
      posCount.set(initKey, 1);
    } catch (_) {}

    let result = null;
    let reason = 'unknown';

    while (!result && this._matchState.running) {
      const curFen    = currentFen(startFen, moves);
      const sideToMove = curFen.split(' ')[1] === 'w' ? 'white' : 'black';

      // Detect checkmate / stalemate BEFORE asking the engine to move.
      // An engine handed a checkmate position may return garbage or hang entirely.
      try {
        const { checkmate, stalemate } = terminalStatus(curFen);
        if (checkmate) {
          result = sideToMove === 'white' ? 'black' : 'white';
          reason = 'checkmate';
          break;
        }
        if (stalemate) {
          result = 'draw'; reason = 'stalemate';
          break;
        }
      } catch (_) {}

      const engineId   = sideToMove === 'white' ? whiteId : blackId;
      const engine     = sideToMove === 'white' ? whiteEng : blackEng;

      // Cancel this engine's own ponder if it's still running from its last move.
      // (The engine that's ABOUT TO MOVE may have been pondering since 2 plies ago.)
      if (engine.isPondering()) {
        // White's ponder is tracked in the store (board is always from white's POV).
        if (sideToMove === 'white') store.ponderEnd(gameId, false);
        await engine.cancelPonder().catch(() => {});
      }

      const t0 = Date.now();
      // Signal search start to store — clears searchLive and emits search_start.
      // Only white's search drives the main board display.
      if (sideToMove === 'white') {
        const budget = useClock ? Math.max(wTimeMs, bTimeMs) : this._matchState.movetimeMs;
        store.searchStart(gameId, { maxTimeMs: budget }, null);
      }
      let bestResult;
      try {
        bestResult = await engine.thinkDynamic(startFen, moves.slice(), {
          ...(useClock
            ? { clock: { wtime: Math.max(wTimeMs, 100), btime: Math.max(bTimeMs, 100), winc: inc, binc: inc } }
            : { movetime: this._matchState.movetimeMs }
          ),
          onInfo: (infoHistory) => {
            const last = infoHistory[infoHistory.length - 1];
            if (last) {
              if (sideToMove === 'white') {
                // Route white's info through store for throttling + search_info event.
                store.searchInfo(gameId, last, null, last.time_ms ?? 0);
              }
              // Also emit raw sp_info for the sidebar eval panel (all engines).
              store.emit('sp_info', {
                gameId, engineId,
                depth:      last.depth,
                seldepth:   last.seldepth,
                score_cp:   last.eval_cp,
                score_mate: last.mate,
                nodes:      last.nodes,
                nps:        last.nps,
                pv:         last.pv ?? [],
                hashfull:   last.hashfull,
                tbhits:     last.tbhits,
                time_ms:    last.time_ms,
              });
            }
            return {};
          },
        });
      } catch (err) {
        // Only 'Engine returned no move' signals a genuine terminal position.
        // Any other error (e.g. 'Engine busy' due to a ponder not cancelled) is a
        // programming bug — log it loudly and abort with 'error' rather than
        // misclassifying as stalemate.
        if (!err.message?.includes('no move')) {
          console.error(`[selfplay] engine error on ply ${moves.length + 1} (${engineId}): ${err.message}`);
          result = 'draw'; reason = 'error';
          break;
        }
        // No legal moves → checkmate or stalemate
        try {
          const posFen = applyMoves(startFen, moves);
          if (posInCheck(posFen)) {
            result = sideToMove === 'white' ? 'black' : 'white'; reason = 'checkmate';
          } else {
            result = 'draw'; reason = 'stalemate';
          }
        } catch (_) { result = 'draw'; reason = 'stalemate'; }
        break;
      }

      if (!bestResult?.move || bestResult.move === '(none)') {
        try {
          const posFen = applyMoves(startFen, moves);
          if (posInCheck(posFen)) {
            result = sideToMove === 'white' ? 'black' : 'white'; reason = 'checkmate';
          } else {
            result = 'draw'; reason = 'stalemate';
          }
        } catch (_) { result = 'draw'; reason = 'stalemate'; }
        break;
      }

      // Validate the move is from a square that actually has a piece belonging
      // to the side to move. Engines can return garbage moves when a position
      // overflows their internal piece arrays (e.g. >32 pieces). Accepting a
      // corrupt move would pass an illegal position to the next engine, which
      // may then crash with a board-consistency assertion.
      try {
        const preMovePos = parseFen(applyMoves(startFen, moves));
        const uci = bestResult.move;
        const ff = uci.charCodeAt(0) - 97;
        const fr = uci.charCodeAt(1) - 49;
        const piece = (ff >= 0 && ff < 8 && fr >= 0 && fr < 8) ? preMovePos.board[fr][ff] : null;
        const ownPiece = piece && piece !== '.' &&
          (sideToMove === 'white' ? piece === piece.toUpperCase() : piece === piece.toLowerCase());
        if (!ownPiece) {
          console.error(`[selfplay] engine ${engineId} returned illegal move ${uci} on ply ${moves.length + 1} (from-square corrupt) — aborting game`);
          result = 'draw'; reason = 'error';
          break;
        }
      } catch (_) { /* parseFen failure is non-fatal */ }

      moves.push(bestResult.move);
      const newFen = applyMoves(startFen, moves);
      this._matchState.currentGame.fen = newFen;
      this._matchState.currentGame.ply = moves.length;
      this._matchState.currentGame.moves = moves.slice();

      // Deduct elapsed time from the mover's clock and add increment.
      // Skipped in movetime mode — each engine takes exactly movetimeMs per move.
      const elapsed = Date.now() - t0;
      if (useClock) {
        if (sideToMove === 'white') {
          wTimeMs = Math.max(wTimeMs - elapsed + inc, 0);
          if (wTimeMs === 0) { result = 'black'; reason = 'timeout'; }
        } else {
          bTimeMs = Math.max(bTimeMs - elapsed + inc, 0);
          if (bTimeMs === 0) { result = 'white'; reason = 'timeout'; }
        }
        this._matchState.currentGame.wtime = wTimeMs;
        this._matchState.currentGame.btime = bTimeMs;
      }

      if (sideToMove === 'white') {
        // White's move: emit search end, then record in store (drives main board).
        store.searchEnd(gameId, bestResult.move, bestResult.ponderMove ?? null, elapsed, null);
        store.recordFullMove(gameId, bestResult.move, moves.length - 1);
        store.updateFen(gameId, newFen);
        store.recordMove(gameId, {
          move:       bestResult.move,
          ply:        moves.length,
          depth:      bestResult.depth    ?? 0,
          seldepth:   bestResult.seldepth ?? 0,
          nodes:      bestResult.nodes    ?? 0,
          nps:        bestResult.nps      ?? 0,
          time_ms:    elapsed,
          eval_cp:    bestResult.eval_cp  ?? null,
          mate:       bestResult.mate     ?? null,
          stop_reason: null,
        });
      } else {
        // Black's (opponent's) move: update FEN first so g.fen is current when
        // the opponent_move event fires (board animation reads g.fullMoves).
        store.updateFen(gameId, newFen);
        store.recordOpponentMove(gameId, bestResult.move, moves.length - 1);
      }
      store.updateClock(gameId, wTimeMs, bTimeMs);

      // Also emit sp_move for the sidebar standings panel.
      store.emit('sp_move', {
        gameId, engineId,
        ply:           moves.length,
        uci:           bestResult.move,
        fen:           newFen,
        wtime:         wTimeMs,
        btime:         bTimeMs,
        nodesSearched: bestResult.nodes ?? null,
        depthReached:  bestResult.depth ?? null,
        ponderMove:    bestResult.ponderMove ?? null,
      });

      // Start pondering on the engine that just moved — it thinks ahead
      // while the opponent is deciding.  Do NOT start the opponent pondering;
      // the opponent engine needs to be free when its turn comes.
      if (!result && this._matchState.ponder && bestResult.ponderMove) {
        const justMovedEng = this._engineProcesses.get(engineId);
        if (justMovedEng && !justMovedEng._session) {
          const ponderOnInfo = (history) => {
            const info = history[history.length - 1];
            if (info) {
              if (sideToMove === 'white') {
                // White's ponder: route through store for correct eval-bar/arrow display.
                store.ponderInfo(gameId, info, 'opponent');
              }
              // Also emit raw sp_info for the sidebar eval panel.
              store.emit('sp_info', {
                gameId, engineId,
                depth:      info.depth,
                seldepth:   info.seldepth,
                score_cp:   info.eval_cp,
                score_mate: info.mate,
                nodes:      info.nodes,
                nps:        info.nps,
                pv:         info.pv ?? [],
                hashfull:   info.hashfull,
                tbhits:     info.tbhits,
                time_ms:    info.time_ms,
                ponderSide: 'opponent',
              });
            }
          };
          if (sideToMove === 'white') {
            store.ponderStart(gameId, bestResult.depth ?? 0, bestResult.ponderMove ?? null);
          }
          try { justMovedEng.startPonder(startFen, moves.slice(), ponderOnInfo); } catch (_) {}
        }
      }

      // Draw detection (skip if result already set by timeout)
      if (!result) {
        const key = newFen.split(' ').slice(0, 4).join(' ');
        const cnt = (posCount.get(key) ?? 0) + 1;
        posCount.set(key, cnt);
        if (cnt >= 3) { result = 'draw'; reason = 'repetition'; break; }

        const halfmove = parseInt(newFen.split(' ')[4] ?? '0', 10);
        if (halfmove >= 100) { result = 'draw'; reason = '50-move'; break; }
        if (isInsufficientMaterial(newFen)) { result = 'draw'; reason = 'insufficientMaterial'; break; }

        // Safety: cap at 500 plies
        if (moves.length >= 500) { result = 'draw'; reason = 'ply-limit'; break; }
      }
    }

    if (!result) { result = 'draw'; reason = 'stopped'; }

    // Update standings
    this._matchState.gameCount++;
    const ws = this._matchState.standings;
    if (!ws[whiteId]) ws[whiteId] = { w: 0, d: 0, l: 0, pts: 0 };
    if (!ws[blackId]) ws[blackId] = { w: 0, d: 0, l: 0, pts: 0 };
    if (result === 'white') {
      ws[whiteId].w++; ws[whiteId].pts++;
      ws[blackId].l++;
    } else if (result === 'black') {
      ws[blackId].w++; ws[blackId].pts++;
      ws[whiteId].l++;
    } else {
      ws[whiteId].d++; ws[whiteId].pts += 0.5;
      ws[blackId].d++; ws[blackId].pts += 0.5;
    }

    this._matchState.currentGame = null;

    // Persist the result into the store game record (fires standard game_end event).
    const storeResult = result === 'white' ? '1-0' : result === 'black' ? '0-1' : '1/2-1/2';
    store.endGame(gameId, storeResult, reason);

    // Also emit sp_game_end for the sidebar standings panel.
    store.emit('sp_game_end', {
      gameId, result, reason,
      plies:     moves.length,
      standings: JSON.parse(JSON.stringify(ws)),
    });

    // Send ucinewgame to clear TT but keep processes alive
    try { whiteEng.newGame(); } catch (_) {}
    try { blackEng.newGame(); } catch (_) {}

    return result;
  }

  /** Main match scheduling loop — runs detached via startMatch(). */
  async _matchLoop() {
    const st = this._matchState;

    // Resolve enabled engine registries
    const engIds = st.enabledEngines.filter(id =>
      this._engineRegistry.find(e => e.id === id && e.available)
    );
    if (engIds.length < 2) {
      console.error('[selfplay-match] not enough available engines');
      st.running = false;
      store.emit('sp_state', { matchState: this.getMatchState() });
      return;
    }

    if (st.mode === 'rr') {
      // Round-robin: each pair plays twice (both colors).
      // primaryId = lower-index engine in engIds so DB attribution is consistent
      // across both halves of each pair (e.g. A vs B and B vs A both recorded
      // from A's perspective when A has a lower index).
      const pairs = [];
      for (let i = 0; i < engIds.length; i++) {
        for (let j = i + 1; j < engIds.length; j++) {
          pairs.push([engIds[i], engIds[j], engIds[i]]);
          pairs.push([engIds[j], engIds[i], engIds[i]]);
        }
      }
      for (const [wId, bId, primaryId] of pairs) {
        if (!st.running) break;
        const fen = this._pickFen();
        await this._runGame(wId, bId, fen, primaryId);
        store.emit('sp_state', { matchState: this.getMatchState() });
      }
      st.running = false;
    } else if (st.mode === 'single') {
      const fen = this._pickFen();
      await this._runGame(engIds[0], engIds[1], fen, engIds[0]);
      st.running = false;
    } else {
      // Loop mode: alternate colors, keep going.
      // Always record from engIds[0]'s perspective so flipping colors does not
      // mix up which engine is "ours" in the DB.
      let flip = false;
      while (st.running) {
        const [wId, bId] = flip ? [engIds[1], engIds[0]] : [engIds[0], engIds[1]];
        flip = !flip;
        const fen = this._pickFen();
        await this._runGame(wId, bId, fen, engIds[0]);
        store.emit('sp_state', { matchState: this.getMatchState() });
        if (!st.running) break;
        // Brief yield between games
        await new Promise(r => setTimeout(r, 200));
      }
    }

    // Quit all match engine processes when done
    for (const [id, eng] of this._engineProcesses) {
      try { await eng.quit(); } catch (_) {}
      this._engineProcesses.delete(id);
    }

    st.running = false;
    store.emit('sp_state', { matchState: this.getMatchState() });
    console.log('[selfplay-match] match loop finished');
  }


  normalizeGameStart(event) {
    const g = event.game ?? event;
    return {
      gameId:   g.gameId ?? g.id,
      color:    g.color,
      opponent: {
        id:    g.opponent?.id       ?? 'sf',
        name:  g.opponent?.username ?? 'Stockfish',
        isBot: true,
      },
      variant: g.variant?.key ?? 'standard',
      speed:   g.speed ?? 'rapid',
    };
  }

  normalizeGameState(event) {
    return {
      moves:     event.moves ? event.moves.split(' ').filter(Boolean) : [],
      status:    event.status,
      winner:    event.winner ?? null,
      wtime:     event.wtime ?? null,
      btime:     event.btime ?? null,
      winc:      event.winc  ?? 0,
      binc:      event.binc  ?? 0,
      wdraw:     false,
      bdraw:     false,
      wtakeback: false,
      btakeback: false,
    };
  }

  // â”€â”€ No-op stubs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

  async acceptChallenge(_id, _token)             { return { ok: true,  status: 200, data: {} }; }
  async declineChallenge(_id, _token, _reason)   { return { ok: true,  status: 200, data: {} }; }
  async cancelChallenge(_id, _token)             { return { ok: true,  status: 200, data: {} }; }
  async chat(_gameId, _token, _text, _room)      { return { ok: true,  status: 200, data: {} }; }
  async handleDraw(_gameId, _accept, _token)     { return { ok: false, status: 400, data: {} }; }
  async handleTakeback(_gameId, _accept, _token) { return { ok: false, status: 400, data: {} }; }
  async claimVictory(_gameId, _token)            { return { ok: true,  status: 200, data: {} }; }

  async resignGame(gameId, _token) {
    const game = this._games.get(gameId);
    if (game) this._endGame(gameId, game.sfColor, 'resign');
    return { ok: true, status: 200, data: {} };
  }

  async abortGame(gameId, _token) {
    const game = this._games.get(gameId);
    if (game) {
      game.queue.push({
        type:   'gameState',
        moves:  game.moves.join(' '),
        status: 'aborted',
        winner: null,
        wtime:  game.tc?.wtime ?? 0,
        btime:  game.tc?.btime ?? 0,
        winc:   0,
        binc:   0,
      });
      game.queue.end();
      game.sfEngine.quit().catch(() => {});
      this._games.delete(gameId);
    }
    return { ok: true, status: 200, data: {} };
  }

  async *exportGames(_username, _token, _opts) { /* no games to export */ }
}

module.exports = SelfPlayAdapter;
