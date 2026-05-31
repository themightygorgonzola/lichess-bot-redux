'use strict';

/**
 * logBus.js — Structured log interceptor.
 *
 * Patches `console.log`, `console.warn`, and `console.error` so every
 * message also emits a `log` SSE event to connected dashboard clients.
 *
 * Usage:
 *   require('./logBus');          // just import once at startup
 *   // all future console.* calls flow to the dashboard automatically
 *
 * Optionally tag a message with a gameId by using the conventional
 * prefix format `[game <id>]`.  The bus parses it out automatically.
 */

const store = require('./store');

const GAME_TAG_RE = /^\[game\s+([^\]]+)\]/;

const _origLog   = console.log.bind(console);
const _origWarn  = console.warn.bind(console);
const _origError = console.error.bind(console);

function intercept(level, origFn, args) {
  // Call original first (always)
  origFn(...args);

  // Build the log message
  const msg = args
    .map(a => (typeof a === 'string' ? a : JSON.stringify(a)))
    .join(' ');

  // Try to extract a gameId from the conventional [game <id>] tag
  let gameId = null;
  const m = msg.match(GAME_TAG_RE);
  if (m) gameId = m[1];

  store.emit('log', { level, msg, ts: Date.now(), gameId });
}

console.log   = (...args) => intercept('info',  _origLog,   args);
console.warn  = (...args) => intercept('warn',  _origWarn,  args);
console.error = (...args) => intercept('error', _origError, args);

module.exports = {
  /** Restore original console functions (useful for tests). */
  restore() {
    console.log   = _origLog;
    console.warn  = _origWarn;
    console.error = _origError;
  },
};
