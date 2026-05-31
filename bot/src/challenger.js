'use strict';

/**
 * challenger.js — Auto-challenger.
 *
 * Dead simple:
 *   1. Discover bots → store in botDb
 *   2. Every tick: pick the closest-elo online bot whose check_back has passed
 *   3. Send a challenge, auto-cancel after 30 s
 *   4. On response: set check_back timestamp
 *   5. Repeat
 *
 * No queue. No pending/active rows. No auto_requeue. No priority.
 * Just bots, cooldowns, and a tick loop.
 */

const botDb      = require('./botDb');
const store      = require('./store');
const dashState  = require('./dashState');
const { getService, getServices } = require('./services');

// wrapper to avoid imposing cooldowns during selfplay runs
function _maybeSetCheckBack(username, service, ts) {
  if (service === 'selfplay') return;
  botDb.setCheckBack(username, service, ts);
}


/* ── Constants ───────────────────────────────────────────────────────── */

const TICK_INTERVAL_MS     = 5_000;
const CHALLENGE_TIMEOUT_MS = 10_000;   // auto-cancel a challenge after 10 seconds
const REFRESH_INTERVAL_MS  = 5 * 60_000;   // refresh online bots every 5 min

/** check_back durations (ms from now) */
const CB = {
  AFTER_GAME:     15 * 60_000,      // 15 min after a game
  PLAYING_LOCK:   24 * 3600_000,    // lock while game is in progress (overwritten by onGameEnd)
  DECLINE:        30 * 60_000,      // 30 min generic decline
  DECLINE_LATER:  60 * 60_000,      // 1h for "later"
  DECLINE_TC:      2 * 3600_000,    // 2h for time-control mismatch
  DECLINE_RATED:   4 * 3600_000,    // 4h for rated/casual mismatch
  DECLINE_NOBOT:  48 * 3600_000,    // 48h doesn't accept bots
  TIMEOUT:         5 * 60_000,      // 5 min no response
  RATE_LIMIT:     60 * 60_000,      // 1h rate-limited
  ERROR:           5 * 60_000,      // 5 min generic error
};

const TC_PRESETS = [
  { id: '1+0',   label: '1+0',   limit: 60,   inc: 0,  speed: 'bullet' },
  { id: '2+1',   label: '2+1',   limit: 120,  inc: 1,  speed: 'bullet' },
  { id: '3+2',   label: '3+2',   limit: 180,  inc: 2,  speed: 'blitz'  },
  { id: '5+3',   label: '5+3',   limit: 300,  inc: 3,  speed: 'blitz'  },
  { id: '10+5',  label: '10+5',  limit: 600,  inc: 5,  speed: 'rapid'  },
  { id: '15+10', label: '15+10', limit: 900,  inc: 10, speed: 'rapid'  },
];

/* ── State ───────────────────────────────────────────────────────────── */

let _running     = false;
let _tickTimer   = null;
let _cancelTimer = null;
let _activeBot   = null;   // username currently being challenged
let _activeCId   = null;   // challenge id
let _activeSentAt = null;  // when we sent the challenge
let _activeTc    = null;   // "3+2" etc
let _lastTickLog = '';
let _lastRefresh = 0;
let _playingOpponents = new Set();  // usernames currently in a game

let _ourUsername  = null;
let _ourRatings   = { bullet: null, blitz: null, rapid: null };
let _ratingsFetched    = false;
let _fixedListSynced   = false;  // sync fixed-list services (selfplay) once on first getState
let _oddsLevels        = new Map(); // username → oddsLevel (0–4), loaded from dashState

// Resolve the default service to the first available one (selfplay if no
// online token is configured) so the seek tab never breaks on missing keys.
function _defaultService() {
  const svcs = getServices();
  return svcs[0]?.adapter.id() ?? 'selfplay';
}

let _settings = {
  enabled:       false,
  rated:         true,
  color:         'random',
  enabledTCs:    ['3+2', '5+3'],
  eloMin:        null,    // null = no lower bound
  eloMax:        null,    // null = no upper bound
  maxConcurrent: 1,
  service:       _defaultService(),
  selfplayFallback: false,
};

