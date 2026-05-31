#!/usr/bin/env node
'use strict';
/**
 * inspect_trace.js — Human-readable per-game eval diagnostic report.
 *
 * Usage:
 *   node tools/inspect_trace.js <game-id-or-path> [options]
 *
 * Examples:
 *   node tools/inspect_trace.js sp_1776388666140_gywwq
 *   node tools/inspect_trace.js sp_1776388666140_gywwq --terms
 *   node tools/inspect_trace.js sp_1776388666140_gywwq --worst 6
 *   node tools/inspect_trace.js sp_1776388666140_gywwq --depth
 *
 * Options:
 *   --terms          Show eval term breakdown in the move table
 *   --worst <n>      Show full term breakdown for N worst-error moves (default 3)
 *   --depth          Show depth history for each move
 *   --no-summary     Skip the game summary header
 *   --no-table       Skip the per-move table (header + worst breakdowns only)
 *   --out <file>     Write report to file instead of stdout
 *   --triage         One-line summary per trace file, sorted by static MAE (no game-id needed)
 *   --max-eval <cp>  Exclude positions where |sf_eval| exceeds this (strips TB/blowout noise)
 *   --build <n>      (triage only) Filter to games from a specific build number
 */

const fs   = require('fs');
const path = require('path');

const TRACE_DIR = path.join(__dirname, '..', '..', 'data', 'games', 'trace');

const TERM_KEYS = [
  'material_pst', 'bishop_pair', 'rook_files', 'pawn_structure', 'mobility',
  'rook_7th', 'outposts', 'pins', 'pin_creation', 'bad_bishop',
  'threats', 'space', 'rook_behind_passer', 'king_passer_dist', 'weak_minor',
  'king_safety', 'castling', 'mopup', 'tempo',
];

// ── CLI ───────────────────────────────────────────────────────────────────
function parseArgs() {
  const args = process.argv.slice(2);
  const opts = {
    id:        null,
    terms:     false,
    worst:     3,
    depth:     false,
    summary:   true,
    table:     true,
    out:       null,
    triage:    false,
    maxEval:   null,   // exclude |sf_eval| > this
    build:     null,   // triage: filter by build number
  };
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (!a.startsWith('--')) { opts.id = a; continue; }
    switch (a) {
      case '--terms':      opts.terms   = true; break;
      case '--depth':      opts.depth   = true; break;
      case '--no-summary': opts.summary = false; break;
      case '--no-table':   opts.table   = false; break;
      case '--triage':     opts.triage  = true; break;
      case '--worst':      opts.worst   = parseInt(args[++i], 10); break;
      case '--out':        opts.out     = args[++i]; break;
      case '--max-eval':   opts.maxEval = parseInt(args[++i], 10); break;
      case '--build':      opts.build   = String(args[++i]); break;
      case '--help': case '-h':
        console.log('Usage: node tools/inspect_trace.js [<game-id>] [--terms] [--worst N] [--depth] [--no-summary] [--no-table] [--triage] [--out <file>]');
        process.exit(0);
    }
  }
  if (!opts.id && !opts.triage) {
    // If no id given, show the most recent trace file
    const files = fs.readdirSync(TRACE_DIR).filter(f => f.endsWith('.json'));
    if (files.length === 0) { console.error('No trace files in', TRACE_DIR); process.exit(1); }
    files.sort((a, b) =>
      fs.statSync(path.join(TRACE_DIR, b)).mtimeMs - fs.statSync(path.join(TRACE_DIR, a)).mtimeMs
    );
    opts.id = files[0].replace('.json', '');
    console.log(`(no id given — showing most recent: ${opts.id})\n`);
  }
  return opts;
}

// ── Load ──────────────────────────────────────────────────────────────────
function load(id) {
  // Accept bare id, id.json, or full path
  let p = id;
  if (!fs.existsSync(p)) p = path.join(TRACE_DIR, id.endsWith('.json') ? id : `${id}.json`);
  if (!fs.existsSync(p)) { console.error('Trace file not found:', id); process.exit(1); }
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}

// ── Helpers ───────────────────────────────────────────────────────────────
function blend(mg, eg, phase) {
  return (mg * phase + eg * (256 - phase)) / 256;
}

