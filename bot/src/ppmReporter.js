'use strict';

/**
 * ppmReporter.js — PPM integration for the lichess bot
 *
 * Registers the bot as a project with the PPM controller, sends periodic
 * heartbeats with session stats, and pushes real-time status updates on
 * game_start / game_end events.
 *
 * Entirely opt-in: if CONTROLLER_URL is not set in bot/.env, this module
 * is a silent no-op. No changes needed to existing bot code beyond two lines
 * in index.js:
 *
 *   const ppm = require('./src/ppmReporter');
 *   ppm.init(store);
 *
 * Env vars (bot/.env):
 *   CONTROLLER_URL=http://localhost:7000   — PPM controller base URL
 *   PPM_BOT_NAME=lichess-bot              — project name in PPM (optional)
 */

const axios = require('axios');
const os    = require('os');

const CONTROLLER_URL   = process.env.CONTROLLER_URL?.replace(/\/$/, '');
const PROJECT_NAME     = process.env.PPM_BOT_NAME || 'lichess-bot';
const PPM_API_KEY      = process.env.PPM_API_KEY || null;
const INSTANCE_ID      = process.env.PPM_INSTANCE_ID || null;
const HEARTBEAT_MS     = 30_000;
const COMMAND_POLL_MS  = 5_000;

// Engine resource commands the PPM dashboard can dispatch remotely.
// Useful when deploying to machines with different CPU/RAM constraints.
const CUSTOM_COMMANDS = [
  { action: 'set-threads',     label: 'Set Engine Threads',    emoji: '🧵', confirmPrompt: 'Enter thread count (e.g. 4)' },
  { action: 'set-hash',        label: 'Set Hash Size (MB)',     emoji: '💾', confirmPrompt: 'Enter hash size in MB (e.g. 256)' },
  { action: 'set-movetime',    label: 'Set Movetime (ms)',      emoji: '⏱️', confirmPrompt: 'Enter movetime per move in ms (e.g. 1000)' },
  { action: 'set-max-games',   label: 'Set Max Concurrent',     emoji: '🎮', confirmPrompt: 'Enter max concurrent games (e.g. 2)' },
  { action: 'set-personality', label: 'Set Personality Mode',   emoji: '🎭', confirmPrompt: 'Enter mode: full or silent' },
];

// ── Module state ─────────────────────────────────────────────────────────────

let projectId  = null;
let apiToken   = null;
let heartbeatTimer    = null;
let commandPollTimer  = null;
let commandHandlers   = {};
let _store            = null;  // set in init(), used by immediate heartbeat after game end
let _engineConfig     = null;  // set in init(), read by heartbeat for engine metrics
const startedAt = Date.now();

// Seeded from gameDb all-time stats at init — incremented as new games finish.
// This ensures W/L/D/Games always show lifetime totals, not session-only.
const session = { wins: 0, losses: 0, draws: 0 };

// Load all-time baseline from the persistent game database.
// Called once at init() before the heartbeat loop starts.
function _seedFromGameDb() {
  try {
    const gameDb = require('./gameDb');
    const stats  = gameDb.getStats();
    session.wins   = stats.botWins   ?? 0;
    session.losses = stats.botLosses ?? 0;
    session.draws  = stats.botDraws  ?? 0;
    console.log(`[ppm] All-time stats loaded: ${session.wins}W ${session.draws}D ${session.losses}L`);
  } catch (err) {
    console.warn(`[ppm] Could not seed stats from gameDb: ${err.message}`);
  }
}

// ── Auth helpers ──────────────────────────────────────────────────────────────

/** Headers for project-scoped write operations (heartbeat, status, logs). */
function _authHeaders() {
  if (!projectId || !apiToken) return {};
  return { 'X-Project-Id': projectId, 'X-Api-Token': apiToken };
}

// ── Public API ────────────────────────────────────────────────────────────────

