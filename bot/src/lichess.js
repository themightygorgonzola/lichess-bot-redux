'use strict';

/**
 * lichess.js — Lichess API client.
 *
 * Exports:
 *   streamNDJSON(path, token)     async generator of parsed objects
 *   api(method, path, token, body?) → parsed JSON response
 *
 * And convenience wrappers for all endpoints the bot needs.
 */

const BASE = 'https://lichess.org';

// ── core ──────────────────────────────────────────────────────────────────

/**
 * Streams an NDJSON endpoint, yielding one parsed object per line.
 * Empty heartbeat lines (keep-alive `\n`) are skipped.
 */
async function* streamNDJSON(urlPath, token) {
  const res = await fetch(`${BASE}${urlPath}`, {
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/x-ndjson',
    },
  });

  if (!res.ok) {
    throw new Error(`Stream ${urlPath} failed: ${res.status} ${res.statusText}`);
  }

  const decoder = new TextDecoder();
  let buf = '';

  for await (const chunk of res.body) {
    buf += decoder.decode(chunk, { stream: true });
    const lines = buf.split('\n');
    buf = lines.pop(); // keep incomplete trailing line
    for (const line of lines) {
      const trimmed = line.trim();
      if (trimmed) {
        try {
          yield JSON.parse(trimmed);
        } catch (e) {
          console.warn('[lichess] NDJSON parse error:', e.message, '| line:', trimmed.slice(0, 80));
        }
      }
    }
  }
}

/**
 * Make a regular REST call (non-streaming).
 */
async function api(method, urlPath, token, body = null) {
  const opts = {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/json',
    },
  };
  if (body !== null) {
    if (typeof body === 'string') {
      opts.headers['Content-Type'] = 'application/x-www-form-urlencoded';
      opts.body = body;
    } else {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
  }
  const res = await fetch(`${BASE}${urlPath}`, opts);
  const text = await res.text();
  try {
    return { ok: res.ok, status: res.status, data: JSON.parse(text) };
  } catch (_) {
    return { ok: res.ok, status: res.status, data: text };
  }
}

// ── event stream ──────────────────────────────────────────────────────────

function streamEvents(token) {
  return streamNDJSON('/api/stream/event', token);
}

// ── bot game stream ───────────────────────────────────────────────────────

function streamGame(gameId, token) {
  return streamNDJSON(`/api/bot/game/stream/${gameId}`, token);
}

// ── challenges ────────────────────────────────────────────────────────────

async function acceptChallenge(challengeId, token) {
  return api('POST', `/api/challenge/${challengeId}/accept`, token);
}

async function declineChallenge(challengeId, token, reason = 'generic') {
  return api('POST', `/api/challenge/${challengeId}/decline`, token, `reason=${reason}`);
}

async function cancelChallenge(challengeId, token) {
  return api('POST', `/api/challenge/${challengeId}/cancel`, token);
}

// ── bot moves ─────────────────────────────────────────────────────────────

async function makeMove(gameId, move, token, { offeringDraw = false } = {}) {
  const q = offeringDraw ? '?offeringDraw=true' : '';
  return api('POST', `/api/bot/game/${gameId}/move/${move}${q}`, token);
}

// ── chat ──────────────────────────────────────────────────────────────────

async function chat(gameId, token, text, room = 'player') {
  return api('POST', `/api/bot/game/${gameId}/chat`, token, `room=${room}&text=${encodeURIComponent(text)}`);
}

// ── game lifecycle ────────────────────────────────────────────────────────

async function resignGame(gameId, token) {
  return api('POST', `/api/bot/game/${gameId}/resign`, token);
}

async function abortGame(gameId, token) {
  return api('POST', `/api/bot/game/${gameId}/abort`, token);
}

// ── draw offers ───────────────────────────────────────────────────────────

async function handleDraw(gameId, accept, token) {
  return api('POST', `/api/bot/game/${gameId}/draw/${accept ? 'yes' : 'no'}`, token);
}

// ── takebacks ─────────────────────────────────────────────────────────────

async function handleTakeback(gameId, accept, token) {
  return api('POST', `/api/bot/game/${gameId}/takeback/${accept ? 'yes' : 'no'}`, token);
}

