'use strict';

/**
 * book.js — Opening book via the Lichess Masters explorer API.
 *
 * At startup, `prewarm()` walks the most popular master-game lines via BFS,
 * caching every reachable position up to PREWARM_PLY deep.  After that,
 * `probe()` is a synchronous cache lookup — zero latency on our turn.
 *
 * For positions outside the prewarmed tree (rare sidelines) probe() falls
 * back to a live fetch, same as before.
 *
 * Cache stores the full candidates array per FEN so weighted-random
 * selection happens fresh each call (variety between games on same line).
 *
 * Usage:
 *   const book = require('./book');
 *   book.prewarm(token);                   // fire-and-forget at startup
 *   const move = await book.probe(fen, ply, token);  // null if out of book
 */

const https   = require('https');
const fs      = require('fs');
const path    = require('path');
const { applyMoves, STARTING_FEN } = require('./fen');

// ── Tunables ────────────────────────────────────────────────────────────────

const MAX_PLY           = 30;   // stop consulting book after this ply
const MIN_GAMES         = 100;  // minimum master games for a move to be played
const TOP_N             = 5;    // max candidates to choose from per position
const API_TIMEOUT       = 2000; // ms per request

// Prewarm BFS settings
const PREWARM_PLY       = 28;   // how deep to walk the tree
const PREWARM_BRANCHES  = 3;    // follow only the top N moves at each node
const PREWARM_MIN_GAMES = 500;  // only follow moves with ≥ this many games
const PREWARM_CONCURR   = 4;    // parallel requests during prewarm
const PREWARM_DELAY_MS  = 50;   // ms between request batches (be nice to API)

// Persistent cache file — grows over time as new positions are encountered
const CACHE_FILE        = path.resolve(__dirname, '../data/book-cache.json');
require('fs').mkdirSync(require('path').dirname(CACHE_FILE), { recursive: true });
const SAVE_DEBOUNCE_MS  = 5_000;  // coalesce rapid live-fetch saves into one write

// ── In-memory cache: FEN → [{uci,total}] | null ─────────────────────────────
// null  = confirmed out-of-book (no qualifying moves)
// array = qualifying candidates (may be empty after MIN_GAMES filter)
const _cache = new Map();
let   _prewarmed  = false;
let   _saveTimer  = null;
// Real Lichess API token — set once at startup via setToken().
// All API calls use this regardless of which service's game triggered them
// (e.g. self-play passes 'true' as its token, not a real Lichess token).
let   _token = null;

// ── Persistent cache I/O ─────────────────────────────────────────────────────

/**
 * Load cache from disk into memory.  Safe to call at any time; silently
 * ignores missing or corrupt files.
 */
function loadCache() {
  try {
    const raw = fs.readFileSync(CACHE_FILE, 'utf8');
    const obj = JSON.parse(raw);   // { fen: candidates[] }  — nulls are never written
    let n = 0;
    for (const [fen, candidates] of Object.entries(obj)) {
      if (!_cache.has(fen)) { _cache.set(fen, candidates); n++; }
    }
    if (n > 0) console.log(`[book] loaded ${n} positions from cache file (${_cache.size} total)`);
  } catch (e) {
    if (e.code !== 'ENOENT') console.warn('[book] cache load error (ignored):', e.message);
  }
}

/** Serialize and write only non-null entries to disk. */
function saveCache() {
  _saveTimer = null;
  try {
    const obj = {};
    for (const [fen, v] of _cache) {
      if (v !== null) obj[fen] = v;   // never persist nulls — they're session-only
    }
    fs.mkdirSync(path.dirname(CACHE_FILE), { recursive: true });
    fs.writeFileSync(CACHE_FILE, JSON.stringify(obj), 'utf8');
  } catch (e) {
    console.warn('[book] cache save error (ignored):', e.message);
  }
}

/** Schedule a debounced save — coalesces many rapid writes into one. */
function _scheduleSave() {
  if (_saveTimer) return;
  _saveTimer = setTimeout(saveCache, SAVE_DEBOUNCE_MS);
}

