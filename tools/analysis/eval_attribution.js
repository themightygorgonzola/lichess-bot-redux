#!/usr/bin/env node
'use strict';

/**
 * eval_attribution.js — Per-rule error attribution analysis.
 *
 * Reads sidecar trace JSON files produced by the bot's eval diagnostics pipeline,
 * computes our eval error vs Stockfish for every traced position, and attributes
 * that error to each of the 19 HCE eval terms.
 *
 * Usage:
 *   node tools/eval_attribution.js [options]
 *
 * Options:
 *   --dir <path>        Trace directory (default: data/games/trace)
 *   --min-games <n>     Minimum games required (default: 1)
 *   --phase <lo>-<hi>   Filter by game phase range 0-256 (e.g. --phase 0-128 for endgames)
 *   --result <w|l|d>    Filter by bot result: w=win, l=loss, d=draw
 *   --build <n>         Filter by engine build number
 *   --csv               Output CSV instead of table
 *   --json              Output JSON
 *   --regression        Run OLS regression for optimal term scaling
 *   --demean            Remove per-game mean error before OLS/correlations (reduces game-outcome confounding)
 *   --max-eval <cp>     Exclude positions where |sf_eval| > cp (e.g. 1000 to strip TB scores)
 *   --phase-split       Run separate OLS regressions for MG (phase>150) and EG (phase<=100)
 */

const fs   = require('fs');
const path = require('path');

// ── 19 HCE eval term keys (must match EVAL_TERM_KEYS in eval.h) ──────────
const TERM_KEYS = [
  'material_pst', 'bishop_pair', 'rook_files', 'pawn_structure', 'mobility',
  'rook_7th', 'outposts', 'pins', 'pin_creation', 'bad_bishop',
  'threats', 'space', 'rook_behind_passer', 'king_passer_dist', 'weak_minor',
  'king_safety', 'castling', 'mopup', 'tempo',
];

// ── CLI args ──────────────────────────────────────────────────────────────
function parseArgs() {
  const args = process.argv.slice(2);
  const opts = {
    dir:        path.join(__dirname, '..', '..', 'data', 'games', 'trace'),
    minGames:   1,
    phaseLo:    0,
    phaseHi:    256,
    result:     null,   // 'w' | 'l' | 'd'
    build:      null,
    csv:        false,
    json:       false,
    regression: false,
    demean:     false,
    maxEval:    null,   // filter |sf_eval| above this magnitude
    phaseSplit: false,
  };
  for (let i = 0; i < args.length; i++) {
    switch (args[i]) {
      case '--dir':        opts.dir = args[++i]; break;
      case '--min-games':  opts.minGames = parseInt(args[++i], 10); break;
      case '--phase': {
        const [lo, hi] = args[++i].split('-').map(Number);
        opts.phaseLo = lo; opts.phaseHi = hi;
        break;
      }
      case '--result':     opts.result = args[++i]; break;
      case '--build':      opts.build = args[++i]; break;
      case '--csv':        opts.csv = true; break;
      case '--json':       opts.json = true; break;
      case '--regression': opts.regression = true; break;
      case '--demean':      opts.demean     = true; break;
      case '--max-eval':    opts.maxEval    = parseInt(args[++i], 10); break;
      case '--phase-split': opts.phaseSplit = true; break;
      default:
        if (args[i] === '--help' || args[i] === '-h') {
          console.log('Usage: node tools/eval_attribution.js [--dir path] [--phase lo-hi] [--result w|l|d] [--build n] [--csv] [--json] [--regression]');
          process.exit(0);
        }
        console.error(`Unknown option: ${args[i]}`);
        process.exit(1);
    }
  }
  return opts;
}

// ── Load trace files ──────────────────────────────────────────────────────
function loadTraces(dir, opts) {
  if (!fs.existsSync(dir)) {
    console.error(`Trace directory not found: ${dir}`);
    process.exit(1);
  }
  const files = fs.readdirSync(dir).filter(f => f.endsWith('.json'));
  if (files.length === 0) {
    console.error('No trace files found.');
    process.exit(1);
  }

  const games = [];
  for (const f of files) {
    try {
      const data = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8'));
      // Filter by build
      if (opts.build != null && String(data.build) !== String(opts.build)) continue;
      // Filter by result
      if (opts.result != null) {
        const br = botResult(data);
        if (br !== opts.result) continue;
      }
      games.push(data);
    } catch (_) { /* skip corrupt files */ }
  }
  return games;
}

