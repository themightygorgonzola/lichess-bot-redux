'use strict';

/**
 * index.js — Bot entry point (multi-service architecture).
 *
 * Starts the dashboard HTTP server, then runs independent event loops
 * for each configured service (Lichess, Chess.com, etc.).  All services
 * share the same engine binary, dashboard, and game store.
 *
 * Run: node index.js
 */

require('dotenv').config({ path: require('path').join(__dirname, '.env') });

const book = require('./src/book');

// Intercept console.* BEFORE any other module logs anything
require('./src/logBus');

// ── Process-level safety net ───────────────────────────────────────────────
process.on('unhandledRejection', (reason, promise) => {
  console.error('[bot] Unhandled promise rejection:', reason?.stack ?? reason);
  ppm.log('error', 'Unhandled promise rejection', { message: reason?.message ?? String(reason) }).catch(() => {});
});
process.on('uncaughtException', (err) => {
  console.error('[bot] Uncaught exception — bot will continue running:', err.stack ?? err.message);
  ppm.log('error', 'Uncaught exception', { message: err.message }).catch(() => {});
});

const path        = require('path');
const { startDashboard } = require('./src/dashboard');
const { getServices }     = require('./src/services');
const GameHandler = require('./src/game');
const store       = require('./src/store');
const ppm         = require('./src/ppmReporter');
const _botStartedAt = Date.now();
const policies    = require('./src/policies');
const queue       = require('./src/challenger');
const dashState   = require('./src/dashState');
const personality = require('./src/personality');

// ── Restore persisted dashboard state ────────────────────────────────────────
// Must run after all modules are loaded to avoid circular-dependency issues.
dashState.applyOnStartup();

// ── Stamp startup time into version.json ────────────────────────────────────
// This lets every PGN game record carry a [BotStartedAt] header so we know
// exactly which deployed code version played each game.
try {
  const _vPath = path.join(__dirname, 'version.json');
  const _v = JSON.parse(require('fs').readFileSync(_vPath, 'utf8'));
  _v.startedAt = new Date().toISOString();
  require('fs').writeFileSync(_vPath, JSON.stringify(_v, null, 2));
} catch (_) { /* non-fatal */ }

// ── Config ─────────────────────────────────────────────────────────────────
const ENGINE_PATH    = process.env.ENGINE_PATH    ?? './engine/lichess-bot.exe';
const ENGINE_AFFINITY_MASK = process.env.ENGINE_AFFINITY_MASK ?? null;
const EVAL_FILE      = process.env.EVAL_FILE      ?? './engine/nn.bin';
console.log(`[bot] ENGINE_PATH=${ENGINE_PATH}`);

// ── Kill orphaned engine processes from prior crashes ──────────────────────
// If the bot was killed hard (no SIGINT/SIGTERM), old engine processes are
// left behind.  Wipe them before spawning new ones so we don't accumulate
// zombie processes across restarts.
try {
  const _engBin = path.basename(ENGINE_PATH).replace(/\.exe$/i, '');
  const { execSync } = require('child_process');
  execSync(`taskkill /F /IM "${_engBin}.exe" /T 2>nul`, { stdio: 'ignore' });
  console.log(`[bot] Cleaned up any orphaned ${_engBin} processes`);
} catch (_) { /* no processes to kill — that's fine */ }
const MOVETIME_MS    = parseInt(process.env.ENGINE_MOVETIME ?? '2000', 10);
const THREADS        = parseInt(process.env.ENGINE_THREADS  ?? '1',    10);
const HASH_MB        = parseInt(process.env.ENGINE_HASH     ?? '128',  10);
const PONDER         = process.env.PONDER === 'true';
let   maxConcurrent  = parseInt(process.env.MAX_CONCURRENT_GAMES ?? '1', 10);
const DASHBOARD_PORT = parseInt(process.env.DASHBOARD_PORT  ?? '7100', 10);
// USE_NNUE: set to 'false' to force HCE mode (EvalParams active, no nn.bin needed).
// When 'true' (default) the engine loads nn.bin and uses NNUE evaluation.
const USE_NNUE       = process.env.USE_NNUE !== 'false';