// ── HTTP helper ─────────────────────────────────────────────────────────────
function _fetchJson(url, token) {
  return new Promise((resolve, reject) => {
    const opts = {
      timeout: API_TIMEOUT,
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    };
    const req = https.get(url, opts, (res) => {
      if (res.statusCode !== 200) {
        res.resume();
        return reject(new Error(`HTTP ${res.statusCode}`));
      }
      let body = '';
      res.on('data', chunk => { body += chunk; });
      res.on('end', () => {
        try { resolve(JSON.parse(body)); }
        catch (e) { reject(e); }
      });
    });
    req.on('timeout', () => { req.destroy(); reject(new Error('timeout')); });
    req.on('error', reject);
  });
}

// ── Move helpers ─────────────────────────────────────────────────────────────

/** Convert raw API moves array to sorted candidates [{uci, total}]. */
function _toCandidates(apiMoves, minGames = MIN_GAMES) {
  return apiMoves
    .map(m => ({ uci: m.uci, total: (m.white ?? 0) + (m.draws ?? 0) + (m.black ?? 0) }))
    .sort((a, b) => b.total - a.total)
    .slice(0, TOP_N)
    .filter(m => m.total >= minGames);
}

/** Weighted-random selection from a candidates array. */
function _selectMove(candidates) {
  if (!candidates || candidates.length === 0) return null;
  const totalWeight = candidates.reduce((s, m) => s + m.total, 0);
  let r = Math.random() * totalWeight;
  for (const m of candidates) {
    r -= m.total;
    if (r <= 0) return m.uci;
  }
  return candidates[0].uci;   // floating-point fallback
}

// ── In-flight fetch tracking — prevents duplicate concurrent requests ───────
const _inflight = new Set();

/** Fetch raw API data and populate cache for this FEN. Returns candidates or null. */
async function _fetchAndCache(fen, token) {
  token = _token ?? token;  // prefer stored Lichess token over caller's service token
  if (_inflight.has(fen)) return _cache.get(fen) ?? null;
  _inflight.add(fen);
  try {
    const encoded = encodeURIComponent(fen);
    const url = `https://explorer.lichess.ovh/masters?fen=${encoded}&moves=10&topGames=0&recentGames=0`;
    const data = await _fetchJson(url, token);
    const candidates = _toCandidates(data.moves ?? []);
    const value = candidates.length > 0 ? candidates : null;
    _cache.set(fen, value);
    if (value !== null) _scheduleSave();   // only persist in-book positions
    return value;
  } catch (err) {
    // Don't cache failures — transient error shouldn't permanently disable a position.
    return null;
  } finally {
    _inflight.delete(fen);
  }
}

/**
 * Speculatively prefetch the given position and its top children into cache.
 * Fire-and-forget: call during opponent's clock with no await.
 *
 * Fetches `fen` (if not already cached), then fans out to each candidate
 * child position in parallel.  By the time the opponent moves, both the
 * resulting position and its own children are already warm.
 *
 * @param {string} fen   Position to prefetch from (opponent's turn)
 * @param {number} ply   Current ply (stops work above MAX_PLY)
 * @param {string} token Lichess API token
 */
async function prefetch(fen, ply, token) {
  if (ply > MAX_PLY) return;

  // Fetch the given position if not already cached/in-flight
  let candidates = _cache.get(fen);
  if (candidates === undefined) {
    candidates = await _fetchAndCache(fen, token);
  }

  // Fan out to each candidate child (one more ply deep)
  if (!candidates || ply + 1 > MAX_PLY) return;
  await Promise.all(
    candidates.map(async ({ uci }) => {
      try {
        const childFen = applyMoves(fen, [uci]);
        if (!_cache.has(childFen) && !_inflight.has(childFen)) {
          await _fetchAndCache(childFen, token);
        }
      } catch (_) { /* invalid move — skip */ }
    })
  );
}

// ── Public API ───────────────────────────────────────────────────────────────