/* ══════════════════════════════════════════════════════════════════════════
   Public API
   ══════════════════════════════════════════════════════════════════════════ */

async function start() {
  if (_running) return;
  _running = true;
  _settings.enabled = true;

  await _fetchOurRatings();
  await refreshBots();

  botDb.log('started', null, { settings: _settings });
  _emitState();
  _tick();
  _tickTimer = setInterval(_tick, TICK_INTERVAL_MS);
  console.log('[challenger] Started');
}

function stop() {
  if (!_running) return;
  _running = false;
  _settings.enabled = false;
  if (_tickTimer) { clearInterval(_tickTimer); _tickTimer = null; }
  botDb.log('stopped');
  _emitState();
  console.log('[challenger] Stopped');
}

function isRunning() { return _running; }

function getSettings() { return { ..._settings }; }

function updateSettings(patch) {
  for (const [k, v] of Object.entries(patch)) {
    if (k in _settings) _settings[k] = v;
  }
  if (patch.enabled === true  && !_running) start();
  if (patch.enabled === false && _running)  stop();
  _emitState();
  return { ..._settings };
}

/** Full state for the dashboard (polled every ~3 s). */
function getState() {
  // Eagerly fetch ratings on first poll (even before start)
  if (!_ratingsFetched) {
    _ratingsFetched = true;
    _fetchOurRatings().catch(() => {});
  }
  // Eagerly sync fixed-list services (selfplay) once so new presets appear in DB
  // immediately without requiring the user to click Refresh or start the challenger.
  if (!_fixedListSynced) {
    _fixedListSynced = true;
    // Load persisted odds levels from dashState
    const savedOdds = dashState.get('selfplayOdds');
    for (const [u, l] of Object.entries(savedOdds)) _oddsLevels.set(u, l);
    // Sync fixed-list services (selfplay) so new presets appear in DB immediately
    for (const svc of getServices()) {
      if (svc.adapter.id() === 'selfplay') {
        _refreshService('selfplay').catch(() => {});
      }
    }
  }

  const bots  = botDb.getAllBots(_settings.service);
  const stats = botDb.getStats(_settings.service);
  const log   = botDb.getLog(60);
  const now   = Date.now();

  // Sort: online first, then by elo closeness to us
  const speed  = _primarySpeed();
  const eloKey = `elo_${speed}`;
  const ourElo = _ourRatings[speed] ?? 1500;

  bots.sort((a, b) => {
    if (a.online !== b.online) return b.online - a.online;
    return Math.abs((a[eloKey] ?? 1500) - ourElo)
         - Math.abs((b[eloKey] ?? 1500) - ourElo);
  });

  const annotated = bots.map(b => ({
    ...b,
    ready:        b.online === 1 && (b.check_back ?? 0) <= now,
    cooldownLeft: Math.max(0, (b.check_back ?? 0) - now),
    playing:      _playingOpponents.has(b.username.toLowerCase()),
  }));

  // Build profile URLs via adapter
  const svc = getService(_settings.service);

  // Attach odds metadata for bots that support it (selfplay)
  const annotatedWithUrl = annotated.map(b => {
    const preset = svc?.adapter?.getPreset?.(b.username);
    return {
      ...b,
      profileUrl: svc?.adapter?.profileUrl(b.username) ?? null,
      oddsLevel:  _oddsLevels.get(b.username) ?? 0,
      oddsLabels: preset?.oddsLevels?.labels ?? null,
    };
  });

  const availableServices = getServices().map(s => ({
    id:   s.adapter.id(),
    name: s.adapter.name(),
  }));

  return {
    running:    _running,
    settings:   { ..._settings },
    ourRatings: { ..._ourRatings },
    ourUsername: _ourUsername,
    serviceName: svc?.adapter?.name() ?? _settings.service,
    availableServices,
    active: _activeBot
      ? { username: _activeBot, challengeId: _activeCId, sentAt: _activeSentAt, tc: _activeTc }
      : null,
    bots:  annotatedWithUrl,
    stats,
    log,
    tcPresets: TC_PRESETS,
  };
}

