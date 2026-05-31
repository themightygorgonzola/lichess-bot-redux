'use strict';

/**
 * LichessAdapter — ServiceAdapter implementation for Lichess.org.
 *
 * Wraps the existing lichess.js API client behind the ServiceAdapter interface.
 * All Lichess-specific protocol details (NDJSON streaming, endpoint paths,
 * event shapes) are encapsulated here.
 */

const ServiceAdapter = require('./ServiceAdapter');
const lichessApi      = require('../lichess');

class LichessAdapter extends ServiceAdapter {
  name() { return 'Lichess'; }
  id()   { return 'lichess'; }

  async authenticate(token) {
    const res = await lichessApi.getAccount(token);
    return {
      ok:       res.ok,
      status:   res.status,
      username: res.data?.username ?? null,
      isBot:    res.data?.title === 'BOT',
      data:     res.data,
    };
  }

  async *streamEvents(token) {
    yield* lichessApi.streamEvents(token);
  }

  async *streamGame(gameId, token) {
    yield* lichessApi.streamGame(gameId, token);
  }

  // ── Challenges ──────────────────────────────────────────────────────
  async acceptChallenge(id, token)                { return lichessApi.acceptChallenge(id, token); }
  async declineChallenge(id, token, reason)       { return lichessApi.declineChallenge(id, token, reason); }
  async cancelChallenge(id, token)                { return lichessApi.cancelChallenge(id, token); }

  // ── In-game ─────────────────────────────────────────────────────────
  async makeMove(gameId, move, token, opts = {})  { return lichessApi.makeMove(gameId, move, token, opts); }
  async chat(gameId, token, text, room)           { return lichessApi.chat(gameId, token, text, room); }
  async resignGame(gameId, token)                 { return lichessApi.resignGame(gameId, token); }
  async abortGame(gameId, token)                  { return lichessApi.abortGame(gameId, token); }
  async handleDraw(gameId, accept, token)         { return lichessApi.handleDraw(gameId, accept, token); }
  async handleTakeback(gameId, accept, token)     { return lichessApi.handleTakeback(gameId, accept, token); }
  async claimVictory(gameId, token)               { return lichessApi.claimVictory(gameId, token); }

  // ── Account / discovery ─────────────────────────────────────────────
  async getAccount(token)                         { return lichessApi.getAccount(token); }
  async getUser(username, token)                  { return lichessApi.getUser(username, token); }
  async fetchOnlineBots(nb, token)                { return lichessApi.fetchOnlineBots(nb, token); }
  async challengeUser(username, token, opts)      { return lichessApi.challengeUser(username, token, opts); }
  async *exportGames(username, token, opts)       { yield* lichessApi.exportGames(username, token, opts); }

  normalizeBot(raw) {
    const name = raw.username ?? raw.id;
    return {
      username:   name,
      service:    'lichess',
      ratings: {
        bullet: raw.perfs?.bullet?.rating ?? null,
        blitz:  raw.perfs?.blitz?.rating  ?? null,
        rapid:  raw.perfs?.rapid?.rating  ?? null,
      },
      profileUrl: `https://lichess.org/@/${name}`,
    };
  }

  normalizeAccount(data) {
    return {
      username: data.username ?? data.id,
      ratings: {
        bullet: data.perfs?.bullet?.rating ?? null,
        blitz:  data.perfs?.blitz?.rating  ?? null,
        rapid:  data.perfs?.rapid?.rating  ?? null,
      },
    };
  }

  profileUrl(username) { return `https://lichess.org/@/${username}`; }

  // ── Event normalization ─────────────────────────────────────────────

  normalizeGameStart(event) {
    // Lichess gameStart event shape: { game: { gameId, color, opponent, variant, speed, ... } }
    const g = event.game ?? event;
    return {
      gameId:    g.gameId ?? g.id,
      color:     g.color,
      opponent: {
        id:       g.opponent?.id   ?? '?',
        name:     g.opponent?.username ?? '?',
        isBot:    g.opponent?.aiLevel != null,
      },
      variant:   g.variant?.key ?? 'standard',
      speed:     g.speed ?? 'unknown',
    };
  }

  normalizeGameState(event) {
    return {
      moves:      event.moves ? event.moves.split(' ').filter(Boolean) : [],
      status:     event.status,
      winner:     event.winner ?? null,
      wtime:      event.wtime ?? null,
      btime:      event.btime ?? null,
      winc:       event.winc  ?? 0,
      binc:       event.binc  ?? 0,
      wdraw:      event.wdraw ?? false,
      bdraw:      event.bdraw ?? false,
      wtakeback:  event.wtakeback ?? false,
      btakeback:  event.btakeback ?? false,
    };
  }
}

module.exports = LichessAdapter;