function botResult(trace) {
  const r = trace.result;
  const c = trace.color;
  if (!r || !c) return null;
  if (r === '1/2-1/2') return 'd';
  if ((r === '1-0' && c === 'white') || (r === '0-1' && c === 'black')) return 'w';
  return 'l';
}

// ── Blend MG/EG using game phase ──────────────────────────────────────────
function blend(mg, eg, phase) {
  return (mg * phase + eg * (256 - phase)) / 256;
}

// ── Extract data points from traces ───────────────────────────────────────
function extractPositions(games, opts) {
  const positions = [];

  for (const game of games) {
    const br = botResult(game);
    const gamePositions = [];
    for (const m of (game.moves ?? [])) {
      if (!m.eval_vec || m.sf_eval == null) continue;

      const vec = m.eval_vec;
      const phase = vec.phase ?? 128;
      if (phase < opts.phaseLo || phase > opts.phaseHi) continue;

      // Filter out TB / extreme SF evals (e.g. Syzygy tablebase scores at ±8000)
      if (opts.maxEval != null && Math.abs(m.sf_eval) > opts.maxEval) continue;

      // Compute blended value for each term
      const termValues = {};
      let termSum = 0;
      for (const key of TERM_KEYS) {
        const pair = vec[key];
        if (!pair || pair.length < 2) { termValues[key] = 0; continue; }
        const v = blend(pair[0], pair[1], phase);
        termValues[key] = v;
        termSum += v;
      }

      // Our eval (White POV, cp)
      const ourEval = vec.total ?? termSum;
      // SF eval converted to White POV (sf_eval in traces is STM convention:
      // positive = good for the side to move in the stored FEN, not white).
      // FEN stm = opponent to move (position after bot played) → negate when 'b'.
      const fenStm = m.fen?.split(' ')[1] ?? 'w';
      const sfEval = fenStm === 'b' ? -m.sf_eval : m.sf_eval;
      // Raw error: how much our eval overestimates vs SF
      const error = ourEval - sfEval;

      gamePositions.push({
        gameId:  game.id,
        ply:     m.ply,
        phase,
        ourEval,
        sfEval,
        error,
        terms:   termValues,
        botResult: br,
      });
    }

    // Per-game demeaning: remove this game's mean error before adding to pool.
    // This strips the game-outcome fixed effect ("bot was losing all game, so
    // every White-positive term spuriously correlates with positive error").
    if (opts.demean && gamePositions.length > 0) {
      const gameMeanErr = mean(gamePositions.map(p => p.error));
      for (const p of gamePositions) p.error -= gameMeanErr;
    }

    positions.push(...gamePositions);
  }
  return positions;
}

// ── Statistics helpers ────────────────────────────────────────────────────
function mean(arr) {
  if (arr.length === 0) return 0;
  return arr.reduce((a, b) => a + b, 0) / arr.length;
}

function stddev(arr) {
  if (arr.length < 2) return 0;
  const m = mean(arr);
  return Math.sqrt(arr.reduce((s, x) => s + (x - m) ** 2, 0) / (arr.length - 1));
}

function pearson(xs, ys) {
  const n = xs.length;
  if (n < 3) return 0;
  const mx = mean(xs), my = mean(ys);
  let num = 0, dx2 = 0, dy2 = 0;
  for (let i = 0; i < n; i++) {
    const dx = xs[i] - mx, dy = ys[i] - my;
    num += dx * dy;
    dx2 += dx * dx;
    dy2 += dy * dy;
  }
  const denom = Math.sqrt(dx2 * dy2);
  return denom > 0 ? num / denom : 0;
}