function termBlended(vec, key) {
  const pair = vec?.[key];
  if (!pair) return 0;
  return blend(pair[0], pair[1], vec.phase ?? 128);
}

// Our engine's search eval, converted to White POV (eval_cp is STM-relative)
function ourEvalWhitePov(m, gameColor) {
  if (m.eval_cp == null) return null;
  // eval_cp positive = good for the side that just moved
  // if bot is black, after our move it's white's turn, but eval_cp is from bot's (black's) view
  // flip to white pov
  return gameColor === 'black' ? -m.eval_cp : m.eval_cp;
}

// Static eval from eval_vec (already White POV)
function staticEvalWhitePov(m) {
  return m.eval_vec?.total ?? null;
}

// SF eval converted to White POV.
// sf_eval in traces is STM (positive = good for side to move in the stored FEN).
// The FEN is the position AFTER the bot moved, so FEN stm = opponent to move.
// Negate when stm='b' (SF evaluated from black's perspective).
function sfEvalWhitePov(m) {
  if (m.sf_eval == null) return null;
  const fenStm = m.fen?.split(' ')[1] ?? 'w';
  return fenStm === 'b' ? -m.sf_eval : m.sf_eval;
}

function sign(n) { return n >= 0 ? '+' : ''; }
function fmt(n, w = 6) {
  if (n == null) return '?'.padStart(w);
  return (sign(n) + Math.round(n)).padStart(w);
}

// ── Move table ────────────────────────────────────────────────────────────
function printMoveTable(data, opts) {
  const col = data.color;
  const traced = data.moves.filter(m => m.eval_vec);

  // Columns: Ply | Move | Search(W) | Static(W) | SF(W) | Error(search) | Error(static) | Phase | Depth | Stop
  const hdr = 'Ply  Move  Search(W)  Static(W)   SF(W)  Err(srch)  Err(stat)  Ph  D  Stop';
  console.log(hdr);
  console.log('-'.repeat(hdr.length));

  for (const m of data.moves) {
    const search = ourEvalWhitePov(m, col);
    const stat   = staticEvalWhitePov(m);
    const sf     = sfEvalWhitePov(m);        // White POV (converted from STM)
    const eSearch = (search != null && sf != null) ? search - sf : null;
    const eStat   = (stat   != null && sf != null) ? stat   - sf : null;

    process.stdout.write(
      String(m.ply).padStart(3) + '  ' +
      (m.move || '?').padEnd(5) + ' ' +
      fmt(search,  9) + '  ' +
      fmt(stat,    9) + '  ' +
      fmt(sf,      7) + '  ' +
      fmt(eSearch, 9) + '  ' +
      fmt(eStat,   9) + '  ' +
      String(m.eval_vec?.phase ?? '?').padStart(3) + ' ' +
      String(m.depth ?? '?').padStart(2) + '  ' +
      (m.stop_reason ?? '').slice(0, 9) +
      '\n'
    );

    if (opts.depth && m.depth_history?.length) {
      const dh = m.depth_history;
      const pts = dh.map(h => `d${h.d}:${h.cp ?? (h.mate != null ? `M${h.mate}` : '?')}`).join('  ');
      console.log('       depth: ' + pts);
    }
  }

  // Summary stats moved to printStats() — called separately
}

// ── Stats summary ─────────────────────────────────────────────────────────
function printStats(data, opts) {
  const col    = data.color;
  const maxEv  = opts?.maxEval ?? null;
  const filter = m => m.sf_eval != null && m.eval_cp != null
    && (maxEv == null || Math.abs(m.sf_eval) <= maxEv);
  const pts = data.moves.filter(filter);
  if (pts.length === 0) return;
  const mae  = arr => arr.length ? (arr.reduce((s, x) => s + Math.abs(x), 0) / arr.length).toFixed(1) : '?';
  const mean = arr => arr.length ? (arr.reduce((s, x) => s + x, 0) / arr.length).toFixed(1) : '?';
  const searchErrs = pts.map(m => ourEvalWhitePov(m, col) - sfEvalWhitePov(m));
  const statPts    = pts.filter(m => m.eval_vec);
  const statErrs   = statPts.map(m => staticEvalWhitePov(m) - sfEvalWhitePov(m));
  const mgStatErrs = statPts.filter(m => (m.eval_vec.phase ?? 128) > 128).map(m => staticEvalWhitePov(m) - sfEvalWhitePov(m));
  const egStatErrs = statPts.filter(m => (m.eval_vec.phase ?? 128) <= 128).map(m => staticEvalWhitePov(m) - sfEvalWhitePov(m));
  const filtNote   = maxEv != null ? ` [|sf_eval|≤${maxEv}]` : '';
  console.log('');
  console.log(`Search MAE: ${mae(searchErrs)} cp   Mean error: ${mean(searchErrs)} cp   (${pts.length} positions${filtNote})`);
  if (statErrs.length) {
    console.log(`Static MAE: ${mae(statErrs)} cp   Mean error: ${mean(statErrs)} cp   (${statErrs.length} positions with eval_vec)`);
    if (mgStatErrs.length && egStatErrs.length)
      console.log(`  MG static MAE: ${mae(mgStatErrs)} cp (${mgStatErrs.length} pos, phase>128)   EG static MAE: ${mae(egStatErrs)} cp (${egStatErrs.length} pos, phase≤128)`);
  }
}