const engineConfig = {
  path:     ENGINE_PATH,
  affinityMask: ENGINE_AFFINITY_MASK,
  evalFile: EVAL_FILE,
  movetime: MOVETIME_MS,
  threads:  THREADS,
  hash:     HASH_MB,
  ponder:   PONDER,
  useNnue:  USE_NNUE,
};

const RECONNECT_DELAY_MS = 5_000;

// Track live game handlers across ALL services.
// Key: "serviceId:gameId" to avoid collisions between platforms.
const activeHandlers = new Map();

// ── Per-service runner ─────────────────────────────────────────────────────

/**
 * Run the event loop for a single chess service.  Each service gets its own
 * reconnecting loop, independent of the others.
 */
async function runService(adapter, token) {
  const svc = adapter.name();
  const tag = `[${svc}]`;
  let botUsername = null;

  // Authenticate
  const acct = await adapter.authenticate(token);
  if (!acct.ok) {
    console.error(`${tag} Failed to authenticate:`, acct.status, acct.data);
    ppm.log('error', `${svc} auth failed`, { status: acct.status }).catch(() => {});
    return; // skip this service — don't crash the whole bot
  }
  botUsername = acct.username;
  console.log(`${tag} Logged in as ${botUsername} (bot: ${acct.isBot})`);

  // Reconnecting event loop
  while (true) {
    try {
      console.log(`${tag} Connecting to event stream…`);
      for await (const event of adapter.streamEvents(token)) {
        if (event.type === 'challenge') {
          await onChallenge(adapter, token, event.challenge, botUsername, tag);
        } else if (event.type === 'challengeDeclined') {
          const c = event.challenge;
          const reason = c?.declineReason ?? c?.declineReasonKey ?? 'unknown';
          const destUsername = c?.destUser?.name ?? '?';
          console.warn(`${tag} Challenge ${c?.id} to ${destUsername} was declined: ${reason}`);
          store.emit('challenge_declined', { id: c?.id, reason, destUsername, service: adapter.id() });
          queue.onEvent('challenge_declined', { id: c?.id, reason, destUsername, service: adapter.id() });
        } else if (event.type === 'challengeCanceled') {
          const cid = event.challenge?.id;
          console.log(`${tag} Challenge ${cid} was canceled`);
          store.emit('challenge_canceled', { id: cid, service: adapter.id() });
          queue.onEvent('challenge_canceled', { id: cid, service: adapter.id() });
        } else if (event.type === 'gameStart') {
          const gsRaw = event.game ?? event;
          onGameStart(adapter, token, gsRaw, tag, botUsername);
          const gsNorm = adapter.normalizeGameStart({ game: gsRaw });
          queue.onEvent('game_start', {
            id: gsNorm.gameId,
            opponent: gsNorm.opponent?.name,
            service: adapter.id(),
          });
        } else if (event.type === 'gameFinish') {
          // store is updated from within GameHandler
        }
      }
    } catch (err) {
      console.error(`${tag} Event loop error:`, err.message);
      ppm.log('warn', `${svc} event loop error`, { message: err.message }).catch(() => {});
    }
    console.log(`${tag} Reconnecting in ${RECONNECT_DELAY_MS / 1000}s…`);
    await sleep(RECONNECT_DELAY_MS);
  }
}

// ── Event handlers ─────────────────────────────────────────────────────────

async function onChallenge(adapter, token, challenge, botUsername, tag) {
  const id = challenge.id;
  const challenger = challenge.challenger?.name ?? '?';

  // Filter outgoing challenge echoes
  if (challenge.direction === 'out' || challenger === botUsername) {
    console.log(`${tag} Ignoring outgoing challenge ${id} (own echo)`);
    return;
  }

  const decision = policies.shouldAcceptChallenge(
    challenge,
    store.activeCount(),
    maxConcurrent,
  );

  if (!decision.accept) {
    const active = store.activeCount();
    const reason = decision.declineReason;
    const why = reason === 'later'
      ? `${active}/${maxConcurrent} games active`
      : reason;
    console.log(`${tag} Declining ${id} from ${challenger} (${why})`);
    await adapter.declineChallenge(id, token, reason);
    return;
  }

  console.log(`${tag} Accepting challenge ${id} from ${challenger}`);
  const res = await adapter.acceptChallenge(id, token);
  if (!res.ok) {
    console.warn(`${tag} Accept failed for ${id}:`, res.status, res.data);
  }
}

