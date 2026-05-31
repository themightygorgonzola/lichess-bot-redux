'use strict';

/**
 * dashboard.js — Express HTTP server + SSE push endpoint.
 *
 * Routes:
 *   GET  /               → public/index.html
 *   GET  /events         → SSE stream of game events (+ log events)
 *   GET  /api/games      → JSON array of all game records
 *   GET  /api/games/:id  → single game record
 *   GET  /api/config     → combined env + policies + personality snapshot
 *   POST /api/config     → live-patch policy values (persisted via dashState → data/dash-state.json)
 *   GET  /api/personality → personality pool names + current mode
 *   POST /api/personality/mode → switch mode:  { mode: 'full' | 'silent' }
 *   GET  /api/bots?nb=50          → online BOT accounts sorted by blitz rating
 *   GET  /api/bots/user/:username → single Lichess user object
 *   POST /api/bots/challenge      → send outgoing challenge { username, timeLimit, increment, rated, color }
 *
 * Analytics routes:
 *   GET  /api/analytics?period=today|week|month|all&result=all|win|loss|draw&max=200
 *        → { count, pgn, summary[] } — fetches game PGNs from Lichess with filtering
 *   POST /api/analytics/save  { pgn, filename? } → writes to <workspace>/analyze/
 *   GET  /api/analytics/files → list of saved .pgn files
 */

const express  = require('express');
const path     = require('path');
const fs       = require('fs').promises;
const fss      = require('fs');
const store      = require('./store');
const config     = require('./config');
const { getService } = require('./services');
const challenger = require('./challenger');
const botDb      = require('./botDb');
const dashState  = require('./dashState');

const _versionPath = path.join(__dirname, '..', 'version.json');
function _readVersion() {
  try {
    const raw = fss.readFileSync(_versionPath, 'utf8').replace(/^\uFEFF/, '');
    return JSON.parse(raw);
  } catch (_) {}
  return { build: 0, date: '', commit: '' };
}