/**
 * @param {string} fen    Current position FEN
 * @param {number} ply    Half-move count (0 = before white's first move)
 * @param {string} [token]  Lichess API token
 * @returns {Promise<string|null>}  UCI move, or null to fall through to engine
 */
async function probe(fen, ply, token) {
  if (ply > MAX_PLY) return null;

  if (_cache.has(fen)) {
    return _selectMove(_cache.get(fen));  // synchronous — null if out-of-book
  }

  // Cache miss: live fetch (should be rare after prewarm)
  if (_prewarmed) {
    console.warn(`[book] cache miss at ply=${ply} — position outside prewarmed tree`);
  }
  const candidates = await _fetchAndCache(fen, token);
  return _selectMove(candidates);
}

/**
 * Pre-warm the cache by BFS-walking the master-game opening tree.
 * Fire-and-forget: call at startup, does not block the bot.
 *
 * Walks up to PREWARM_PLY deep, following the top PREWARM_BRANCHES moves
 * at each node (by master game count).  Stops a branch when moves fall
 * below PREWARM_MIN_GAMES — that position gets cached as null (out of book).
 *
 * @param {string} token  Lichess API token
 */
async function prewarm(token) {
  const t0 = Date.now();
  console.log('[book] prewarming opening tree...');

  // BFS queue: {fen, uciMoves, ply}
  const queue  = [{ fen: STARTING_FEN, uciMoves: [], ply: 0 }];
  const queued = new Set([STARTING_FEN]);
  let   fetched = 0;

  while (queue.length > 0) {
    // Take a batch of PREWARM_CONCURR items
    const batch = queue.splice(0, PREWARM_CONCURR);

    await Promise.all(batch.map(async ({ fen, uciMoves, ply }) => {
      if (_cache.has(fen)) return;  // already fetched (convergent lines)

      try {
        const encoded = encodeURIComponent(fen);
        const url = `https://explorer.lichess.ovh/masters?fen=${encoded}&moves=10&topGames=0&recentGames=0`;
        const data = await _fetchJson(url, token);
        fetched++;
        if (fetched % 50 === 0) {
          const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
          console.log(`[book] prewarm: ${fetched} fetched, ${queued.size} queued, ${elapsed}s elapsed`);
        }

        // Cache with normal MIN_GAMES threshold for probe()
        const playCandidates = _toCandidates(data.moves ?? [], MIN_GAMES);
        _cache.set(fen, playCandidates.length > 0 ? playCandidates : null);

        // Enqueue children using the stricter PREWARM threshold
        if (ply < PREWARM_PLY) {
          const children = _toCandidates(data.moves ?? [], PREWARM_MIN_GAMES)
            .slice(0, PREWARM_BRANCHES);
          for (const { uci } of children) {
            try {
              const childMoves = [...uciMoves, uci];
              const childFen   = applyMoves(STARTING_FEN, childMoves);
              if (!queued.has(childFen)) {
                queued.add(childFen);
                queue.push({ fen: childFen, uciMoves: childMoves, ply: ply + 1 });
              }
            } catch (_) { /* invalid move in API data — skip */ }
          }
        }
      } catch (err) {
        // Ignore individual fetch failures during prewarm
      }
    }));

    if (queue.length > 0 && PREWARM_DELAY_MS > 0) {
      await new Promise(r => setTimeout(r, PREWARM_DELAY_MS));
    }
  }

  _prewarmed = true;
  const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
  console.log(`[book] prewarm complete: ${fetched} positions fetched, ${_cache.size} cached in ${elapsed}s`);
  saveCache();  // immediate save after full prewarm (not debounced)
}

/** Evict the entire cache. */
function clearCache() { _cache.clear(); _prewarmed = false; }

/** How many FENs are currently cached. */
function cacheSize() { return _cache.size; }

/** True once prewarm() has finished. */
function isPrewarmed() { return _prewarmed; }

/** Store the real Lichess API token used for all explorer API calls. */
function setToken(token) { _token = token || null; }

module.exports = { probe, prefetch, prewarm, loadCache, saveCache, clearCache, cacheSize, isPrewarmed, setToken };