function onGameStart(adapter, token, game, tag, botUsername) {
  const norm = adapter.normalizeGameStart({ game });
  const gameId = norm.gameId;
  const handlerKey = `${adapter.id()}:${gameId}`;

  if (activeHandlers.has(handlerKey)) {
    console.log(`${tag} Game ${gameId}: handler already running, ignoring duplicate`);
    return;
  }

  // Enforce concurrent game cap on incoming gameStart events.
  // This prevents external services (Lichess) from replaying multiple open
  // gameStart events on reconnect and spawning an engine for each.
  // Self-play is excluded — it's internally rate-limited by the challenger.
  if (adapter.id() !== 'selfplay' && activeHandlers.size >= maxConcurrent) {
    console.warn(`${tag} Game ${gameId}: at handler cap (${activeHandlers.size}/${maxConcurrent}), ignoring`);
    return;
  }

  const color = norm.color;
  if (color !== 'white' && color !== 'black') {
    console.error(`${tag} Game ${gameId}: unrecognised color '${color}' — skipping`);
    return;
  }

  const meta = {
    color,
    opponentId:    norm.opponent.id,
    opponentName:  norm.opponent.name,
    opponentIsBot: norm.opponent.isBot,
    variant:       norm.variant,
    speed:         norm.speed,
    service:       adapter.id(),
    ourName:       botUsername ?? null,
  };

  store.createGame(gameId, meta);
  console.log(`${tag} Game ${gameId} started vs ${meta.opponentName} (${meta.speed}) as ${color}`);

  const handler = new GameHandler(gameId, color, token, engineConfig, adapter);
  activeHandlers.set(handlerKey, handler);
  handler.run().then(() => {
    activeHandlers.delete(handlerKey);
    console.log(`${tag} Game ${gameId} handler finished`);
    // Notify challenge queue of game end
    const rec = store.getGame(gameId);
    if (rec) {
      queue.onGameEnd(gameId, rec.result, rec.opponentId, adapter.id());
    }
  }).catch((err) => {
    activeHandlers.delete(handlerKey);
    console.error(`${tag} Game ${gameId} handler threw:`, err.message);
    ppm.log('error', `Game handler crashed`, { gameId, message: err.message }).catch(() => {});
    const rec = store.getGame(gameId);
    if (rec) {
      queue.onGameEnd(gameId, rec.result, rec.opponentId, adapter.id());
    }
  });
}

// ── Main ───────────────────────────────────────────────────────────────────