/** Fetch online bots from one service and sync to DB. */
async function _refreshService(serviceId) {
  const svc = getService(serviceId);
  if (!svc) return 0;

  const rawBots    = await svc.adapter.fetchOnlineBots(1000, svc.token);
  const normalized = rawBots
    .map(b => svc.adapter.normalizeBot(b))
    .filter(b => b.username.toLowerCase() !== (_ourUsername ?? '').toLowerCase());

  const count = botDb.syncOnlineBots(normalized, serviceId);
  // For fixed-list services (selfplay) purge stale rows that no longer match presets.
  if (serviceId === 'selfplay') {
    botDb.purgeUnknownBots(serviceId, normalized.map(b => b.username));
  }
  console.log(`[challenger] Refreshed ${serviceId}: ${count} bots online`);
  return count;
}

/** Refresh ALL configured services so every tab shows up-to-date bots. */
async function refreshBots() {
  try {
    const services = getServices();
    let total = 0;
    for (const svc of services) total += await _refreshService(svc.id);
    _lastRefresh = Date.now();
    botDb.log('refreshed', null, { online: total });
    _emitState();
    return total;
  } catch (e) {
    console.error('[challenger] Refresh error:', e.message);
    return 0;
  }
}

/* ══════════════════════════════════════════════════════════════════════════
   Event handling — called from index.js
   ══════════════════════════════════════════════════════════════════════════ */

function onEvent(type, data) {

  /* ── challenge_declined ───────────────────────────────────────────── */
  if (type === 'challenge_declined') {
    const who = data.destUsername ?? _activeBot;
    if (!who) return;
    if (data.id !== _activeCId && who.toLowerCase() !== (_activeBot ?? '').toLowerCase()) return;

    _clearCancelTimer();
    const reason = data.reason ?? data.declineReasonKey ?? 'unknown';
    const streak = botDb.bumpDeclineStreak(who, _settings.service);
    const baseCb = _declineCooldown(reason, data);
    // Exponential backoff: base × 2^(streak-1), capped at 24h
    const baseMs = baseCb - Date.now();
    const backoff = Math.min(baseMs * Math.pow(2, streak - 1), 24 * 3600_000);
    const cb = Date.now() + backoff;

    _maybeSetCheckBack(who, _settings.service, cb);
    botDb.recordChallenge(who, _settings.service, `declined:${reason}`);
    botDb.resetTimeoutStreak(who, _settings.service);
    botDb.log('declined', who, { reason, streak, checkBackMin: Math.round(backoff / 60_000) });
    console.log(`[challenger] ${who} declined (${reason}, streak ${streak}), check back ${Math.round(backoff / 60_000)}m`);

    _activeBot = null;
    _activeCId = null;
    _activeSentAt = null;
    _activeTc = null;
    _emitState();
  }

  /* ── challenge_canceled / timeout ─────────────────────────────────── */
  if (type === 'challenge_canceled') {
    if (data.id !== _activeCId && !_activeBot) return;

    _clearCancelTimer();
    const who = _activeBot;
    if (who) {
      const streak = botDb.bumpTimeoutStreak(who, _settings.service);
      // Exponential backoff: 5m, 10m, 20m, 40m, 80m, ... capped at 24h
      const backoff = Math.min(CB.TIMEOUT * Math.pow(2, streak - 1), 24 * 3600_000);
      const cb = Date.now() + backoff;
      _maybeSetCheckBack(who, _settings.service, cb);
      botDb.recordChallenge(who, _settings.service, 'timeout');
      botDb.log('timeout', who, { streak, backoffMin: Math.round(backoff / 60_000) });
      console.log(`[challenger] ${who} timed out (streak ${streak}, backoff ${Math.round(backoff / 60_000)}m)`);
    }

    _activeBot = null;
    _activeCId = null;
    _activeSentAt = null;
    _activeTc = null;
    _emitState();
  }

  /* ── game_start ───────────────────────────────────────────────────── */
  if (type === 'game_start') {
    const gameId   = data.gameId ?? data.id;
    const opponent = (data.opponent ?? data.opponentName ?? data.opponentId ?? '').toLowerCase();

    if (opponent !== (_activeBot ?? '').toLowerCase() && data.id !== _activeCId) return;

    _clearCancelTimer();
    const who = _activeBot || opponent;

    // Lock check_back for the duration of the game so the tick loop cannot
    // re-challenge this opponent while we are still playing them.
    // onGameEnd() will overwrite this with the real AFTER_GAME cooldown.
    _maybeSetCheckBack(who, _settings.service, Date.now() + CB.PLAYING_LOCK);
    _playingOpponents.add(who.toLowerCase());
    botDb.recordChallenge(who, _settings.service, 'game');
    botDb.recordGame(who, _settings.service);
    botDb.resetTimeoutStreak(who, _settings.service);
    botDb.resetDeclineStreak(who, _settings.service);
    botDb.log('game_started', who, { gameId });
    console.log(`[challenger] Game started vs ${who}: ${gameId}`);

    _activeBot = null;
    _activeCId = null;
    _activeSentAt = null;
    _activeTc = null;
    _emitState();
  }
}

