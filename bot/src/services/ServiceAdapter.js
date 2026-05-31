'use strict';

/**
 * ServiceAdapter — Abstract base class for chess platform integrations.
 *
 * Each platform (Lichess, Chess.com) implements this interface.
 * The bot core (index.js, GameHandler) talks ONLY through this interface,
 * never directly to platform APIs.
 *
 * Methods that must be implemented:
 *   - name()                          → platform display name
 *   - async authenticate(token)       → { ok, username, isBot }
 *   - async* streamEvents(token)      → yields { type, ... } events
 *   - async* streamGame(gameId, token)→ yields game events
 *   - async acceptChallenge(id, token)
 *   - async declineChallenge(id, token, reason)
 *   - async cancelChallenge(id, token)
 *   - async makeMove(gameId, move, token, opts)
 *   - async chat(gameId, token, text, room)
 *   - async resignGame(gameId, token)
 *   - async abortGame(gameId, token)
 *   - async handleDraw(gameId, accept, token)
 *   - async handleTakeback(gameId, accept, token)
 *   - async claimVictory(gameId, token)
 *   - async fetchOnlineBots(nb, token)
 *   - async challengeUser(username, token, opts)
 *   - async exportGames(username, token, opts) → async generator
 *   - normalizeGameStart(event)       → { gameId, color, opponent, variant, speed, clock }
 *   - normalizeGameState(event)       → { moves, status, winner, clock, drawOffer, takeback }
 */
class ServiceAdapter {
  /** @returns {string} Platform name for logs/UI */
  name() { throw new Error('ServiceAdapter.name() not implemented'); }

  /** @returns {string} Short identifier: 'lichess' | 'chesscom' */
  id() { throw new Error('ServiceAdapter.id() not implemented'); }

  /** Verify token, return account info */
  async authenticate(_token) { throw new Error('Not implemented'); }

  /** Stream platform events (challenges, game starts, etc.) */
  async *streamEvents(_token) { throw new Error('Not implemented'); }

  /** Stream events for a specific game */
  async *streamGame(_gameId, _token) { throw new Error('Not implemented'); }

  // ── Challenge management ────────────────────────────────────────────
  async acceptChallenge(_id, _token) { throw new Error('Not implemented'); }
  async declineChallenge(_id, _token, _reason) { throw new Error('Not implemented'); }
  async cancelChallenge(_id, _token) { throw new Error('Not implemented'); }

  // ── In-game actions ─────────────────────────────────────────────────
  async makeMove(_gameId, _move, _token, _opts) { throw new Error('Not implemented'); }
  async chat(_gameId, _token, _text, _room) { throw new Error('Not implemented'); }
  async resignGame(_gameId, _token) { throw new Error('Not implemented'); }
  async abortGame(_gameId, _token) { throw new Error('Not implemented'); }
  async handleDraw(_gameId, _accept, _token) { throw new Error('Not implemented'); }
  async handleTakeback(_gameId, _accept, _token) { throw new Error('Not implemented'); }
  async claimVictory(_gameId, _token) { throw new Error('Not implemented'); }

  // ── Account / discovery ─────────────────────────────────────────────
  async getAccount(_token) { throw new Error('Not implemented'); }
  async getUser(_username, _token) { throw new Error('Not implemented'); }
  async fetchOnlineBots(_nb, _token) { return []; }
  async challengeUser(_username, _token, _opts) { throw new Error('Not implemented'); }
  async *exportGames(_username, _token, _opts) { /* no-op */ }

  /**
   * Normalize a raw bot object from the platform into the canonical shape:
   *   { username, service, ratings: { bullet, blitz, rapid }, profileUrl }
   */
  normalizeBot(_raw) { throw new Error('Not implemented'); }

  /**
   * Normalize account/profile data into:
   *   { username, ratings: { bullet, blitz, rapid } }
   */
  normalizeAccount(_data) { throw new Error('Not implemented'); }

  /**
   * Build a profile URL for a username on this platform.
   */
  profileUrl(_username) { return null; }

  // ── Event normalization ─────────────────────────────────────────────
  /** Normalize a gameStart event into the common format used by GameHandler */
  normalizeGameStart(_event) { throw new Error('Not implemented'); }

  /** Normalize a gameState/gameFull sub-event */
  normalizeGameState(_event) { throw new Error('Not implemented'); }
}

module.exports = ServiceAdapter;
