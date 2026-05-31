#!/usr/bin/env node
'use strict';

/**
 * pst_tuner.js — OLS-based Piece-Square Table tuner.
 *
 * Reads sidecar trace JSON files, extracts FEN + sf_eval + game phase,
 * builds a 768-dimensional phase-weighted feature matrix from piece
 * placements, and solves for optimal PST values via ridge-regularised
 * OLS (Cholesky decomposition).
 *
 * Output: 12 PST arrays (6 pieces × MG/EG) formatted as C++ initialiser
 * lists, ready to paste into eval_params.h.
 *
 * Usage:
 *   node tools/pst_tuner.js [options]
 *
 * Options:
 *   --dir <path>        Trace directory (default: data/games/trace)
 *   --build <n>         Filter by engine build number
 *   --max-eval <cp>     Exclude positions where |sf_eval| > cp (default: 1000)
 *   --lambda <f>        Ridge regularisation strength (default: 1e-4)
 *   --piece-values      Include piece base values as free variables (adds 10 params)
 *   --stats             Print per-piece MAE statistics
 *   --json              Output JSON instead of C++
 */

const fs   = require('fs');
const path = require('path');

// ── Piece types & names ───────────────────────────────────────────────────
const PIECES = ['pawn', 'knight', 'bishop', 'rook', 'queen', 'king'];
const PHASES = ['mg', 'eg'];

// FEN piece char → { color: 'w'|'b', pieceIdx: 0..5 }
const FEN_PIECE_MAP = {
  P: { color: 'w', pieceIdx: 0 },
  N: { color: 'w', pieceIdx: 1 },
  B: { color: 'w', pieceIdx: 2 },
  R: { color: 'w', pieceIdx: 3 },
  Q: { color: 'w', pieceIdx: 4 },
  K: { color: 'w', pieceIdx: 5 },
  p: { color: 'b', pieceIdx: 0 },
  n: { color: 'b', pieceIdx: 1 },
  b: { color: 'b', pieceIdx: 2 },
  r: { color: 'b', pieceIdx: 3 },
  q: { color: 'b', pieceIdx: 4 },
  k: { color: 'b', pieceIdx: 5 },
};

// Number of PST features: 6 pieces × 2 phases × 64 squares = 768
const N_PST = PIECES.length * PHASES.length * 64; // 768

// Default piece values (cp) — used to subtract material from sf_eval
// so we're fitting PST residuals only.  Must match PIECE_VALUES in eval.h.
const PIECE_VALUES = [100, 320, 330, 500, 900, 0]; // pawn..king

// ── CLI args ──────────────────────────────────────────────────────────────
function parseArgs() {
  const args = process.argv.slice(2);
  const opts = {
    dir:         path.join(__dirname, '..', '..', 'data', 'games', 'trace'),
    build:       null,
    maxEval:     1000,
    lambda:      1e-4,
    pieceValues: false,
    stats:       false,
    json:        false,
    normalize:   false,
    residualTarget: false,
  };
  for (let i = 0; i < args.length; i++) {
    switch (args[i]) {
      case '--dir':          opts.dir = args[++i]; break;
      case '--build':        opts.build = args[++i].split(',').map(s => s.trim()); break;
      case '--max-eval':     opts.maxEval = parseInt(args[++i], 10); break;
      case '--lambda':       opts.lambda = parseFloat(args[++i]); break;
      case '--piece-values': opts.pieceValues = true; break;
      case '--stats':        opts.stats = true; break;
      case '--json':         opts.json = true; break;
      case '--normalize':   opts.normalize = true; break;
      case '--residual-target': opts.residualTarget = true; break;
      case '--help': case '-h':
        console.log('Usage: node tools/pst_tuner.js [--dir path] [--build n] [--max-eval cp] [--lambda f] [--normalize] [--stats] [--json]');
        process.exit(0);
      default:
        console.error(`Unknown option: ${args[i]}`);
        process.exit(1);
    }
  }
  return opts;
}

// ── Parse FEN → piece placements ──────────────────────────────────────────
// Returns array of { color: 'w'|'b', pieceIdx: 0..5, sq: 0..63 }
// Square convention: a1=0, b1=1, ..., h8=63 (same as engine).
// FEN rank order: rank 8 first (top of board).
function parseFenPieces(fen) {
  const placement = fen.split(' ')[0];
  const ranks = placement.split('/');
  const pieces = [];
  for (let ri = 0; ri < 8; ri++) {
    const rank = ranks[ri];
    const engineRank = 7 - ri; // FEN rank 8 = engine rank 7
    let file = 0;
    for (const ch of rank) {
      if (ch >= '1' && ch <= '8') {
        file += parseInt(ch, 10);
      } else {
        const info = FEN_PIECE_MAP[ch];
        if (info) {
          const sq = engineRank * 8 + file;
          pieces.push({ color: info.color, pieceIdx: info.pieceIdx, sq });
        }
        file++;
      }
    }
  }
  return pieces;
}