/**
 * Call once at startup, passing the store module and optional command handlers.
 * @param {object} store
 * @param {Record<string, (payload: object) => Promise<{success:boolean,message:string}>>} handlers
 * @param {object} [engineConfig]  live engine config object (read each heartbeat)
 */
async function init(store, handlers = {}, engineConfig = null) {
  if (!CONTROLLER_URL) return; // opt-out: env var not set

  _store        = store;
  _engineConfig = engineConfig;

  // Seed W/L/D counters from persisted game history so they show all-time totals
  _seedFromGameDb();

  console.log(`[ppm] Connecting to controller at ${CONTROLLER_URL}`);

  // Register (or reconnect) with the controller
  try {
    const res = await axios.post(`${CONTROLLER_URL}/api/projects/register`, {
      name:          PROJECT_NAME,
      version:       require('../package.json').version || '1.0.0',
      tags:          ['chess-bot', 'lichess'],
      capabilities:  ['play', 'analysis'],
      source:        'sdk',
      instanceId:    INSTANCE_ID,
      customCommands: CUSTOM_COMMANDS,
      metricSchema: [
        { key: 'activeGames', label: 'Active',    type: 'gauge',   unit: '' },
        { key: 'wins',        label: 'W',         type: 'counter', unit: '' },
        { key: 'losses',      label: 'L',         type: 'counter', unit: '' },
        { key: 'draws',       label: 'D',         type: 'counter', unit: '' },
        { key: 'total',       label: 'Games',     type: 'counter', unit: '' },
        { key: 'movetime',    label: 'Movetime',  type: 'gauge',   unit: 'ms' },
        { key: 'threads',     label: 'Threads',   type: 'gauge',   unit: '' },
        { key: 'hashMB',      label: 'Hash',      type: 'gauge',   unit: 'MB' },
        { key: 'uptimeSec',   label: 'Uptime',    type: 'gauge',   unit: 's' }
      ]
    }, { timeout: 10_000 });

    projectId = res.data.id;
    apiToken  = res.data.apiToken;
    console.log(`[ppm] Registered — projectId=${projectId}`);
  } catch (err) {
    if (err.response?.status === 409) {
      // Already registered from a previous run — reconnect using existing ID
      projectId = err.response.data?.existingProject?.id;
      apiToken  = err.response.data?.existingProject?.apiToken;
      console.log(`[ppm] Reconnected — projectId=${projectId}`);
      // Push updated commands + schema in case they changed since first registration
      axios.patch(`${CONTROLLER_URL}/api/projects/${projectId}`, {
        customCommands: CUSTOM_COMMANDS,
        metricSchema: [
          { key: 'activeGames', label: 'Active',    type: 'gauge',   unit: '' },
          { key: 'wins',        label: 'W',         type: 'counter', unit: '' },
          { key: 'losses',      label: 'L',         type: 'counter', unit: '' },
          { key: 'draws',       label: 'D',         type: 'counter', unit: '' },
          { key: 'total',       label: 'Games',     type: 'counter', unit: '' },
          { key: 'movetime',    label: 'Movetime',  type: 'gauge',   unit: 'ms' },
          { key: 'threads',     label: 'Threads',   type: 'gauge',   unit: '' },
          { key: 'hashMB',      label: 'Hash',      type: 'gauge',   unit: 'MB' },
          { key: 'uptimeSec',   label: 'Uptime',    type: 'gauge',   unit: 's' }
        ]
      }, { timeout: 5_000 }).catch(() => {});
    } else {
      console.warn(`[ppm] Registration failed: ${err.message} — running without PPM`);
      return;
    }
  }

  // Merge caller-supplied command handlers
  commandHandlers = { ...handlers };

  // Wrap store.emit to intercept game lifecycle events
  _hookStore(store);

  // Start heartbeat loop
  _sendHeartbeat(store);
  heartbeatTimer = setInterval(() => _sendHeartbeat(store), HEARTBEAT_MS);

  // Start command poll loop
  commandPollTimer = setInterval(_pollCommands, COMMAND_POLL_MS);

  // Clean shutdown
  process.on('SIGINT',  _shutdown);
  process.on('SIGTERM', _shutdown);
}

