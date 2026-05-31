'use strict';

/**
 * dashState.js — Dashboard control-state persistence.
 *
 * Reads / writes a JSON file that holds all dashboard settings that
 * should survive process restarts:
 *
 *   data/dash-state.json
 *   └─ {
 *        "policies":    { "thoughtfulness": 0.7, "evalInfluence": 0.5, ... },
 *        "personality": { "mode": "full" }
 *      }
 *
 * The file is written atomically (write tmp → rename) so a crash mid-write
 * never corrupts the previous good copy.
 *
 * Public API:
 *   dashState.applyOnStartup()        // call once from index.js after all modules loaded
 *   dashState.save('policies', patch) // merge patch into a section and persist
 *   dashState.save('personality', { mode })
 *   dashState.get('policies')         // snapshot of the named section (read-only copy)
 *   dashState.get()                   // snapshot of the full state
 */

const fs   = require('fs');
const path = require('path');

// ── Paths ────────────────────────────────────────────────────────────────────

// bot/data/ lives one level up from bot/src/
const DATA_DIR   = path.join(__dirname, '..', 'data');
const STATE_FILE = path.join(DATA_DIR, 'dash-state.json');
const TMP_FILE   = STATE_FILE + '.tmp';
require('fs').mkdirSync(DATA_DIR, { recursive: true });

// ── In-memory copy ───────────────────────────────────────────────────────────

/** @type {{ policies: Record<string,any>, personality: Record<string,any>, selfplayOdds: Record<string,number>, selfplayConfig: Record<string,any> }} */
const _state = {
  policies:       {},
  personality:    {},
  selfplayOdds:   {},
  selfplayConfig: {
    enabledEngines: [],
    enabledPools:   ['startpos'],
    mode:           'loop',
    tc:             { initial: 60000, increment: 2000 },
    ponder:         false,
  },
};

// ── Helpers ───────────────────────────────────────────────────────────────────

/**
 * Load the state file synchronously.  Called once at startup before any async
 * work begins.  Silently ignores a missing file (first run) but warns on JSON
 * parse errors or I/O failures so the operator is aware.
 */
function _loadSync() {
  try {
    const raw    = fs.readFileSync(STATE_FILE, 'utf8');
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === 'object') {
      if (parsed.policies    && typeof parsed.policies    === 'object')
        Object.assign(_state.policies,    parsed.policies);
      if (parsed.personality && typeof parsed.personality === 'object')
        Object.assign(_state.personality, parsed.personality);
      if (parsed.selfplayOdds && typeof parsed.selfplayOdds === 'object')
        Object.assign(_state.selfplayOdds, parsed.selfplayOdds);
      if (parsed.selfplayConfig && typeof parsed.selfplayConfig === 'object')
        Object.assign(_state.selfplayConfig, parsed.selfplayConfig);
    }
    console.log(`[dashState] Loaded from ${STATE_FILE}`);
  } catch (err) {
    if (err.code !== 'ENOENT') {
      console.warn(`[dashState] Could not load state file — using defaults (${err.message})`);
    }
  }
}

/**
 * Write the full _state to disk atomically (tmp → rename).
 * Errors are logged but never thrown — a failed write should not crash the bot.
 */
function _persistAsync() {
  const json = JSON.stringify(_state, null, 2);
  // Ensure the data directory exists, then write atomically.
  fs.promises.mkdir(DATA_DIR, { recursive: true })
    .then(() => fs.promises.writeFile(TMP_FILE, json, 'utf8'))
    .then(() => fs.promises.rename(TMP_FILE, STATE_FILE))
    .catch(err => console.error(`[dashState] Persist failed: ${err.message}`));
}

// ── Public API ────────────────────────────────────────────────────────────────

/**
 * Return a shallow copy of a named section, or the full state if no section
 * is specified.  Callers must not mutate the returned object.
 *
 * @param {string} [section] 'policies' | 'personality'
 * @returns {object}
 */
function get(section) {
  if (!section) return {
    policies:       { ..._state.policies },
    personality:    { ..._state.personality },
    selfplayOdds:   { ..._state.selfplayOdds },
    selfplayConfig: { ..._state.selfplayConfig },
  };
  return { ...(_state[section] ?? {}) };
}

/**
 * Merge a patch into a named section and persist the full state to disk.
 *
 * @param {string} section  'policies' | 'personality'
 * @param {object} patch    Flat key→value map.  Only the provided keys are
 *                          updated; existing keys in the section are kept.
 */
function save(section, patch) {
  if (!_state[section]) _state[section] = {};
  Object.assign(_state[section], patch);
  _persistAsync();
}

/**
 * Load persisted state and apply it to live modules.
 *
 * Must be called **once** from index.js, after all modules have been required,
 * to avoid circular-dependency issues.  It is safe to call it synchronously
 * during startup because it only reads and then delegates to module APIs.
 */
function applyOnStartup() {
  _loadSync();

  // ── Restore policy overrides ────────────────────────────────────────────
  const savedPolicies = _state.policies;
  if (Object.keys(savedPolicies).length > 0) {
    const policies = require('./policies');
    let count = 0;
    for (const [key, val] of Object.entries(savedPolicies)) {
      if (!(key in policies.DEFAULTS)) continue;
      const t = typeof val;
      if (t !== 'number' && t !== 'boolean') continue;
      if (typeof policies.override === 'function') {
        policies.override(key, val);
        count++;
      }
    }
    if (count > 0) console.log(`[dashState] Restored ${count} policy override(s)`);
  }

  // ── Restore personality mode ────────────────────────────────────────────
  const { mode } = _state.personality;
  if (mode === 'full' || mode === 'silent') {
    const personality = require('./personality');
    personality.setMode(mode);
    console.log(`[dashState] Restored personality mode: ${mode}`);
  }
}

module.exports = { get, save, applyOnStartup };