// ── Build feature vector for one position ─────────────────────────────────
// Feature layout: [pawn_mg_0..pawn_mg_63, pawn_eg_0..pawn_eg_63,
//                  knight_mg_0..knight_mg_63, ... king_eg_0..king_eg_63]
// = 6 pieces × 2 phases × 64 squares = 768 features.
//
// For WHITE piece on sq:
//   PST lookup in engine = PST[mirror(sq)] where mirror = sq ^ 56
//   So the feature index = piece_offset + (sq ^ 56)
//   MG weight = +phase/256, EG weight = +(256-phase)/256
//
// For BLACK piece on sq:
//   PST lookup = PST[sq] (no mirror)
//   Feature index = piece_offset + sq
//   Sign is NEGATIVE (black contribution subtracted from white-POV score)
//
function buildFeatureVector(pieces, phase) {
  const x = new Float64Array(N_PST);
  const mgWeight = phase / 256;
  const egWeight = (256 - phase) / 256;

  for (const { color, pieceIdx, sq } of pieces) {
    const mgOffset = (pieceIdx * 2) * 64;     // MG start for this piece
    const egOffset = (pieceIdx * 2 + 1) * 64; // EG start for this piece
    const sign = color === 'w' ? 1 : -1;

    // PST index: white uses mirror(sq)=sq^56, black uses sq directly
    const pstSq = color === 'w' ? (sq ^ 56) : sq;

    x[mgOffset + pstSq] += sign * mgWeight;
    x[egOffset + pstSq] += sign * egWeight;
  }
  return x;
}

// ── Compute material score for a position (to subtract from target) ───────
function materialScore(pieces) {
  let score = 0;
  for (const { color, pieceIdx } of pieces) {
    const sign = color === 'w' ? 1 : -1;
    score += sign * PIECE_VALUES[pieceIdx];
  }
  return score;
}

// ── Load trace files and extract data points ──────────────────────────────