// ── Internal helpers ──────────────────────────────────────────────────────────

function _hookStore(store) {
  const originalEmit = store.emit.bind(store);

  store.emit = function ppmWrappedEmit(type, data) {
    // Always call the original first
    originalEmit(type, data);

    if (!projectId) return;

    if (type === 'game_start') {
      _onGameStart(data);
    } else if (type === 'game_end') {
      _onGameEnd(data, store);
    }
  };
}

function _onGameStart(data) {
  const game = data; // { gameId, color, opponentName, speed, variant, rated, ... }
  _postStatus({
    label: 'Game started',
    severity: 'info',
    data: {
      gameId:       game.gameId,
      opponent:     game.opponentName || game.opponentId || '?',
      color:        game.color,
      speed:        game.speed,
      variant:      game.variant,
      rated:        game.rated
    }
  }).catch(() => {});
}

function _onGameEnd(data, store) {
  const { gameId, result, reason, durationMs } = data;
  const game = store.getGame?.(gameId);

  // Track session stats
  if (result === '1-0' || result === '0-1') {
    const ourColor = game?.color;
    const weWon = (result === '1-0' && ourColor === 'white') ||
                  (result === '0-1' && ourColor === 'black');
    if (weWon) session.wins++;
    else       session.losses++;
  } else if (result === '1/2-1/2') {
    session.draws++;
  }

  // Send immediate heartbeat after result so counters update now, not in 30s
  _sendHeartbeat(_store).catch(() => {});

  const weWon = (() => {
    if (!game) return null;
    return (result === '1-0' && game.color === 'white') ||
           (result === '0-1' && game.color === 'black');
  })();

  _postStatus({
    label:    `Game ended — ${result ?? 'unknown'}`,
    severity: weWon === true ? 'info' : weWon === false ? 'warn' : 'info',
    data: {
      gameId,
      result,
      reason,
      opponent:    game?.opponentName || game?.opponentId || '?',
      color:       game?.color,
      movesPlayed: game?.fullMoves?.length ?? 0,
      durationSec: durationMs != null ? Math.round(durationMs / 1000) : null,
      session:     { ...session }
    }
  }).catch(() => {});
}

async function _pollCommands() {
  if (!projectId) return;
  let commands;
  try {
    const res = await axios.get(
      `${CONTROLLER_URL}/api/projects/${projectId}/commands`,
      { params: { status: 'pending' }, timeout: 5_000, headers: _authHeaders() }
    );
    commands = res.data?.value ?? [];
  } catch (_) { return; }

  for (const cmd of commands) {
    // Ack first so the dashboard shows it's being handled
    try {
      await axios.post(
        `${CONTROLLER_URL}/api/projects/${projectId}/commands/${cmd.id}/ack`,
        {}, { timeout: 3_000, headers: _authHeaders() }
      );
    } catch (_) {}

    let result = { success: false, message: `Unknown command: ${cmd.action}` };
    const handler = commandHandlers[cmd.action];
    if (handler) {
      try {
        result = await handler(cmd.payload ?? {});
      } catch (err) {
        result = { success: false, message: err.message };
      }
    }

    try {
      await axios.post(
        `${CONTROLLER_URL}/api/projects/${projectId}/commands/${cmd.id}/complete`,
        result, { timeout: 3_000, headers: _authHeaders() }
      );
    } catch (_) {}
  }
}

