'use strict';

/**
 * ChessComAdapter — ServiceAdapter stub for Chess.com.
 *
 * Chess.com does not yet offer a public bot/streaming API equivalent to
 * Lichess's bot API. This adapter provides the structural scaffolding so
 * that when the API becomes available, only this file needs to be filled in.
 *
 * Current status: STUB — all methods throw or return placeholders.
 * The bot framework will skip this service if CHESSCOM_TOKEN is not set.
 *
 * Chess.com API docs: https://www.chess.com/news/view/published-data-api
 * (The published API is read-only; a bot/play API is not yet public.)
 */

const ServiceAdapter = require('./ServiceAdapter');

const BASE = 'https://api.chess.com';

class ChessComAdapter extends ServiceAdapter {
  name() { return 'Chess.com'; }
  id()   { return 'chesscom'; }

  async authenticate(token) {
    // TODO: Chess.com bot authentication when API is available
    // For now, return a placeholder that indicates the service is not yet supported
    return {
      ok:       false,
      status:   501,
      username: null,
      isBot:    false,
      data:     { error: 'Chess.com bot API not yet available' },
    };
  }

  async *streamEvents(_token) {
    // Chess.com doesn't have a streaming event API yet.
    // When available, this would long-poll or websocket for challenges/game starts.
    throw new Error('Chess.com event streaming not yet implemented');
  }

  async *streamGame(_gameId, _token) {
    // Would stream game state updates (moves, clock, chat)
    throw new Error('Chess.com game streaming not yet implemented');
  }

  // ── Challenges ──────────────────────────────────────────────────────
  async acceptChallenge(_id, _token) {
    throw new Error('Chess.com challenge accept not yet implemented');
  }
  async declineChallenge(_id, _token, _reason) {
    throw new Error('Chess.com challenge decline not yet implemented');
  }

  // ── In-game ─────────────────────────────────────────────────────────
  async makeMove(_gameId, _move, _token, _opts) {
    throw new Error('Chess.com makeMove not yet implemented');
  }
  async chat(_gameId, _token, _text, _room) {
    // Chess.com chat is different — may not support bot chat at all
    return { ok: false, status: 501, data: 'not implemented' };
  }
  async resignGame(_gameId, _token) {
    throw new Error('Chess.com resign not yet implemented');
  }
  async abortGame(_gameId, _token) {
    throw new Error('Chess.com abort not yet implemented');
  }
  async handleDraw(_gameId, _accept, _token) {
    throw new Error('Chess.com draw handling not yet implemented');
  }
  async handleTakeback(_gameId, _accept, _token) {
    throw new Error('Chess.com takeback handling not yet implemented');
  }
  async claimVictory(_gameId, _token) {
    throw new Error('Chess.com claim victory not yet implemented');
  }

  // ── Account / discovery ─────────────────────────────────────────────
  async getAccount(_token) {
    return { ok: false, status: 501, data: { error: 'Chess.com bot API not yet available' } };
  }

  async fetchOnlineBots(_nb, _token) {
    return [];
  }

  normalizeBot(raw) {
    const name = raw.username ?? raw.id;
    return {
      username:   name,
      service:    'chesscom',
      ratings: {
        bullet: raw.chess_bullet?.last?.rating ?? null,
        blitz:  raw.chess_blitz?.last?.rating  ?? null,
        rapid:  raw.chess_rapid?.last?.rating  ?? null,
      },
      profileUrl: `https://www.chess.com/member/${name}`,
    };
  }

  normalizeAccount(data) {
    return {
      username: data.username ?? data.id,
      ratings: {
        bullet: data.chess_bullet?.last?.rating ?? null,
        blitz:  data.chess_blitz?.last?.rating  ?? null,
        rapid:  data.chess_rapid?.last?.rating  ?? null,
      },
    };
  }

  profileUrl(username) { return `https://www.chess.com/member/${username}`; }

  // ── Normalization stubs ─────────────────────────────────────────────

  normalizeGameStart(event) {
    // Chess.com event shape TBD — placeholder
    return {
      gameId:    event.id ?? event.gameId ?? '?',
      color:     event.color ?? 'white',
      opponent: {
        id:    event.opponent?.id ?? '?',
        name:  event.opponent?.username ?? '?',
        isBot: false,
      },
      variant:   'standard',
      speed:     event.speed ?? 'unknown',
    };
  }

  normalizeGameState(event) {
    return {
      moves:     event.moves ?? [],
      status:    event.status,
      winner:    event.winner ?? null,
      wtime:     event.wtime ?? null,
      btime:     event.btime ?? null,
      winc:      event.winc  ?? 0,
      binc:      event.binc  ?? 0,
      wdraw:     false,
      bdraw:     false,
      wtakeback: false,
      btakeback: false,
    };
  }
}

module.exports = ChessComAdapter;