// -- Load trace files and extract data points ------------------------------
function loadData(opts) {
  if (!fs.existsSync(opts.dir)) {
    console.error(`Trace directory not found: ${opts.dir}`);
    process.exit(1);
  }
  const files = fs.readdirSync(opts.dir).filter(f => f.endsWith('.json'));
  if (files.length === 0) {
    console.error('No trace files found.');
    process.exit(1);
  }

  const data = []; // { x: Float64Array, y: number }
  let nGames = 0;
  let nSkipped = 0;
  let nResidual = 0;
  const builds = new Set();

  for (const f of files) {
    let trace;
    try {
      trace = JSON.parse(fs.readFileSync(path.join(opts.dir, f), 'utf8'));
    } catch (_) { continue; }

    if (opts.build != null && !opts.build.includes(String(trace.build))) continue;
    builds.add(trace.build);

    let gameUsed = false;
    for (const m of (trace.moves ?? [])) {
      if (!m.fen || m.sf_eval == null || !m.eval_vec) continue;

      // m.sf_eval is STM (positive = good for the side to move in the stored
      // FEN).  The stored FEN is the position AFTER the bot moved, so the FEN
      // stm is the OPPONENT (not the bot).  Convert to white POV:
      //   FEN stm='b' → SF evaluated from black's perspective → negate for white POV.
      //   FEN stm='w' → SF evaluated from white's perspective → already white POV.
      const fenStm = m.fen.split(' ')[1] ?? 'w';
      const sfEvalWhite = fenStm === 'b' ? -m.sf_eval : m.sf_eval;

      if (opts.maxEval != null && Math.abs(sfEvalWhite) > opts.maxEval) {
        nSkipped++;
        continue;
      }

      const phase = m.eval_vec.phase ?? 128;
      const pieces = parseFenPieces(m.fen);
      const x = buildFeatureVector(pieces, phase);

      let y;
      if (opts.residualTarget && m.eval_vec.total != null && m.eval_vec.material_pst) {
        // Residual target: what material_pst should be to make our total == SF.
        // y = sf_eval_white - (eval_total_white - material_pst_blend)
        //   = sf_eval_white - eval_total_white + material_pst_blend
        const pstMg = m.eval_vec.material_pst[0];
        const pstEg = m.eval_vec.material_pst[1];
        const pstBlend = pstMg * phase / 256 + pstEg * (256 - phase) / 256;
        y = sfEvalWhite - m.eval_vec.total + pstBlend;
        nResidual++;
      } else {
        // Standard target: sf_eval_white - material (fit PST + all other terms)
        const matScore = materialScore(pieces);
        y = sfEvalWhite - matScore;
      }

      data.push({ x, y });
      gameUsed = true;
    }
    if (gameUsed) nGames++;
  }

  if (opts.residualTarget) console.error(`Residual target: ${nResidual} positions used.`);
  return { data, nGames, nSkipped, builds: [...builds].sort() };
}
function olsSolve(data, k, lambda) {
  const n = data.length;
  if (n < k + 1) {
    console.error(`Not enough data: ${n} positions for ${k} unknowns.`);
    process.exit(1);
  }

  console.error(`Building normal equations (${n} × ${k})...`);

  // X^T X  (k × k) — build incrementally to avoid huge n×k matrix
  const XtX = new Float64Array(k * k);
  const Xty = new Float64Array(k);

  for (let r = 0; r < n; r++) {
    const x = data[r].x;
    const yr = data[r].y;
    for (let i = 0; i < k; i++) {
      if (x[i] === 0) continue;
      Xty[i] += x[i] * yr;
      for (let j = i; j < k; j++) {
        if (x[j] === 0) continue;
        XtX[i * k + j] += x[i] * x[j];
      }
    }
  }
  // Symmetric fill
  for (let i = 0; i < k; i++)
    for (let j = i + 1; j < k; j++)
      XtX[j * k + i] = XtX[i * k + j];

  // Ridge regularisation
  const ridgePenalty = lambda * n;
  for (let i = 0; i < k; i++)
    XtX[i * k + i] += ridgePenalty;

  console.error('Solving via Cholesky decomposition...');

  // Cholesky: XtX = L L^T
  const L = new Float64Array(k * k);
  for (let i = 0; i < k; i++) {
    for (let j = 0; j <= i; j++) {
      let s = XtX[i * k + j];
      for (let p = 0; p < j; p++) s -= L[i * k + p] * L[j * k + p];
      if (i === j) {
        if (s <= 0) {
          console.error(`Cholesky failed at index ${i}: matrix not positive definite (s=${s}).`);
          process.exit(1);
        }
        L[i * k + j] = Math.sqrt(s);
      } else {
        L[i * k + j] = s / L[j * k + j];
      }
    }
  }

  // Forward substitution: L z = Xty
  const z = new Float64Array(k);
  for (let i = 0; i < k; i++) {
    let s = Xty[i];
    for (let j = 0; j < i; j++) s -= L[i * k + j] * z[j];
    z[i] = s / L[i * k + i];
  }

  // Back substitution: L^T w = z
  const w = new Float64Array(k);
  for (let i = k - 1; i >= 0; i--) {
    let s = z[i];
    for (let j = i + 1; j < k; j++) s -= L[j * k + i] * w[j];
    w[i] = s / L[i * k + i];
  }

  return w;
}

// ── Compute fit statistics ────────────────────────────────────────────────
function computeStats(data, w) {
  const n = data.length;
  let sumAbsErr = 0;
  let sumSqErr = 0;
  for (let r = 0; r < n; r++) {
    const x = data[r].x;
    let pred = 0;
    for (let j = 0; j < w.length; j++) pred += w[j] * x[j];
    const err = data[r].y - pred;
    sumAbsErr += Math.abs(err);
    sumSqErr += err * err;
  }
  return {
    mae:  sumAbsErr / n,
    rmse: Math.sqrt(sumSqErr / n),
  };
}

// ── Format PST as C++ array ───────────────────────────────────────────────
// -- Normalize PST arrays -----------------------------------------------------
// Subtract per-table mean so the negative offset absorbed from non-PST eval
// terms is removed.  For pawns, indices 0-7 and 56-63 are unused (pawns live
// on ranks 2-7 = PST indices 8-55), so they are zeroed; mean computed over 8-55.
function normalizePsts(psts) {
  const out = {};
  for (const [key, vals] of Object.entries(psts)) {
    const isPawn = key.startsWith('pst_pawn_');
    const arr = Float64Array.from(vals);
    if (isPawn) {
      let sum = 0;
      for (let i = 8; i < 56; i++) sum += arr[i];
      const mean = sum / 48;
      for (let i = 8; i < 56; i++) arr[i] -= mean;
      for (let i = 0;  i <  8; i++) arr[i] = 0;
      for (let i = 56; i < 64; i++) arr[i] = 0;
    } else {
      let sum = 0;
      for (let i = 0; i < 64; i++) sum += arr[i];
      const mean = sum / 64;
      for (let i = 0; i < 64; i++) arr[i] -= mean;
    }
    out[key] = Array.from(arr);
  }
  return out;
}