function median(arr) {
  if (arr.length === 0) return 0;
  const s = arr.slice().sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

// ── OLS regression: find optimal term scaling ─────────────────────────────
// Solves: sf_eval ≈ sum(w_i * term_i) via normal equations X^T X w = X^T y.
// Returns array of {key, weight, correction} where correction = weight - 1.
// minAbsMean: drop terms whose mean-of-absolute-values across these positions is below this
//   threshold. This prevents multicollinearity blow-ups from terms that rarely fire in a
//   given phase regime (e.g. pins/pin_creation in endgames).
function olsRegression(positions, minAbsMean = 0) {
  const n = positions.length;

  // Determine active term keys (drop terms with negligible average magnitude)
  const activeKeys = minAbsMean > 0
    ? TERM_KEYS.filter(k => {
        const vals = positions.map(p => p.terms[k]);
        const absMn = vals.reduce((s, x) => s + Math.abs(x), 0) / vals.length;
        return absMn >= minAbsMean;
      })
    : TERM_KEYS;

  const k = activeKeys.length;
  if (n < k + 1) return null; // not enough data

  // Build X (n x k) and y (n x 1)
  const X = new Float64Array(n * k);
  const y = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    y[i] = positions[i].sfEval;
    for (let j = 0; j < k; j++) {
      X[i * k + j] = positions[i].terms[activeKeys[j]];
    }
  }

  // X^T X  (k x k)
  const XtX = new Float64Array(k * k);
  for (let i = 0; i < k; i++) {
    for (let j = i; j < k; j++) {
      let s = 0;
      for (let r = 0; r < n; r++) s += X[r * k + i] * X[r * k + j];
      XtX[i * k + j] = s;
      XtX[j * k + i] = s;
    }
  }

  // X^T y  (k x 1)
  const Xty = new Float64Array(k);
  for (let j = 0; j < k; j++) {
    let s = 0;
    for (let r = 0; r < n; r++) s += X[r * k + j] * y[r];
    Xty[j] = s;
  }

  // Solve via Cholesky (with ridge regularization for stability)
  const lambda = 1e-6 * n;
  for (let i = 0; i < k; i++) XtX[i * k + i] += lambda;

  // Cholesky decomposition: XtX = L L^T
  const L = new Float64Array(k * k);
  for (let i = 0; i < k; i++) {
    for (let j = 0; j <= i; j++) {
      let s = XtX[i * k + j];
      for (let p = 0; p < j; p++) s -= L[i * k + p] * L[j * k + p];
      if (i === j) {
        if (s <= 0) return null; // not positive definite
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

  return activeKeys.map((key, i) => ({
    key,
    weight:     w[i],
    correction: w[i] - 1,
  }));
}

// ── Compute per-term attribution ──────────────────────────────────────────
function computeAttribution(positions) {
  const errors = positions.map(p => p.error);
  const result = {};

  for (const key of TERM_KEYS) {
    const vals = positions.map(p => p.terms[key]);
    const meanVal = mean(vals);
    const sdVal   = stddev(vals);
    const corr    = pearson(vals, errors);

    // Mean contribution to error: how much of the mean error comes from this term
    // If all terms were perfectly scaled, mean error would be 0.
    // The "error contribution" of a term = how much the term's mean value overshoots
    // relative to SF's implicit weighting. Approximated by corr * sd(term) * sd(error).
    const sdErr = stddev(errors);
    const contribution = corr * sdVal * sdErr;

    result[key] = {
      mean:         meanVal,
      stddev:       sdVal,
      corr_error:   corr,
      contribution: contribution,
      abs_mean:     mean(vals.map(Math.abs)),
    };
  }
  return result;
}

// ── Output formatters ─────────────────────────────────────────────────────
function printTable(summary, attribution, regressionResult) {
  console.log('=== Eval Attribution Report ===\n');
  const filterNotes = [];
  if (summary.demean) filterNotes.push('per-game demeaned');
  if (summary.maxEval != null) filterNotes.push(`|sf_eval|≤${summary.maxEval}cp`);
  const filterStr = filterNotes.length ? `  [${filterNotes.join(', ')}]` : '';
  console.log(`Positions: ${summary.nPositions}  |  Games: ${summary.nGames}  |  Build: ${summary.builds.join(',')}${filterStr}`);
  console.log(`MAE: ${summary.mae.toFixed(1)} cp  |  Median error: ${summary.medianError.toFixed(1)} cp  |  Mean error: ${summary.meanError.toFixed(1)} cp`);
  console.log(`Phase range: ${summary.phaseLo}-${summary.phaseHi}  |  Mean phase: ${summary.meanPhase.toFixed(0)}`);
  if (summary.resultFilter) console.log(`Result filter: ${summary.resultFilter}`);
  console.log('');

  // Sort by absolute correlation (most impactful first)
  const sorted = TERM_KEYS.slice().sort((a, b) =>
    Math.abs(attribution[b].corr_error) - Math.abs(attribution[a].corr_error)
  );

  const hdr = 'Term'.padEnd(20) +
    'Mean'.padStart(8) +
    'StdDev'.padStart(8) +
    '|AbsMean|'.padStart(10) +
    'Corr(err)'.padStart(10) +
    'Contrib'.padStart(10);
  console.log(hdr);
  console.log('-'.repeat(hdr.length));

  for (const key of sorted) {
    const a = attribution[key];
    console.log(
      key.padEnd(20) +
      a.mean.toFixed(1).padStart(8) +
      a.stddev.toFixed(1).padStart(8) +
      a.abs_mean.toFixed(1).padStart(10) +
      a.corr_error.toFixed(3).padStart(10) +
      a.contribution.toFixed(1).padStart(10)
    );
  }

  if (regressionResult) {
    printOlsTable('OLS Regression: Optimal Term Scaling', regressionResult.all);
    if (regressionResult.mg) printOlsTable('OLS (Midgame only — phase > 150)', regressionResult.mg);
    if (regressionResult.eg) printOlsTable('OLS (Endgame only — phase ≤ 100)', regressionResult.eg);
  }
}

function printOlsTable(title, rows) {
  if (!rows) { console.log(`\n=== ${title} ===\n  (insufficient data)`); return; }
  console.log(`\n=== ${title} ===\n`);
  console.log('Term'.padEnd(20) + 'Weight'.padStart(8) + 'N'.padStart(6) + 'Correction'.padStart(12));
  console.log('-'.repeat(46));
  const sorted = rows.slice().sort((a, b) => Math.abs(b.correction) - Math.abs(a.correction));
  for (const r of sorted) {
    console.log(
      r.key.padEnd(20) +
      r.weight.toFixed(3).padStart(8) +
      (r.n != null ? r.n.toString() : '').padStart(6) +
      ((r.correction >= 0 ? '+' : '') + r.correction.toFixed(3)).padStart(12)
    );
  }
}

function printCsv(attribution, regressionResult) {
  const cols = ['term', 'mean', 'stddev', 'abs_mean', 'corr_error', 'contribution'];
  if (regressionResult) cols.push('ols_weight', 'ols_correction');
  console.log(cols.join(','));
  for (const key of TERM_KEYS) {
    const a = attribution[key];
    const row = [key, a.mean.toFixed(2), a.stddev.toFixed(2), a.abs_mean.toFixed(2),
                 a.corr_error.toFixed(4), a.contribution.toFixed(2)];
    if (regressionResult) {
      const r = regressionResult.find(x => x.key === key);
      row.push(r ? r.weight.toFixed(4) : '', r ? r.correction.toFixed(4) : '');
    }
    console.log(row.join(','));
  }
}

function printJson(summary, attribution, regressionResult) {
  console.log(JSON.stringify({ summary, attribution, regression: regressionResult }, null, 2));
}

// ── Main ──────────────────────────────────────────────────────────────────
function main() {
  const opts = parseArgs();
  const games = loadTraces(opts.dir, opts);

  if (games.length < opts.minGames) {
    console.error(`Only ${games.length} games found (need ${opts.minGames}).`);
    process.exit(1);
  }

  const positions = extractPositions(games, opts);
  if (positions.length === 0) {
    console.error('No traced positions with both eval_vec and sf_eval.');
    process.exit(1);
  }

  const errors = positions.map(p => p.error);
  const phases = positions.map(p => p.phase);
  const builds = [...new Set(games.map(g => g.build).filter(Boolean))];

  const summary = {
    nPositions:   positions.length,
    nGames:       games.length,
    builds,
    mae:          mean(errors.map(Math.abs)),
    meanError:    mean(errors),
    medianError:  median(errors),
    meanPhase:    mean(phases),
    phaseLo:      opts.phaseLo,
    phaseHi:      opts.phaseHi,
    resultFilter: opts.result,
    demean:       opts.demean,
    maxEval:      opts.maxEval,
  };

  const attribution = computeAttribution(positions);
  let regressionResult = null;
  if (opts.regression) {
    const allOls = olsRegression(positions);
    // Annotate each row with n (same for all since we use the full set)
    if (allOls) allOls.forEach(r => { r.n = positions.length; });

    let mgOls = null, egOls = null;
    if (opts.phaseSplit) {
      const mgPos = positions.filter(p => p.phase > 150);
      const egPos = positions.filter(p => p.phase <= 100);
      // Use minAbsMean=2.0 for phase subsets: terms with negligible average magnitude in
      // a phase regime (e.g. pins/pin_creation in endgames, mopup in midgame) cause
      // multicollinearity blow-ups — their rare occurrence means the OLS can assign
      // arbitrary offsetting weights without changing residuals much.
      mgOls = olsRegression(mgPos, 2.0);
      if (mgOls) mgOls.forEach(r => { r.n = mgPos.length; });
      egOls = olsRegression(egPos, 2.0);
      if (egOls) egOls.forEach(r => { r.n = egPos.length; });
    }

    regressionResult = { all: allOls, mg: mgOls, eg: egOls };
  }

  if (opts.json) {
    printJson(summary, attribution, regressionResult);
  } else if (opts.csv) {
    printCsv(attribution, regressionResult);
  } else {
    printTable(summary, attribution, regressionResult);
  }
}

main();