/** Called from game handler .then()/.catch() */
function onGameEnd(gameId, result, opponent, service) {
  if (!opponent) return;
  opponent = opponent.toLowerCase();
  service = service || _settings.service;
  // don't apply any cooldowns when running selfplay
  if (service === 'selfplay') return;

  _playingOpponents.delete(opponent);
  _maybeSetCheckBack(opponent, service, Date.now() + CB.AFTER_GAME);
  botDb.log('game_ended', opponent, { gameId, result });
  _emitState();
}

/* ══════════════════════════════════════════════════════════════════════════
   Tick loop
   ══════════════════════════════════════════════════════════════════════════ */

function _tickLog(reason) {
  if (reason === _lastTickLog) return;
  _lastTickLog = reason;
  console.log('[challenger] tick:', reason);
}

function _tick() {
  if (!_running) return;

  try {
    // 1. Already challenging?
    if (_activeBot) {
      _tickLog(`waiting on ${_activeBot}`);
      return;
    }

    // 2. Concurrent game cap
    const activeGames = store.activeCount();
    if (activeGames >= _settings.maxConcurrent) {
      _tickLog(`at game cap (${activeGames}/${_settings.maxConcurrent})`);
      return;
    }

    // 3. Refresh online bots periodically
    if (Date.now() - _lastRefresh > REFRESH_INTERVAL_MS) {
      refreshBots(); // fire-and-forget
    }

    // 4. Find ready bots
    const ready = botDb.getReadyBots(_settings.service);
    if (!ready.length) {
      const allBots = botDb.getAllBots(_settings.service);
      const cooling = allBots.filter(b => b.online && (b.check_back ?? 0) > Date.now());
      if (cooling.length) {
        const nearest = Math.min(...cooling.map(b => b.check_back));
        const sec = Math.max(0, Math.round((nearest - Date.now()) / 1000));
        _tickLog(`${cooling.length} cooling, next in ${sec}s`);
        // Selfplay fallback: when all real opponents are on cooldown, play a local game
        if (_settings.selfplayFallback && _settings.service !== 'selfplay') {
          _trySelfplayFallback();
        }
      } else {
        _tickLog('no bots available');
      }
      return;
    }

    // 5. Filter by elo range
    const speed  = _primarySpeed();
    const eloKey = `elo_${speed}`;
    let candidates = ready;

    if (_settings.eloMin != null) {
      candidates = candidates.filter(b => (b[eloKey] ?? 0) >= _settings.eloMin);
    }
    if (_settings.eloMax != null) {
      candidates = candidates.filter(b => (b[eloKey] ?? 9999) <= _settings.eloMax);
    }

    if (!candidates.length) {
      _tickLog(`${ready.length} ready but none match elo ${_settings.eloMin ?? 0}..${_settings.eloMax ?? '∞'}`);
      // Selfplay fallback: elo filter excluded everyone, so fall back to a local game
      if (_settings.selfplayFallback && _settings.service !== 'selfplay') {
        _trySelfplayFallback();
      }
      return;
    }

    // 6. Sort by elo closeness
    const ourElo = _ourRatings[speed] ?? 1500;
    candidates.sort((a, b) =>
      Math.abs((a[eloKey] ?? 1500) - ourElo) - Math.abs((b[eloKey] ?? 1500) - ourElo)
    );

    const pick = candidates[0];
    _lastTickLog = '';
    console.log(`[challenger] Challenging ${pick.username} (${pick[eloKey] ?? '?'} ${speed})`);
    _sendChallenge(pick);

  } catch (err) {
    console.error('[challenger] Tick error:', err.message);
    botDb.log('error', null, { error: err.message });
  }
}