function formatCppArray(name, values) {
  const lines = [`    int ${name}[64] = {`];
  for (let rank = 0; rank < 8; rank++) {
    const start = rank * 8;
    const row = [];
    for (let file = 0; file < 8; file++) {
      row.push(String(Math.round(values[start + file])).padStart(4));
    }
    const comma = rank < 7 ? ',' : '';
    const rankLabel = `  // rank ${8 - rank}`;
    lines.push(`        ${row.join(',')}${comma}${rankLabel}`);
  }
  lines.push('    };');
  return lines.join('\n');
}

// ── Main ──────────────────────────────────────────────────────────────────
function main() {
  const opts = parseArgs();

  console.error('Loading traces...');
  const { data, nGames, nSkipped, builds } = loadData(opts);
  console.error(`Loaded ${data.length} positions from ${nGames} games (builds: ${builds.join(',')}).`);
  if (nSkipped > 0) console.error(`Skipped ${nSkipped} positions with |sf_eval| > ${opts.maxEval}.`);

  if (data.length < 1000) {
    console.error(`Warning: only ${data.length} positions — results may be noisy.`);
  }

  const k = N_PST; // 768
  const w = olsSolve(data, k, opts.lambda);

  // Compute fit quality
  const stats = computeStats(data, w);
  console.error(`\nFit quality (PST residuals):`);
  console.error(`  MAE:  ${stats.mae.toFixed(1)} cp`);
  console.error(`  RMSE: ${stats.rmse.toFixed(1)} cp`);

  // Extract 12 PST arrays from the flat weight vector
  const psts = {};
  for (let pi = 0; pi < PIECES.length; pi++) {
    for (let phi = 0; phi < PHASES.length; phi++) {
      const offset = (pi * 2 + phi) * 64;
      const key = `pst_${PIECES[pi]}_${PHASES[phi]}`;
      psts[key] = Array.from(w.slice(offset, offset + 64));
    }
  }


  // Normalize: subtract per-table mean to remove offset absorbed from other eval terms
  const finalPsts = opts.normalize ? normalizePsts(psts) : psts;
  if (opts.normalize) console.error('Normalization applied (per-table mean subtracted).');
  if (opts.json) {
    console.log(JSON.stringify(finalPsts, null, 2));
    return;
  }

  // Print C++ output
  console.log('// ── PST arrays tuned by tools/pst_tuner.js ──');
  console.log(`// ${data.length} positions, ${nGames} games, builds: ${builds.join(',')}`);
  console.log(`// MAE: ${stats.mae.toFixed(1)} cp  |  RMSE: ${stats.rmse.toFixed(1)} cp`);
  console.log(`// Lambda: ${opts.lambda}  |  Max eval: ${opts.maxEval} cp`);
  console.log('');

  console.log('    // --- Middlegame PSTs ---\n');
  for (const piece of PIECES) {
    console.log(formatCppArray(`pst_${piece}_mg`, finalPsts[`pst_${piece}_mg`]));
    console.log('');
  }

  console.log('    // --- Endgame PSTs ---\n');
  for (const piece of PIECES) {
    console.log(formatCppArray(`pst_${piece}_eg`, finalPsts[`pst_${piece}_eg`]));
    console.log('');
  }

  // Per-piece stats
  if (opts.stats) {
    console.error('\n=== Per-piece PST statistics ===');
    for (const piece of PIECES) {
      for (const phase of PHASES) {
        const key = `pst_${piece}_${phase}`;
        const vals = finalPsts[key];
        const min = Math.min(...vals);
        const max = Math.max(...vals);
        const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
        const range = max - min;
        console.error(`  ${key.padEnd(18)} min=${min.toFixed(0).padStart(5)}  max=${max.toFixed(0).padStart(5)}  mean=${mean.toFixed(1).padStart(7)}  range=${range.toFixed(0).padStart(5)}`);
      }
    }
  }
}

main();