async function _sendHeartbeat(store) {
  if (!projectId) return;
  try {
    const s = store || _store;
    const activeGames = s?.activeCount?.() ?? 0;
    const ec = _engineConfig;
    await axios.post(`${CONTROLLER_URL}/api/projects/heartbeat`, {
      projectId,
      state:     'alive',
      uptimeSec: Math.floor((Date.now() - startedAt) / 1000),
      instanceId: INSTANCE_ID,
      metrics: {
        activeGames,
        wins:      session.wins,
        losses:    session.losses,
        draws:     session.draws,
        total:     session.wins + session.losses + session.draws,
        movetime:  ec?.movetime  ?? null,
        threads:   ec?.threads   ?? null,
        hashMB:    ec?.hash      ?? null,
        uptimeSec: Math.floor((Date.now() - startedAt) / 1000),
        hostname:  os.hostname(),
        pid:       process.pid
      }
    }, { timeout: 5_000, headers: _authHeaders() });
  } catch (_) {
    // Controller unreachable — silent, will retry next interval
  }
}

async function _postStatus(payload) {
  if (!projectId) return;
  await axios.post(
    `${CONTROLLER_URL}/api/projects/${projectId}/status`,
    payload,
    { timeout: 5_000, headers: _authHeaders() }
  );
}

function _shutdown() {
  if (heartbeatTimer)   clearInterval(heartbeatTimer);
  if (commandPollTimer) clearInterval(commandPollTimer);
  // Fire-and-forget offline heartbeat
  if (projectId) {
    axios.post(`${CONTROLLER_URL}/api/projects/heartbeat`, {
      projectId, state: 'offline', metrics: { ...session }
    }, { timeout: 3_000, headers: _authHeaders() }).catch(() => {});
  }
}

/**
 * Forward a structured log entry to PPM's log store.
 * Silently no-ops if not yet registered or controller unreachable.
 * Useful for surfacing Lichess API errors, engine crashes, stream drops, etc.
 *
 * @param {'debug'|'info'|'warn'|'error'} level
 * @param {string} message
 * @param {object} [meta]
 */
async function log(level, message, meta = {}) {
  if (!projectId || !CONTROLLER_URL) return;
  const validLevels = ['debug', 'info', 'warn', 'error'];
  const safeLevel = validLevels.includes(level) ? level : 'info';
  try {
    await axios.post(`${CONTROLLER_URL}/api/logs`, {
      projectId,
      logs: [{
        level:     safeLevel,
        message:   String(message),
        meta,
        source:    'bot',
        timestamp: new Date().toISOString()
      }]
    }, { timeout: 3_000, headers: _authHeaders() });
  } catch (_) {
    // Controller unreachable — silent, log already printed to PM2 stdout
  }
}

/**
 * Fetch one or more secrets from PPM and return them as a plain object.
 * Call this at startup (before getServices()) to pull sensitive values out of
 * .env and centralise them in the PPM controller.
 *
 * Returns an object like { LICHESS_TOKEN: '...', ... } for every key that
 * was found.  Keys that are missing are simply omitted — the caller can fall
 * back to process.env or error out as it sees fit.
 *
 * Works even before init() is called because it uses only CONTROLLER_URL
 * and PPM_BOT_NAME from the environment, not the registered projectId.
 *
 * @param {string[]} keys  Secret key names to fetch
 * @returns {Promise<Record<string, string>>}
 */
async function fetchSecrets(keys) {
  if (!CONTROLLER_URL) return {};
  const result = {};
  await Promise.all(keys.map(async (key) => {
    try {
      const headers = {};
      if (PPM_API_KEY) headers['x-api-key'] = PPM_API_KEY;
      const res = await axios.get(
        `${CONTROLLER_URL}/api/secrets/${encodeURIComponent(PROJECT_NAME)}/${encodeURIComponent(key)}`,
        { timeout: 5_000, headers }
      );
      if (res.data?.value) {
        result[key] = res.data.value;
      }
    } catch (err) {
      if (err.response?.status !== 404) {
        console.warn(`[ppm] Could not fetch secret "${key}": ${err.message}`);
      }
      // 404 = not stored in PPM yet, silently fall back to .env
    }
  }));
  return result;
}

module.exports = { init, log, fetchSecrets };