async function main() {
  // ── Pull secrets from PPM (overrides .env values if controller is reachable)
  const ppmSecrets = await ppm.fetchSecrets(['LICHESS_TOKEN']);
  const secretCount = Object.keys(ppmSecrets).length;
  if (secretCount > 0) {
    Object.assign(process.env, ppmSecrets);
    console.log(`[bot] Loaded ${secretCount} secret(s) from PPM: ${Object.keys(ppmSecrets).join(', ')}`);
  }

  const services = getServices();

  if (services.length === 0) {
    console.error('[bot] No service tokens configured.');
    console.error('  Set LICHESS_TOKEN (and/or CHESSCOM_TOKEN) in bot/.env');
    console.error('  See bot/.env.example for reference.');
    process.exit(1);
  }

  console.log(`[bot] ${services.length} service(s) configured: ${services.map(s => s.adapter.name()).join(', ')}`);

  // Connect to PPM controller (no-op if CONTROLLER_URL not set)
  // Pass engineConfig so heartbeats surface live threads/hash/movetime values
  await ppm.init(store, {
    'set-threads': ({ value }) => {
      const v = parseInt(value, 10);
      if (!v || v < 1) return { success: false, message: `Invalid thread count: ${value}` };
      engineConfig.threads = v;
      console.log(`[ppm] Engine threads → ${v}`);
      return { success: true, message: `Engine threads set to ${v}` };
    },
    'set-hash': ({ value }) => {
      const v = parseInt(value, 10);
      if (!v || v < 1) return { success: false, message: `Invalid hash size: ${value}` };
      engineConfig.hash = v;
      console.log(`[ppm] Engine hash → ${v} MB`);
      return { success: true, message: `Engine hash set to ${v} MB (takes effect next game)` };
    },
    'set-movetime': ({ value }) => {
      const v = parseInt(value, 10);
      if (!v || v < 100) return { success: false, message: `Invalid movetime: ${value} (min 100ms)` };
      engineConfig.movetime = v;
      console.log(`[ppm] Engine movetime → ${v} ms`);
      return { success: true, message: `Engine movetime set to ${v} ms` };
    },
    'ping': () => ({
      success: true,
      message: `pong — uptime ${Math.floor((Date.now() - _botStartedAt) / 1000)}s, games: ${store.activeCount?.() ?? 0} active`
    }),
    'restart': () => {
      console.log('[ppm] Restart command received — exiting for PM2 restart');
      setImmediate(() => process.exit(0));
      return { success: true, message: 'Bot exiting for PM2-managed restart' };
    },
    'stop': () => {
      console.log('[ppm] Stop command received — shutting down');
      setImmediate(() => process.exit(0));
      return { success: true, message: 'Bot shutting down' };
    },
    'set-max-games': ({ value }) => {
      const v = parseInt(value, 10);
      if (!v || v < 1) return { success: false, message: `Invalid max games: ${value}` };
      maxConcurrent = v;
      console.log(`[ppm] Max concurrent games → ${v}`);
      return { success: true, message: `Max concurrent games set to ${v}` };
    },
    'set-personality': ({ value }) => {
      if (value !== 'full' && value !== 'silent') return { success: false, message: `Invalid mode '${value}' — use 'full' or 'silent'` };
      personality.setMode(value);
      return { success: true, message: `Personality mode set to '${value}'` };
    },
  }, engineConfig);

  // Start dashboard (shared across all services)
  startDashboard(DASHBOARD_PORT);

  // Pre-warm opening book in background — probe() will be a cache hit by game time.
  // loadCache() always runs (cache is service-agnostic; self-play benefits too).
  book.loadCache();
  const lichessToken = services.find(s => s.adapter.id() === 'lichess')?.token;
  if (lichessToken) {
    book.setToken(lichessToken);   // real Lichess token for explorer API calls
    book.prewarm(lichessToken).catch(() => {});
  }

  // Graceful shutdown
  const shutdown = (sig) => {
    console.log(`[bot] ${sig} — stopping ${activeHandlers.size} active game(s)…`);
    // stop() kills the engine process immediately; cleanup() is still called
    // async from the run() finally block but the process is already gone.
    for (const h of activeHandlers.values()) h.stop();
    // Also kill engine by name as a belt-and-suspenders measure in case any
    // handler isn't tracked in activeHandlers (e.g. finished but not yet removed).
    try {
      const _engBin = path.basename(ENGINE_PATH).replace(/\.exe$/i, '');
      require('child_process').execSync(`taskkill /F /IM "${_engBin}.exe" /T 2>nul`, { stdio: 'ignore' });
    } catch (_) {}
    setTimeout(() => process.exit(0), 2_000).unref();
  };
  process.on('SIGINT',  () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));

  // Run each service concurrently in its own event loop
  const runners = services.map(({ adapter, token }) =>
    runService(adapter, token).catch(err => {
      console.error(`[bot] ${adapter.name()} runner crashed:`, err.message);
    })
  );

  await Promise.all(runners);
}

// ── Helpers ────────────────────────────────────────────────────────────────

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

main().catch((err) => {
  console.error('[bot] Fatal:', err);
  process.exit(1);
});