/* ── Selfplay fallback ───────────────────────────────────────────────── */

function _trySelfplayFallback() {
  // Don't stack a fallback on top of an active challenge
  if (_activeBot) return;

  const svc = getService('selfplay');
  if (!svc) { _tickLog('selfplay fallback: service not configured'); return; }

  const bots = botDb.getReadyBots('selfplay');   // already filters selected=1
  if (!bots.length) { _tickLog('selfplay fallback: no selected bots ready'); return; }

  const pick = bots[Math.floor(Math.random() * bots.length)];
  _lastTickLog = '';
  console.log(`[challenger] Selfplay fallback → ${pick.username}`);
  _sendChallenge(pick, 'selfplay');
}

/* ── Send challenge ──────────────────────────────────────────────────── */

async function _sendChallenge(bot, serviceId = null) {
  const svcId = serviceId ?? _settings.service;
  const svc = getService(svcId);
  if (!svc) {
    botDb.log('error', bot.username, { error: 'service not configured' });
    return;
  }

  const tc = _pickTimeControl();
  _activeBot    = bot.username;
  _activeCId    = null;
  _activeSentAt = Date.now();
  _activeTc     = `${tc.limit / 60}+${tc.inc}`;
  _emitState();

  try {
    const result = await svc.adapter.challengeUser(bot.username, svc.token, {
      timeLimit:  tc.limit,
      increment:  tc.inc,
      rated:      _settings.rated,
      color:      _settings.color,
      variant:    'standard',
      oddsLevel:  _oddsLevels.get(bot.username) ?? 0,
    });

    if (result.ok) {
      const challengeId = result.data?.challenge?.id ?? result.data?.id;
      _activeCId = challengeId;
      botDb.recordChallenge(bot.username, svcId, 'sent');
      botDb.log('sent', bot.username, { challengeId, tc: _activeTc });
      store.emit('challenge_sent', { challengeId, username: bot.username });

      // Auto-cancel after CHALLENGE_TIMEOUT_MS
      _cancelTimer = setTimeout(async () => {
        if (_activeCId === challengeId) {
          try { await svc.adapter.cancelChallenge(challengeId, svc.token); } catch (_) {}
          if (_activeCId === challengeId) {
            onEvent('challenge_canceled', { id: challengeId });
          }
        }
      }, CHALLENGE_TIMEOUT_MS);

    } else {
      const errMsg = result.data?.error ?? result.data?.message ?? `HTTP ${result.status}`;
      const cb = _errorCooldown(result.status, errMsg);
      _maybeSetCheckBack(bot.username, svcId, cb);
      botDb.recordChallenge(bot.username, svcId, `error:${result.status}`);
      botDb.log('error', bot.username, { status: result.status, error: errMsg });
      console.warn(`[challenger] ${bot.username} error: ${errMsg}`);
      _activeBot = null; _activeCId = null; _activeSentAt = null; _activeTc = null;
      _emitState();
    }

  } catch (err) {
    console.error(`[challenger] Send error ${bot.username}:`, err.message);
    _maybeSetCheckBack(bot.username, svcId, Date.now() + CB.ERROR);
    botDb.log('error', bot.username, { error: err.message });
    _activeBot = null; _activeCId = null; _activeSentAt = null; _activeTc = null;
    _emitState();
  }
}

/* ══════════════════════════════════════════════════════════════════════════
   Cooldown computation
   ══════════════════════════════════════════════════════════════════════════ */