// ── claim win / draw ──────────────────────────────────────────────────────

async function claimVictory(gameId, token) {
  return api('POST', `/api/bot/game/${gameId}/claim-victory`, token);
}

async function claimDraw(gameId, token) {
  return api('POST', `/api/bot/game/${gameId}/claim-draw`, token);
}

// ── account info ──────────────────────────────────────────────────────────

async function getAccount(token) {
  return api('GET', '/api/account', token);
}

// ── user info ────────────────────────────────────────────────────────────

async function getUser(username, token) {
  return api('GET', `/api/user/${encodeURIComponent(username)}`, token);
}

// ── online bots ───────────────────────────────────────────────────────────

/**
 * Fetch up to `nb` online BOT accounts as a JSON array.
 * The /api/bot/online endpoint is public (no auth required), but passing a
 * valid token is harmless and keeps the request authenticated.
 */
async function fetchOnlineBots(nb = 50, token = '') {
  const headers = { Accept: 'application/x-ndjson' };
  if (token) headers['Authorization'] = `Bearer ${token}`;

  const res = await fetch(`${BASE}/api/bot/online?nb=${nb}`, { headers });
  if (!res.ok) throw new Error(`/api/bot/online failed: ${res.status} ${res.statusText}`);

  const bots = [];
  const decoder = new TextDecoder();
  let buf = '';
  for await (const chunk of res.body) {
    buf += decoder.decode(chunk, { stream: true });
    const lines = buf.split('\n');
    buf = lines.pop();
    for (const line of lines) {
      const t = line.trim();
      if (t) { try { bots.push(JSON.parse(t)); } catch (_) {} }
    }
  }
  return bots;
}

// ── game export ──────────────────────────────────────────────────────────

/**
 * Stream all games for a user as NDJSON (one game object per iteration).
 * Each object includes a `pgn` field when pgnInJson=true.
 *
 * @param {string} username
 * @param {string} token
 * @param {object} [opts]
 * @param {number} [opts.since]    epoch ms — only games after this time
 * @param {number} [opts.until]    epoch ms — only games before this time
 * @param {number} [opts.max=200]  max number of games
 */
function exportGames(username, token, { since, until, max = 200 } = {}) {
  const params = new URLSearchParams({
    max:       String(Math.min(max, 500)),
    moves:     'true',
    tags:      'true',
    clocks:    'false',
    evals:     'false',
    opening:   'true',
    pgnInJson: 'true',
  });
  if (since) params.set('since', String(since));
  if (until) params.set('until', String(until));
  return streamNDJSON(`/api/games/user/${encodeURIComponent(username)}?${params}`, token);
}

// ── outgoing challenges ───────────────────────────────────────────────────

/**
 * Send a challenge to another user.
 * @param {string} username  Target username.
 * @param {string} token     Bot token.
 * @param {object} [opts]
 * @param {number} [opts.timeLimit=300]  Clock seconds.
 * @param {number} [opts.increment=3]   Increment seconds.
 * @param {boolean}[opts.rated=false]   Rated game.
 * @param {string} [opts.color='random'] 'random' | 'white' | 'black'.
 * @param {string} [opts.variant='standard']
 */
async function challengeUser(username, token, {
  timeLimit = 300,
  increment = 3,
  rated     = false,
  color     = 'random',
  variant   = 'standard',
} = {}) {
  const body = [
    `rated=${rated}`,
    `clock.limit=${timeLimit}`,
    `clock.increment=${increment}`,
    `color=${color}`,
    `variant=${variant}`,
  ].join('&');
  return api('POST', `/api/challenge/${encodeURIComponent(username)}`, token, body);
}

module.exports = {
  streamNDJSON,
  api,
  streamEvents,
  streamGame,
  acceptChallenge,
  declineChallenge,
  cancelChallenge,
  makeMove,
  chat,
  resignGame,
  abortGame,
  handleDraw,
  handleTakeback,
  claimVictory,
  claimDraw,
  getAccount,
  getUser,
  fetchOnlineBots,
  challengeUser,
  exportGames,
};
