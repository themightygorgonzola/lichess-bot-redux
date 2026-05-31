'use strict';

/**
 * config.js — Runtime-readable config + live-patchable policies.
 *
 * Exposes two categories:
 *   1. **env** — read-only snapshot of process.env keys relevant to the bot
 *   2. **policies** — the DEFAULTS object from policies.js (serialisable subset)
 *
 * The dashboard hits `GET /api/config` to snapshot the running config and
 * `POST /api/config` to live-patch policy values.  Changes are persisted to
 * data/dash-state.json via dashState and survive restarts.
 */

const policies = require('./policies');
const personality = require('./personality');

// ── env snapshot ─────────────────────────────────────────────────────────

const ENV_KEYS = [
  'LICHESS_TOKEN',
  'CHESSCOM_TOKEN',
  'SELFPLAY_ENABLED',
  'SELFPLAY_ENGINE',
  'SELFPLAY_SF_THREADS',
  'SELFPLAY_MOVETIME',
  'ENGINE_PATH',
  'ENGINE_AFFINITY_MASK',
  'EVAL_FILE',
  'USE_NNUE',
  'ENGINE_MOVETIME',
  'ENGINE_THREADS',
  'ENGINE_HASH',
  'MAX_CONCURRENT_GAMES',
  'DASHBOARD_PORT',
  'THOUGHTFULNESS',
  'PONDER',
  'PERSONALITY_MODE',
];

function envSnapshot() {
  const snap = {};
  for (const k of ENV_KEYS) {
    const val = process.env[k];
    if (val == null) continue;
    // Mask any token key — show only last 4 chars
    snap[k] = k.endsWith('_TOKEN') ? `***${val.slice(-4)}` : val;
  }
  return snap;
}

// ── serialisable policy snapshot ─────────────────────────────────────────

/**
 * Build a plain-object snapshot of the *effective* policy values (DEFAULTS
 * merged with any live overrides) suitable for JSON serialisation.
 * Sets are converted to arrays, functions are omitted.
 */
function policySnapshot() {
  const d         = policies.DEFAULTS;
  const overrides = policies.getOverrides ? policies.getOverrides() : {};
  const snap = {};
  for (const [k, v] of Object.entries(d)) {
    if (typeof v === 'function') continue;
    // Prefer live override value; fall back to default.
    const effective = (k in overrides) ? overrides[k] : v;
    snap[k] = effective instanceof Set ? [...effective] : effective;
  }
  return snap;
}

// ── personality summary ──────────────────────────────────────────────────

function personalitySummary() {
  // Return pool names + counts (don't leak the actual quotes)
  const pools = personality.poolNames ? personality.poolNames() : [];
  return { mode: personality.getMode(), pools };
}

// ── combined getter ──────────────────────────────────────────────────────

function getConfig() {
  return {
    env:         envSnapshot(),
    policies:    policySnapshot(),
    personality: personalitySummary(),
  };
}

module.exports = { getConfig, envSnapshot, policySnapshot, personalitySummary };