function _declineCooldown(reason, data) {
  const now = Date.now();
  const msg = JSON.stringify(data);

  // Explicit ISO timestamp
  const iso = msg.match(/(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)/);
  if (iso) {
    const ts = new Date(iso[1]).getTime();
    if (ts > now && ts < now + 48 * 3600_000) return ts;
  }

  // Duration: "30 minutes", "2 hours"
  const dur = msg.match(/(\d+)\s*(minute|hour|min|hr|h|m)\b/i);
  if (dur) {
    const n = parseInt(dur[1], 10);
    const ms = dur[2].toLowerCase().startsWith('h') ? n * 3600_000 : n * 60_000;
    if (ms > 0 && ms < 48 * 3600_000) return now + ms;
  }

  switch (reason) {
    case 'later':                       return now + CB.DECLINE_LATER;
    case 'tooFast': case 'tooSlow':
    case 'timeControl':                 return now + CB.DECLINE_TC;
    case 'rated': case 'casual':        return now + CB.DECLINE_RATED;
    case 'noBot':                       return now + CB.DECLINE_NOBOT;
    default:                            return now + CB.DECLINE;
  }
}

function _errorCooldown(status, errMsg) {
  const now = Date.now();

  if (status === 429) return now + CB.RATE_LIMIT;

  const iso = (errMsg || '').match(/(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)/);
  if (iso) {
    const ts = new Date(iso[1]).getTime();
    if (ts > now && ts < now + 72 * 3600_000) return ts;
  }

  const lower = (errMsg || '').toLowerCase();
  if (lower.includes('daily') || lower.includes('limit') || lower.includes('too many')) {
    const tomorrow = new Date();
    tomorrow.setUTCDate(tomorrow.getUTCDate() + 1);
    tomorrow.setUTCHours(0, 0, 0, 0);
    return tomorrow.getTime();
  }

  return now + CB.ERROR;
}

/* ══════════════════════════════════════════════════════════════════════════
   Helpers
   ══════════════════════════════════════════════════════════════════════════ */

function _primarySpeed() {
  const ids = _settings.enabledTCs || [];
  if (ids.some(id => TC_PRESETS.find(t => t.id === id)?.speed === 'blitz'))  return 'blitz';
  if (ids.some(id => TC_PRESETS.find(t => t.id === id)?.speed === 'bullet')) return 'bullet';
  if (ids.some(id => TC_PRESETS.find(t => t.id === id)?.speed === 'rapid'))  return 'rapid';
  return 'blitz';
}

function _pickTimeControl() {
  const enabled = TC_PRESETS.filter(tc => _settings.enabledTCs.includes(tc.id));
  if (!enabled.length) return { limit: 300, inc: 3, speed: 'blitz' };
  const pick = enabled[Math.floor(Math.random() * enabled.length)];
  return { limit: pick.limit, inc: pick.inc, speed: pick.speed };
}

async function _fetchOurRatings() {
  try {
    const svc = getService(_settings.service);
    if (!svc) return;
    const acc = await svc.adapter.getAccount(svc.token);
    if (acc.ok) {
      const norm = svc.adapter.normalizeAccount(acc.data);
      _ourUsername = norm.username;
      _ourRatings = {
        bullet: norm.ratings.bullet ?? null,
        blitz:  norm.ratings.blitz  ?? null,
        rapid:  norm.ratings.rapid  ?? null,
      };

      console.log(`[challenger] Our ratings: bullet=${_ourRatings.bullet} blitz=${_ourRatings.blitz} rapid=${_ourRatings.rapid} (${svc.adapter.name()})`);
      _emitState();
    }
  } catch (e) {
    console.warn('[challenger] Could not fetch our ratings:', e.message);
  }
}

function _clearCancelTimer() {
  if (_cancelTimer) { clearTimeout(_cancelTimer); _cancelTimer = null; }
}

function _emitState() {
  store.emit('queue_state', getState());
}

/**
 * Set or update the odds level for a self-play bot.
 * @param {string} username  Bot username (e.g. 'SF-Knights')
 * @param {number} level     0–4
 */
function setOddsLevel(username, level) {
  const clamped = Math.max(0, Math.min(4, level));
  _oddsLevels.set(username, clamped);
  dashState.save('selfplayOdds', { [username]: clamped });
}

module.exports = {
  start,
  stop,
  isRunning,
  getSettings,
  updateSettings,
  getState,
  refreshBots,
  onEvent,
  onGameEnd,
  setOddsLevel,
  TC_PRESETS,
};