// ── Worst-error term breakdowns ───────────────────────────────────────────
function printWorstBreakdowns(data, n, opts) {
  const col    = data.color;
  const maxEv  = opts?.maxEval ?? null;
  const candidates = data.moves
    .filter(m => m.eval_vec && m.sf_eval != null
      && (maxEv == null || Math.abs(m.sf_eval) <= maxEv))
    .map(m => ({ ...m, _errStat: staticEvalWhitePov(m) - sfEvalWhitePov(m) }))
    .sort((a, b) => Math.abs(b._errStat) - Math.abs(a._errStat))
    .slice(0, n);

  if (candidates.length === 0) { console.log('(no traced positions with sf_eval)'); return; }

  for (const m of candidates) {
    const v  = m.eval_vec;
    const ph = v.phase ?? 128;
    const search = ourEvalWhitePov(m, col);
    console.log(`\n--- Ply ${m.ply}  ${m.move}  phase=${ph}  depth=${m.depth}  stop=${m.stop_reason ?? '?'}`);
    console.log(`    Search(W): ${fmt(search).trim()}   Static(W): ${fmt(v.total).trim()}   SF(W): ${fmt(sfEvalWhitePov(m)).trim()}`);
    console.log(`    Err(search): ${fmt(search != null ? search - sfEvalWhitePov(m) : null).trim()}   Err(static): ${fmt(m._errStat).trim()}`);
    console.log(`    FEN: ${m.fen ?? '(not captured)'}`);
    console.log('');
    console.log('    Term'.padEnd(26) + '    mg'.padStart(6) + '    eg'.padStart(6) + '  blend'.padStart(8));
    console.log('    ' + '-'.repeat(44));
    for (const key of TERM_KEYS) {
      const pair = v[key];
      if (!pair) continue;
      const bl = blend(pair[0], pair[1], ph);
      if (Math.abs(bl) < 1 && Math.abs(pair[0]) < 1 && Math.abs(pair[1]) < 1) continue;
      console.log(
        '    ' + key.padEnd(22) +
        pair[0].toFixed(0).padStart(8) +
        pair[1].toFixed(0).padStart(6) +
        bl.toFixed(1).padStart(8)
      );
    }
    console.log('    ' + '-'.repeat(44));
    console.log(
      '    ' + 'TOTAL'.padEnd(22) +
      (v.mg ?? '?').toString().padStart(8) +
      (v.eg ?? '?').toString().padStart(6) +
      (v.total ?? '?').toString().padStart(8)
    );
  }
}

