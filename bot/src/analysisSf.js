'use strict';
/**
 * analysisSf.js — Singleton background Stockfish analysis engine.
 *
 * Spawns one SF process (lazily) for post-move position evaluation that runs
 * concurrently with pondering. Queries are serialised to keep SF load low.
 *
 * Usage:
 *   const analysisSf = require('./analysisSf');
 *   const p = analysisSf.queryEval(fen);   // fire-and-forget or await
 *   // When resolved: { eval_cp, depth } or null on error/timeout
 */

const path   = require('path');
const { Engine } = require('./engine');

// SF binary — same resolution logic as SelfPlayAdapter
const root  = path.resolve(__dirname, '..', '..'); // workspace root
const SF_PATH = process.env.SELFPLAY_ENGINE
  ?? path.join(root, 'engines', 'stockfish-17.1', 'stockfish',
               'stockfish-windows-x86-64-avx2.exe');

const ANALYSIS_THREADS = 2;
const ANALYSIS_HASH    = 64;   // MB — small, no TT reuse across positions
const ANALYSIS_MOVETIME_MS = 150;  // enough for accurate eval at depth ~20 on this machine
const INIT_TIMEOUT_MS  = 5000;

let _engine    = null;   // Engine instance once initialised
let _ready     = false;  // true once 'uciok' + 'readyok' received
let _initErr   = false;  // true if init failed — disables the module

// Serialisation queue: one pending query at a time
let _busy      = false;
const _queue   = [];     // Array of { fen, resolve, reject }

// ── Init ──────────────────────────────────────────────────────────────────

async function _init() {
  if (_engine || _initErr) return;
  try {
    _engine = new Engine(SF_PATH, { threads: ANALYSIS_THREADS, hash: ANALYSIS_HASH });
    await Promise.race([
      _engine.init(),
      new Promise((_, rej) => setTimeout(() => rej(new Error('SF analysis init timeout')), INIT_TIMEOUT_MS)),
    ]);
    _ready = true;
    console.log('[analysisSf] ready');
  } catch (err) {
    console.warn('[analysisSf] init failed (analysis disabled):', err.message ?? err);
    _engine  = null;
    _initErr = true;
  }
}

// ── Drain queue ───────────────────────────────────────────────────────────

async function _drainNext() {
  if (_busy || _queue.length === 0 || !_ready) return;
  _busy = true;
  const { fen, resolve } = _queue.shift();
  try {
    const result = await _engine.thinkDynamic(fen, [], {
      maxTimeMs: ANALYSIS_MOVETIME_MS,
      onInfo: () => ({}),
    });
    resolve(result ? { eval_cp: result.eval_cp ?? null, depth: result.depth ?? null } : null);
  } catch (err) {
    console.warn('[analysisSf] query failed (ignored):', err.message ?? err);
    resolve(null);
  } finally {
    _busy = false;
    if (_queue.length > 0) _drainNext();
  }
}

// ── Public API ────────────────────────────────────────────────────────────

/**
 * Queue a background SF eval of the given FEN.
 * Returns a Promise<{eval_cp, depth}|null>.
 * Never rejects — errors resolve to null.
 */
function queryEval(fen) {
  if (_initErr) return Promise.resolve(null);

  return new Promise(resolve => {
    _queue.push({ fen, resolve });

    if (!_engine) {
      // First call — initialise SF then drain
      _init().then(() => _drainNext()).catch(() => {
        // flush all queued items with null
        while (_queue.length) _queue.shift().resolve(null);
      });
    } else {
      _drainNext();
    }
  });
}

/**
 * How many queries are waiting.
 */
function pendingCount() {
  return _queue.length + (_busy ? 1 : 0);
}

/**
 * Wait for all in-flight and queued queries to complete.
 * Useful before saving game records — pass a timeout to avoid blocking too long.
 */
function drain(timeoutMs = 3000) {
  if (pendingCount() === 0) return Promise.resolve();
  return new Promise(resolve => {
    const deadline = setTimeout(resolve, timeoutMs);
    const check = () => {
      if (pendingCount() === 0) { clearTimeout(deadline); resolve(); }
      else setTimeout(check, 20);
    };
    check();
  });
}

module.exports = { queryEval, drain, pendingCount };