function createDashboard() {
  const app = express();

  // JSON body parsing for POST endpoints
  app.use(express.json());

  // Static assets
  app.use(express.static(path.join(__dirname, '..', 'public')));

  // Cache bot username (avoids repeated /api/account calls)
  let _botUsername = null;
  async function _getBotUsername() {
    if (_botUsername) return _botUsername;
    const svc = getService('lichess');
    if (!svc) throw new Error('Lichess not configured');
    const acc = await svc.adapter.getAccount(svc.token);
    if (!acc.ok) throw new Error('Cannot resolve bot account');
    _botUsername = acc.data.id || acc.data.username;
    return _botUsername;
  }

  const ANALYZE_DIR = path.join(__dirname, '..', '..', 'analyze');

  // ── SSE endpoint ──────────────────────────────────────────────────────
  app.get('/events', (req, res) => {
    // Disable Nagle's algorithm so each SSE write is sent immediately
    // rather than being coalesced with subsequent writes into a burst.
    req.socket.setNoDelay(true);

    res.setHeader('Content-Type',  'text/event-stream');
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('Connection',    'keep-alive');
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.flushHeaders();

    store.addSseClient(res);

    // If a selfplay match is running, immediately push its state to this new
    // client so a page refresh restores the board without waiting for the next
    // scheduled sp_state event.
    try {
      const spAdapter = getService('selfplay')?.adapter;
      if (spAdapter) {
        const spMsg = `data: ${JSON.stringify({ type: 'sp_state', data: { matchState: spAdapter.getMatchState() } })}\n\n`;
        res.write(spMsg);
      }
    } catch (_) {}

    // Keep-alive ping every 25 s
    const ping = setInterval(() => {
      try { res.write(': ping\n\n'); } catch (_) { clearInterval(ping); }
    }, 25_000);

    req.on('close', () => {
      clearInterval(ping);
      store.removeSseClient(res);
    });
  });

  // ── REST: games ───────────────────────────────────────────────────────
  app.get('/api/games', (_req, res) => {
    res.json([...store.games.values()]);
  });

  app.get('/api/games/:id', (req, res) => {
    const g = store.getGame(req.params.id);
    if (!g) return res.status(404).json({ error: 'not found' });
    return res.json(g);
  });

  // ── REST: game actions (resign / abort) ──────────────────────────────
  // POST /api/games/:id/resign  — forfeit the game immediately
  // POST /api/games/:id/abort   — abort before the first move is made
  app.post('/api/games/:id/resign', async (req, res) => {
    const g = store.getGame(req.params.id);
    if (!g) return res.status(404).json({ error: 'game not found' });
    const svc = getService(g.service);
    if (!svc) return res.status(503).json({ error: `service '${g.service}' not available` });
    try {
      console.log(`[ctrl] dashboard resign game=${req.params.id}`);
      const r = await svc.adapter.resignGame(req.params.id, svc.token);
      return res.json({ ok: true, result: r });
    } catch (e) {
      return res.status(502).json({ error: e.message });
    }
  });

  app.post('/api/games/:id/abort', async (req, res) => {
    const g = store.getGame(req.params.id);
    if (!g) return res.status(404).json({ error: 'game not found' });
    const svc = getService(g.service);
    if (!svc) return res.status(503).json({ error: `service '${g.service}' not available` });
    try {
      console.log(`[ctrl] dashboard abort game=${req.params.id}`);
      const r = await svc.adapter.abortGame(req.params.id, svc.token);
      return res.json({ ok: true, result: r });
    } catch (e) {
      return res.status(502).json({ error: e.message });
    }
  });

  // ── REST: config ──────────────────────────────────────────────────────
  app.get('/api/config', (_req, res) => {
    res.json(config.getConfig());
  });

  // ── REST: build version ───────────────────────────────────────────────
  app.get('/api/version', (_req, res) => res.json(_readVersion()));

  // ── REST: build archives ──────────────────────────────────────────────
  // Returns array of { build, date, commit, files } sorted newest-first.
  const _archivesRoot = path.join(__dirname, '..', '..', 'archives');
  app.get('/api/archives', async (_req, res) => {
    try {
      const entries = await fs.readdir(_archivesRoot).catch(() => []);
      const builds = await Promise.all(
        entries
          .filter(e => /^build-\d+$/.test(e))
          .map(async e => {
            try {
              const raw = await fs.readFile(path.join(_archivesRoot, e, 'manifest.json'), 'utf8');
              return JSON.parse(raw.replace(/^\uFEFF/, ''));
            } catch { return null; }
          })
      );
      const sorted = builds.filter(Boolean).sort((a, b) => b.build - a.build);
      res.json(sorted);
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  });

  // ── REST: services ────────────────────────────────────────────────────
  // Returns which chess services are configured and active.
  app.get('/api/services', (_req, res) => {
    const { getServices } = require('./services');
    const svcs = getServices().map(s => ({
      id:   s.adapter.id(),
      name: s.adapter.name(),
    }));
    return res.json({ services: svcs });
  });

  app.post('/api/config', (req, res) => {
    // Live-patch policy values.  Body should be a flat object of DEFAULTS
    // keys → new values.  Only numeric and boolean scalars are accepted for
    // safety.  Applied values are persisted to data/dash-state.json so they
    // survive process restarts.
    const patches = req.body;
    if (!patches || typeof patches !== 'object') {
      return res.status(400).json({ error: 'body must be an object' });
    }

    const applied = {};
    const policies = require('./policies');
    for (const [key, val] of Object.entries(patches)) {
      if (!(key in policies.DEFAULTS)) continue;
      const t = typeof val;
      if (t !== 'number' && t !== 'boolean') continue;
      // policies.DEFAULTS is frozen — we can't mutate it directly.
      // Instead, expose a mutable overlay via policies.override()
      if (typeof policies.override === 'function') {
        policies.override(key, val);
        applied[key] = val;
      }
    }
    // Persist to disk so overrides survive restarts.
    if (Object.keys(applied).length > 0) {
      dashState.save('policies', applied);
      // Emit one log line per changed key so the dashboard log tab shows it.
      for (const [key, val] of Object.entries(applied)) {
        console.log(`[ctrl] policy key=${key} val=${val} effective=immediate`);
      }
    }
    return res.json({ applied });
  });

  // ── REST: personality ─────────────────────────────────────────────────
  app.get('/api/personality', (_req, res) => {
    res.json(config.personalitySummary());
  });

  app.post('/api/personality/mode', (req, res) => {
    const { mode } = req.body ?? {};
    if (mode !== 'full' && mode !== 'silent') {
      return res.status(400).json({ error: "mode must be 'full' or 'silent'" });
    }
    const personality = require('./personality');
    personality.setMode(mode);
    dashState.save('personality', { mode });
    console.log(`[ctrl] personality mode=${mode} effective=immediate`);
    return res.json({ mode });
  });

  // ── REST: bot seek ────────────────────────────────────────────────────
  // GET  /api/bots?nb=50          → array of online BOT users, sorted by blitz rating
  // GET  /api/bots/user/:username → single user object (any title)
  // POST /api/bots/challenge      → send outgoing challenge { username, timeLimit, increment, rated, color }

  app.get('/api/bots', async (_req, res) => {
    const nb  = Math.min(parseInt(_req.query.nb ?? '50', 10), 1000);
    const svc = getService('lichess');
    if (!svc) return res.status(503).json({ error: 'Lichess not configured' });
    try {
      const bots = await svc.adapter.fetchOnlineBots(nb, svc.token);
      // Sort by blitz rating descending
      bots.sort((a, b) => (b.perfs?.blitz?.rating ?? 0) - (a.perfs?.blitz?.rating ?? 0));
      return res.json(bots);
    } catch (e) {
      return res.status(502).json({ error: e.message });
    }
  });

  app.get('/api/bots/user/:username', async (req, res) => {
    const svc = getService('lichess');
    if (!svc) return res.status(503).json({ error: 'Lichess not configured' });
    try {
      const r = await svc.adapter.getUser(req.params.username, svc.token);
      if (!r.ok) return res.status(r.status).json(r.data);
      return res.json(r.data);
    } catch (e) {
      return res.status(502).json({ error: e.message });
    }
  });

  app.post('/api/bots/challenge', async (req, res) => {
    const { username, timeLimit, increment, rated, color, variant, service: svcId } = req.body ?? {};
    if (!username) return res.status(400).json({ error: 'username required' });
    // Route to the explicitly-requested service, or fall back to selfplay if
    // Stockfish is the target, otherwise default to Lichess.
    const resolvedSvcId = svcId
      ?? (username.toLowerCase() === 'stockfish' ? 'selfplay' : 'lichess');
    const svc = getService(resolvedSvcId);
    if (!svc) return res.status(503).json({ error: `${resolvedSvcId} not configured` });
    try {
      const r = await svc.adapter.challengeUser(username, svc.token, { timeLimit, increment, rated, color, variant });
      if (r.ok) {
        const challengeId = r.data?.challenge?.id ?? r.data?.id;
        if (challengeId) {
          // Notify the dashboard frontend immediately so it can track the pending challenge
          // without relying on a race between the HTTP response and incoming SSE events.
          store.emit('challenge_sent', { challengeId, username });
          // Auto-cancel after 30 s if the opponent hasn't responded.
          // cancelChallenge will 404 silently if the game already started.
          setTimeout(async () => {
            const cr = await svc.adapter.cancelChallenge(challengeId, svc.token).catch(() => null);
            if (cr && cr.ok) {
              console.log(`[bot] Auto-cancelled challenge ${challengeId} (no response after 30s)`);
              store.emit('challenge_canceled', { id: challengeId });
            }
          }, 30_000);
        }
      }
      return res.status(r.ok ? 200 : r.status).json(r.data);
    } catch (e) {
      return res.status(502).json({ error: e.message });
    }
  });

  // ── REST: challenger ──────────────────────────────────────────────

  app.get('/api/queue', (_req, res) => res.json(challenger.getState()));

  app.post('/api/queue/settings', (req, res) => {
    return res.json(challenger.updateSettings(req.body ?? {}));
  });

  app.post('/api/queue/start', async (_req, res) => {
    await challenger.start();
    return res.json({ running: true });
  });

  app.post('/api/queue/stop', (_req, res) => {
    challenger.stop();
    return res.json({ running: false });
  });

  app.post('/api/queue/cancel', async (_req, res) => {
    const state = challenger.getState();
    const active = state.active;
    if (!active?.challengeId) return res.json({ ok: true, note: 'nothing active' });
    const svcId = state.settings.service || 'lichess';
    const svc = getService(svcId);
    if (svc) {
      try { await svc.adapter.cancelChallenge(active.challengeId, svc.token); } catch (_) {}
    }
    challenger.onEvent('challenge_canceled', { id: active.challengeId });
    store.emit('challenge_canceled', { id: active.challengeId, service: svcId });
    return res.json({ ok: true });
  });

  app.post('/api/queue/refresh', async (_req, res) => {
    const count = await challenger.refreshBots();
    return res.json({ ok: true, online: count });
  });

  app.post('/api/queue/nuke', (_req, res) => {
    botDb.nuke();
    store.emit('queue_state', challenger.getState());
    return res.json({ ok: true });
  });

  // Toggle a bot's selected state for the auto-challenger.
  // Body: { username, selected: true|false }
  // Uses current queue service setting to scope the update.
  app.post('/api/queue/bots/select', (req, res) => {
    const { username, selected } = req.body ?? {};
    if (!username || selected == null) {
      return res.status(400).json({ error: 'username and selected required' });
    }
    const service = challenger.getState().settings.service;
    botDb.setSelected(username, service, selected);
    store.emit('queue_state', challenger.getState());
    return res.json({ ok: true });
  });

  // Set the odds level (0–4) for a self-play bot.
  // Body: { username, oddsLevel }
  app.post('/api/queue/bots/odds', (req, res) => {
    const { username, oddsLevel } = req.body ?? {};
    if (!username || oddsLevel == null) {
      return res.status(400).json({ error: 'username and oddsLevel required' });
    }
    challenger.setOddsLevel(username, parseInt(oddsLevel, 10));
    store.emit('queue_state', challenger.getState());
    return res.json({ ok: true });
  });

  // ── REST: analytics ───────────────────────────────────────────────────
  //
  // GET /api/analytics?period=today|week|month|all&result=all|win|loss|draw&max=200
  //
  // Streams games from Lichess, filters by result, returns:
  //   { count, pgn, summary: [{ id, result, speed, opening, opponent }] }
  //
  app.get('/api/analytics', async (req, res) => {
    const svc = getService('lichess');
    if (!svc) return res.status(503).json({ error: 'Lichess not configured' });

    const { period = 'week', result = 'all', max: maxStr = '200' } = req.query;
    const max = Math.min(parseInt(maxStr, 10) || 200, 500);

    try {
      const botId = await _getBotUsername();

      const PERIOD_MS = { today: 86_400_000, week: 7 * 86_400_000, month: 30 * 86_400_000 };
      const since = PERIOD_MS[period] ? Date.now() - PERIOD_MS[period] : undefined;

      const collected = [];
      const pgnParts  = [];

      for await (const game of svc.adapter.exportGames(botId, svc.token, { since, max })) {
        // Determine bot colour and outcome for this game
        const botIsWhite = game.players?.white?.user?.id === botId;
        const botColor   = botIsWhite ? 'white' : 'black';
        const outcome    = game.winner          // 'white' | 'black' | undefined
          ? (game.winner === botColor ? 'win' : 'loss')
          : 'draw';

        // Apply result filter
        if (result !== 'all' && outcome !== result) continue;

        collected.push({
          id:       game.id,
          result:   outcome,
          speed:    game.speed,
          opening:  game.opening?.name ?? null,
          opponent: botIsWhite
            ? (game.players?.black?.user?.name ?? '?')
            : (game.players?.white?.user?.name ?? '?'),
          createdAt: game.createdAt,
        });

        if (game.pgn) pgnParts.push(game.pgn);
      }

      const pgn = pgnParts.join('\n\n');
      return res.json({ count: collected.length, pgn, summary: collected });

    } catch (e) {
      return res.status(502).json({ error: e.message });
    }
  });

  // POST /api/analytics/save  { pgn, filename? }
  // Writes a .pgn file into the <workspace>/analyze/ directory.
  app.post('/api/analytics/save', async (req, res) => {
    const { pgn, filename } = req.body ?? {};
    if (!pgn) return res.status(400).json({ error: 'pgn is required' });

    try {
      await fs.mkdir(ANALYZE_DIR, { recursive: true });

      const ts   = new Date().toISOString().replace(/[:.]/g, '-').replace('T', '_').slice(0, 19);
      const name = filename ?? `games_${ts}.pgn`;
      const file = path.join(ANALYZE_DIR, name);

      await fs.writeFile(file, pgn, 'utf8');
      return res.json({ file: name, bytes: pgn.length });
    } catch (e) {
      return res.status(500).json({ error: e.message });
    }
  });

  // GET /api/analytics/files  → list of .pgn files in analyze/
  app.get('/api/analytics/files', async (_req, res) => {
    try {
      await fs.mkdir(ANALYZE_DIR, { recursive: true });
      const entries = await fs.readdir(ANALYZE_DIR);
      const pgns    = entries.filter(f => f.endsWith('.pgn') || f.endsWith('.PGN'));
      const stats   = await Promise.all(pgns.map(async name => {
        const st = await fs.stat(path.join(ANALYZE_DIR, name));
        return { name, size: st.size, mtime: st.mtime.toISOString() };
      }));
      stats.sort((a, b) => b.mtime.localeCompare(a.mtime));
      return res.json(stats);
    } catch (e) {
      return res.json([]);
    }
  });

  // ── REST: game history ────────────────────────────────────────────────
  //
  // GET /api/gamedb?service=&month=YYYY-MM&result=1-0|0-1|1%2F2-1%2F2&opponent=&limit=&offset=
  //   → array of game_history index rows (fast SQLite query, no PGN parsing)
  //
  // GET /api/gamedb/stats?service=
  //   → { total, wins, losses, draws, unfinished } aggregates
  //
  // GET /api/gamedb/game/:id/pgn
  //   → download single-game PGN with hashed filename
  //
  // GET /api/gamedb/pgn/:month
  //   → download full annotated PGN file for a month

  const gameDb  = require('./gameDb');
  const fsSync  = require('fs');
  const dataDir = path.join(__dirname, '..', '..', 'data');

  /** djb2 hash → stable 8-char hex filename per game id */
  function _hashGameId(id) {
    let h = 5381;
    for (let i = 0; i < id.length; i++) h = (((h << 5) + h) ^ id.charCodeAt(i)) >>> 0;
    return h.toString(16).padStart(8, '0');
  }

  app.get('/api/gamedb', (req, res) => {
    try {
      const { service, month, result, opponent, bot_result, limit = '50', offset = '0' } = req.query;
      return res.json(gameDb.queryGames({
        service:    service    || undefined,
        month:      month      || undefined,
        result:     result     || undefined,
        opponent:   opponent   || undefined,
        bot_result: bot_result || undefined,
        limit:  Math.min(parseInt(limit,  10) || 50, 500),
        offset: parseInt(offset, 10) || 0,
      }));
    } catch (e) {
      return res.status(500).json({ error: e.message });
    }
  });

  app.get('/api/gamedb/game/:id/pgn', (req, res) => {
    try {
      const pgn = gameDb.getGamePgn(req.params.id);
      if (!pgn) return res.status(404).json({ error: 'Game PGN not found' });
      const hash = _hashGameId(req.params.id);
      res.setHeader('Content-Type',        'application/x-chess-pgn');
      res.setHeader('Content-Disposition', `attachment; filename="game-${hash}.pgn"`);
      return res.send(pgn);
    } catch (e) {
      return res.status(500).json({ error: e.message });
    }
  });

  app.get('/api/gamedb/game/:id', (req, res) => {
    try {
      const game = gameDb.getGame(req.params.id);
      if (!game) return res.status(404).json({ error: 'Game not found' });
      return res.json(game);
    } catch (e) {
      return res.status(500).json({ error: e.message });
    }
  });

  app.get('/api/gamedb/stats', (req, res) => {
    try {
      return res.json(gameDb.getStats(req.query.service || undefined));
    } catch (e) {
      return res.status(500).json({ error: e.message });
    }
  });

  app.get('/api/gamedb/pgn/:month', (req, res) => {
    const { month } = req.params;
    if (!/^\d{4}-\d{2}$/.test(month)) {
      return res.status(400).json({ error: 'Invalid month — expected YYYY-MM' });
    }
    const filePath = gameDb.pgnFilePath(month);
    if (!fsSync.existsSync(filePath)) {
      return res.status(404).json({ error: `No games found for ${month}` });
    }
    res.setHeader('Content-Type',        'application/x-chess-pgn');
    res.setHeader('Content-Disposition', `attachment; filename="bot-games-${month}.pgn"`);
    return res.sendFile(filePath);
  });

  // ── REST: selfplay match orchestrator ────────────────────────────────
  // All routes guarded: selfplay service must be configured.

  function _spAdapter() {
    const svc = getService('selfplay');
    return svc?.adapter ?? null;
  }

  // GET  /api/selfplay/engines   → engine registry with availability
  app.get('/api/selfplay/engines', (_req, res) => {
    const adapter = _spAdapter();
    if (!adapter) return res.status(503).json({ error: 'selfplay not configured' });
    return res.json(adapter.getEngineRegistry());
  });

  // GET  /api/selfplay/positions → all position categories + positions
  app.get('/api/selfplay/positions', (_req, res) => {
    const adapter = _spAdapter();
    if (!adapter) return res.status(503).json({ error: 'selfplay not configured' });
    return res.json(adapter.getPositionCategories());
  });

  // POST /api/selfplay/categories          → create category { name }
  app.post('/api/selfplay/categories', (req, res) => {
    const { name } = req.body ?? {};
    if (!name?.trim()) return res.status(400).json({ error: 'name required' });
    const positionStore = require('./positionStore');
    return res.json(positionStore.createCategory(name.trim()));
  });

  // DELETE /api/selfplay/categories/:catId → delete category
  app.delete('/api/selfplay/categories/:catId', (req, res) => {
    const positionStore = require('./positionStore');
    return res.json(positionStore.deleteCategory(req.params.catId));
  });

  // POST /api/selfplay/categories/:catId/positions → add position { name, fen }
  app.post('/api/selfplay/categories/:catId/positions', (req, res) => {
    const { name, fen } = req.body ?? {};
    if (!name?.trim()) return res.status(400).json({ error: 'name required' });
    if (!fen?.trim())  return res.status(400).json({ error: 'fen required' });
    const positionStore = require('./positionStore');
    return res.json(positionStore.addPosition(req.params.catId, name.trim(), fen.trim()));
  });

  // PUT /api/selfplay/categories/:catId/positions/:posId → update position { name?, fen? }
  app.put('/api/selfplay/categories/:catId/positions/:posId', (req, res) => {
    const { name, fen } = req.body ?? {};
    const positionStore = require('./positionStore');
    return res.json(positionStore.updatePosition(req.params.catId, req.params.posId, { name, fen }));
  });

  // DELETE /api/selfplay/categories/:catId/positions/:posId → delete position
  app.delete('/api/selfplay/categories/:catId/positions/:posId', (req, res) => {
    const positionStore = require('./positionStore');
    return res.json(positionStore.deletePosition(req.params.catId, req.params.posId));
  });

  // GET  /api/selfplay/state     → live match state + standings
  app.get('/api/selfplay/state', (_req, res) => {
    const adapter = _spAdapter();
    if (!adapter) return res.status(503).json({ error: 'selfplay not configured' });
    return res.json(adapter.getMatchState());
  });

  // POST /api/selfplay/config    → update match config (persisted)
  app.post('/api/selfplay/config', (req, res) => {
    const adapter = _spAdapter();
    if (!adapter) return res.status(503).json({ error: 'selfplay not configured' });
    const { mode, enabledEngines, enabledPositions, tc, ponder, tmMode, movetimeMs } = req.body ?? {};
    adapter.configureMatch({ mode, enabledEngines, enabledPositions, tc, ponder, tmMode, movetimeMs });
    return res.json({ ok: true, state: adapter.getMatchState() });
  });

  // POST /api/selfplay/start     → start the match loop
  app.post('/api/selfplay/start', async (req, res) => {
    const adapter = _spAdapter();
    if (!adapter) return res.status(503).json({ error: 'selfplay not configured' });
    // Accept optional one-shot config with start command
    const { mode, enabledEngines, enabledPositions, tc, ponder, tmMode, movetimeMs } = req.body ?? {};
    if (mode || enabledEngines || enabledPositions || tc || ponder !== undefined || tmMode || movetimeMs) {
      adapter.configureMatch({ mode, enabledEngines, enabledPositions, tc, ponder, tmMode, movetimeMs });
    }
    try {
      const result = await adapter.startMatch();
      if (!result.ok) return res.status(400).json(result);
      return res.json({ ok: true, state: adapter.getMatchState() });
    } catch (e) {
      return res.status(500).json({ error: e.message });
    }
  });

  // POST /api/selfplay/stop      → stop the match loop after current game
  app.post('/api/selfplay/stop', async (req, res) => {
    const adapter = _spAdapter();
    if (!adapter) return res.status(503).json({ error: 'selfplay not configured' });
    try {
      await adapter.stopMatch();
      return res.json({ ok: true, state: adapter.getMatchState() });
    } catch (e) {
      return res.status(500).json({ error: e.message });
    }
  });

  return app;
}

function startDashboard(port) {
  const app = createDashboard();
  app.listen(port, () => {
    console.log(`[dashboard] http://localhost:${port}`);
  });
  return app;
}

module.exports = { startDashboard };
