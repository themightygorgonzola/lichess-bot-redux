'use strict';

/**
 * services/index.js — Service registry and factory.
 *
 * Discovers which chess services are configured (have tokens in .env),
 * instantiates the appropriate adapters, and exports them.
 *
 * Usage:
 *   const { getServices, getService } = require('./services');
 *   const services = getServices();          // [LichessAdapter, ...]
 *   const lichess  = getService('lichess');   // or null
 */

const LichessAdapter   = require('./LichessAdapter');
const ChessComAdapter  = require('./ChessComAdapter');
const SelfPlayAdapter  = require('./SelfPlayAdapter');

// Registry of all known adapters and their .env token keys.
// For SelfPlay, the key is a boolean flag rather than a real auth token;
// the adapter ignores the token value entirely.
// SelfPlay is listed first so it is always the baseline service even when
// online tokens are missing — this prevents the seek tab from breaking.
const ADAPTERS = [
  { key: 'SELFPLAY_ENABLED', AdapterClass: SelfPlayAdapter },
  { key: 'LICHESS_TOKEN',   AdapterClass: LichessAdapter },
  { key: 'CHESSCOM_TOKEN',  AdapterClass: ChessComAdapter },
];

/** Cached adapter instances (created once) */
const _instances = new Map();

/**
 * Return all service adapters that have a configured token.
 * Lazily instantiates adapters on first call.
 * @returns {import('./ServiceAdapter')[]}
 */
function getServices() {
  const result = [];
  for (const { key, AdapterClass } of ADAPTERS) {
    const token = process.env[key];
    if (!token) continue;

    if (!_instances.has(key)) {
      _instances.set(key, new AdapterClass());
    }
    result.push({ adapter: _instances.get(key), token });
  }
  return result;
}

/**
 * Get a specific service adapter by id.
 * @param {'lichess'|'chesscom'} serviceId
 * @returns {{ adapter: import('./ServiceAdapter'), token: string } | null}
 */
function getService(serviceId) {
  return getServices().find(s => s.adapter.id() === serviceId) ?? null;
}

/**
 * Get the token key name for a service id.
 * @param {'lichess'|'chesscom'} serviceId
 * @returns {string|null}
 */
function getTokenKey(serviceId) {
  const entry = ADAPTERS.find(a => {
    const inst = new a.AdapterClass();
    return inst.id() === serviceId;
  });
  return entry?.key ?? null;
}

module.exports = { getServices, getService, getTokenKey, ADAPTERS };