// ── Triage: one-line summary per trace file ──────────────────────────────
function runTriage(opts) {
  const maxEv  = opts?.maxEval ?? null;
  const buildF = opts?.build   ?? null;
  const files = fs.readdirSync(TRACE_DIR).filter(f => f.endsWith('.json'));
  if (files.length === 0) { console.log('No trace files.'); return; }
  const rows = [];
  for (const f of files) {
    const data = JSON.parse(fs.readFileSync(path.join(TRACE_DIR, f), 'utf8'));
    if (buildF != null && String(data.build) !== buildF) continue;
    const col  = data.color;
    const outcome = data.result === '1-0' ? (col === 'white' ? 'WIN' : 'LOSS')
                  : data.result === '0-1' ? (col === 'black' ? 'WIN' : 'LOSS')
                  : data.result === '1/2-1/2' ? 'DRAW' : (data.result ?? '*');
    const sfOk = m => m.sf_eval != null && m.eval_cp != null
      && (maxEv == null || Math.abs(m.sf_eval) <= maxEv);
    const pts     = data.moves.filter(sfOk);
    const statPts = pts.filter(m => m.eval_vec);
    const mae = arr => arr.length ? (arr.reduce((s, x) => s + Math.abs(x), 0) / arr.length) : null;
    const searchMAE = mae(pts.map(m => ourEvalWhitePov(m, col) - sfEvalWhitePov(m)));
    const staticMAE = mae(statPts.map(m => staticEvalWhitePov(m) - sfEvalWhitePov(m)));
    rows.push({ id: data.id ?? f.replace('.json',''), outcome, color: col,
                reason: data.reason ?? '?', build: data.build ?? '?',
                moves: data.moves.length, sfCov: pts.length,
                searchMAE, staticMAE });
  }
  rows.sort((a, b) => (b.staticMAE ?? 0) - (a.staticMAE ?? 0));
  const h = 'Game                         Outcome  Color   Reason    Bld  Mv  SF  SrchMAE  StatMAE';
  console.log(h);
  console.log('-'.repeat(h.length));
  for (const r of rows) {
    const sm = r.searchMAE != null ? r.searchMAE.toFixed(0).padStart(7) : '      ?';
    const st = r.staticMAE != null ? r.staticMAE.toFixed(0).padStart(7) : '      ?';
    console.log(
      r.id.padEnd(29) +
      r.outcome.padEnd(9) +
      r.color.padEnd(8) +
      (r.reason ?? '?').slice(0,9).padEnd(10) +
      String(r.build).padStart(3) + ' ' +
      String(r.moves).padStart(3) + ' ' +
      String(r.sfCov).padStart(3) + ' ' +
      sm + '  ' + st
    );
  }
}

// ── Main ──────────────────────────────────────────────────────────────────
function main() {
  const opts = parseArgs();

  if (opts.triage) { runTriage(opts); return; }

  const data = load(opts.id);

  // Redirect output to file if --out specified
  let outStream = null;
  if (opts.out) {
    fs.mkdirSync(path.dirname(path.resolve(opts.out)), { recursive: true });
    outStream = fs.createWriteStream(opts.out, { encoding: 'utf8' });
    const origLog = console.log.bind(console);
    const origWrite = process.stdout.write.bind(process.stdout);
    console.log = (...args) => outStream.write(args.join(' ') + '\n');
    process.stdout.write = (str) => { outStream.write(str); return true; };
    process.on('exit', () => { outStream.end(); console.log = origLog; process.stdout.write = origWrite; });
  }

  if (opts.summary) {
    const traced = data.moves.filter(m => m.eval_vec).length;
    const withSf = data.moves.filter(m => m.sf_eval != null).length;
    console.log(`Game:   ${data.id}`);
    const outcome = data.result === '1-0' ? (data.color === 'white' ? 'WIN' : 'LOSS')
                  : data.result === '0-1' ? (data.color === 'black' ? 'WIN' : 'LOSS')
                  : data.result === '1/2-1/2' ? 'DRAW' : data.result ?? '*';
    console.log(`Color:  ${data.color}   Result: ${data.result ?? '*'} (${outcome})   Reason: ${data.reason ?? '?'}`);
    console.log(`Build:  ${data.build ?? '?'}   Moves: ${data.moves.length}   Traced: ${traced}   With SF eval: ${withSf}`);
    console.log(`FEN:    ${data.initialFen ?? 'startpos'}`);
    console.log('');
    console.log('Columns: Search(W) = search eval White-POV | Static(W) = evalvec.total | SF(W) = SF search White-POV');
    console.log('Error = our eval minus SF eval. Negative = we think we\'re worse off than SF does.');
    console.log('');
  }

  if (opts.table) printMoveTable(data, opts);
  printStats(data, opts);

  if (opts.worst > 0) {
    console.log(`\n${'='.repeat(60)}`);
    console.log(`Worst ${opts.worst} positions by |static error|:`);
    printWorstBreakdowns(data, opts.worst, opts);
  }
}

main();
