'use strict';

/**
 * game.js â€” GameHandler: manages one active Lichess game end-to-end.
 *
 * Lifecycle:
 *   1. Constructed with gameId, color, token, engineConfig
 *   2. run() streams game events, drives the engine, posts moves
 *   3. Handles draw offers, takebacks, opponent abandonment, resignation
 *   4. Cleans up the engine process when the game ends
 *
 * All behavioural decisions are delegated to the policies module so they
 * can be tuned or swapped independently.
 *
 * The GameHandler is service-agnostic: it talks to Lichess, Chess.com, or
 * any future platform through the ServiceAdapter interface passed at
 * construction time.
 */

const { Engine }   = require('./engine');
const store        = require('./store');
const policies     = require('./policies');
const personality  = require('./personality');
const book         = require('./book');
const analysisSf   = require('./analysisSf');
const { applyMoves, STARTING_FEN } = require('./fen');

class GameHandler {
  /**
   * @param {string} gameId
   * @param {string} color        'white' | 'black'
   * @param {string} token        Auth token for the platform
   * @param {object} engineConfig Engine settings
   * @param {import('./services/ServiceAdapter')} service  Platform adapter
   */
  constructor(gameId, color, token, engineConfig, service) {
    this.gameId        = gameId;
    this.color         = color;   // 'white' | 'black'
    this.token         = token;
    this.service       = service; // ServiceAdapter instance
    this.engineConfig  = engineConfig;
    this.engine        = new Engine(engineConfig.path, {
      affinityMask: engineConfig.affinityMask ?? null,
      threads:  engineConfig.threads,
      hash:     engineConfig.hash,
      evalFile: engineConfig.evalFile ?? null,
      useNnue:  engineConfig.useNnue  ?? true,
    });
    this._running          = false;
    this._greeted          = false;
    this._drawOffered      = false;   // track whether *opponent* has a pending draw offer
    this._recentResults    = [];      // sliding window of our recent engine results
    this._initialFen       = STARTING_FEN;
    this._clock            = null;    // latest { wtime, btime, winc, binc }
    this._ponderEnabled    = engineConfig.ponder ?? false;
    this._ponderMove       = null;    // engine's predicted opponent reply (for ponderhit)
    this._speed            = null;    // filled from gameFull event
    this._dominancePosted  = false;   // spectator comment when we are crushing
    this._strugglePosted   = false;   // spectator comment when we are losing badly
    this._prevSearchEval   = null;    // eval (cp) from our last completed search
    this._lastMoveInfo     = null;    // info snapshot from our last completed search (seed for next)
    this._mateAnnounced    = false;   // true once we have announced a forced mate
    this._lastBlunderPly   = -99;     // ply of last blunder taunt (cooldown)
    this._blunderTauntCount = 0;      // hard cap: max 2 taunts per game
    this._lastDrawOfferPly  = -99;    // ply of last draw offer we sent (re-offer cooldown)
  }

  // â”€â”€ main loop â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

  async run() {
    this._running = true;
    const MAX_STREAM_RETRIES = 5;
    const STREAM_RETRY_BASE_MS = 2_000;
    let streamErrors = 0;

    try {
      await this.engine.init();

      while (this._running) {
        try {
          const stream = this.service.streamGame(this.gameId, this.token);
          streamErrors = 0;   // reset on every successful connection

          for await (const event of stream) {
            if (!this._running) break;
            switch (event.type) {
              case 'gameFull':      await this._onGameFull(event);    break;
              case 'gameState':     await this._onGameState(event);   break;
              case 'chatLine':      this._onChatLine(event);          break;
              case 'opponentGone':  await this._onOpponentGone(event); break;
            }
          }
          break;  // clean stream end â†’ game over, exit while
        } catch (err) {
          streamErrors++;
          if (!this._running || streamErrors > MAX_STREAM_RETRIES) {
            console.error(`[game ${this.gameId}] stream failed after ${streamErrors} attempt(s):`, err.message);
            break;
          }
          const delay = STREAM_RETRY_BASE_MS * streamErrors;
          console.warn(`[game ${this.gameId}] stream dropped (attempt ${streamErrors}/${MAX_STREAM_RETRIES}), reconnecting in ${delay}msâ€¦`, err.message);
          await new Promise(r => setTimeout(r, delay));
        }
      }
    } catch (err) {
      console.error(`[game ${this.gameId}] fatal error:`, err.message);
    } finally {
      await this.cleanup();
    }
  }

  // â”€â”€ event handlers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

  async _onGameFull(event) {
    // Stash the initial FEN (matters for Chess960 / from-position games)
    this._initialFen = event.initialFen === 'startpos'
      ? STARTING_FEN
      : (event.initialFen ?? STARTING_FEN);

    // Extract clock info from the gameFull envelope
    if (event.clock) {
      this._clock = {
        wtime: event.state?.wtime ?? event.clock.initial,
        btime: event.state?.btime ?? event.clock.initial,
        winc:  event.state?.winc  ?? event.clock.increment ?? 0,
        binc:  event.state?.binc  ?? event.clock.increment ?? 0,
      };
    }

    // Stash game speed for dynamic time management
    this._speed = event.speed ?? 'rapid';

    // Extract ratings and time control for dashboard
    const isWhite = this.color === 'white';
    const ourPlayer  = isWhite ? event.white : event.black;
    const oppPlayer  = isWhite ? event.black : event.white;
    store.updateGameMeta(this.gameId, {
      ourRating:   ourPlayer?.rating  ?? null,
      oppRating:   oppPlayer?.rating  ?? null,
      rated:       event.rated ?? false,
      timeControl: event.clock
        ? `${Math.floor((event.clock.initial ?? 0) / 60000)}+${Math.floor((event.clock.increment ?? 0) / 1000)}`
        : 'correspondence',
      initialFen:  this._initialFen,
    });

    // Send a greeting at the start of the game
    if (!this._greeted) {
      this._greeted = true;
      const playerMsg = personality.greeting();
      if (playerMsg) {
        this.service.chat(this.gameId, this.token, playerMsg, 'player').catch(() => {});
      }
      const specMsg = personality.spectatorGreeting();
      if (specMsg) {
        this.service.chat(this.gameId, this.token, specMsg, 'spectator').catch(() => {});
      }
    }

    await this._onGameState(event.state, event);
  }

  async _onGameState(state, _fullEvent = null) {
    if (!this._running) return;

    // â”€â”€ Update clock â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if (state.wtime != null) {
      this._clock = {
        wtime: state.wtime,
        btime: state.btime,
        winc:  state.winc  ?? 0,
        binc:  state.binc  ?? 0,
      };
      store.updateClock(this.gameId, state.wtime, state.btime);
    }

    // â”€â”€ Check if game is over â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    const status = state.status;
    if (status && !['created', 'started'].includes(status)) {
      const result = deriveResult(status, state.winner);
      this._running = false;
      // Drain any in-flight SF analysis before persisting the game record.
      // Self-play games don't use the external SF analyser, so a short timeout suffices.
      const drainMs = this.service.constructor?.name === 'SelfPlayAdapter' ? 300 : 2000;
      await analysisSf.drain(drainMs);
      store.endGame(this.gameId, result, status);
      // Send end-game message (win / loss / draw â€” context-aware)
      const endMsg = personality.gameOver(result, this.color, status);
      if (endMsg) this.service.chat(this.gameId, this.token, endMsg, 'player').catch(() => {});
      return;
    }

    // â”€â”€ Handle incoming draw offer â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    // The gameState event includes wdraw / bdraw booleans when a player
    // has offered a draw.  Detect if the *opponent* has offered one.
    const theirDrawField = this.color === 'white' ? 'bdraw' : 'wdraw';
    if (state[theirDrawField]) {
      this._drawOffered = true;
    } else {
      this._drawOffered = false;
    }

    // â”€â”€ Handle incoming takeback â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    const theirTakebackField = this.color === 'white' ? 'btakeback' : 'wtakeback';
    if (state[theirTakebackField]) {
      const accept = policies.shouldAcceptTakeback();
      console.log(`[game ${this.gameId}] opponent requests takeback â†’ ${accept ? 'accept' : 'decline'}`);
      this.service.handleTakeback(this.gameId, accept, this.token).catch(() => {});
    }

    // â”€â”€ Is it my turn? â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    const moves = state.moves ? state.moves.split(' ').filter(Boolean) : [];
    const isWhiteTurn = moves.length % 2 === 0;
    const myTurn = (this.color === 'white' && isWhiteTurn) ||
                   (this.color === 'black' && !isWhiteTurn);

    // â”€â”€ Update FEN for the dashboard board display â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    try {
      const currentFen = applyMoves(this._initialFen, moves);
      store.updateFen(this.gameId, currentFen);
    } catch (_) { /* best-effort FEN tracking */ }

    if (!myTurn) return;

    // â”€â”€ Record opponent's move that triggered our turn â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    const opMove = moves.length > 0 ? moves[moves.length - 1] : null;
    if (opMove) store.recordOpponentMove(this.gameId, opMove, moves.length - 1);

    // â”€â”€ Build search profile for this move â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    const opponentTimeMs = this._clock
      ? (this.color === 'white' ? this._clock.btime : this._clock.wtime) ?? null
      : null;
    const profile = policies.getSearchProfile(
      this._speed, this._clock, this.color, moves.length, {}, opponentTimeMs,
    );

    // ── Opening book probe ───────────────────────────────────────────────────
    // Fast path: if the position appears in the master games database, play the
    // book move instantly without consulting the engine.
    {
      const bookFen = (() => { try { return applyMoves(this._initialFen, moves); } catch (_) { return null; } })();
      if (bookFen) {
        const bookMove = await book.probe(bookFen, moves.length, this.token);
        if (bookMove) {
          const bookPly = moves.length + 1;
          console.log(`[game ${this.gameId}] ply=${bookPly} BOOK move=${bookMove} (cache=${book.cacheSize()} entries)`);
          if (this.engine.isPondering()) {
            store.ponderEnd(this.gameId);
            await this.engine.cancelPonder(500);
          }
          let resp = await this.service.makeMove(this.gameId, bookMove, this.token, {});
          if (!resp.ok) {
            if (resp.status >= 500 || resp.status === 0) {
              await new Promise(r => setTimeout(r, 1_000));
              resp = await this.service.makeMove(this.gameId, bookMove, this.token, {});
            }
            if (!resp.ok) {
              console.error(`[game ${this.gameId}] book makeMove failed (${resp.status}):`, resp.data);
              return;
            }
          }
          store.recordFullMove(this.gameId, bookMove, moves.length);
          try {
            const afterFen = applyMoves(this._initialFen, [...moves, bookMove]);
            store.updateFen(this.gameId, afterFen);
          } catch (_) { /* best-effort */ }
          store.recordMove(this.gameId, {
            move: bookMove, depth: 0, seldepth: 0, nodes: 0, nps: 0,
            time_ms: 0, eval_cp: null, mate: null,
            ply: bookPly, confidence: 1.0, stop_reason: 'book',
          });
          // Prefetch opponent's responses to our book move during their clock.
          try {
            const pfFen = applyMoves(this._initialFen, [...moves, bookMove]);
            book.prefetch(pfFen, bookPly, this.token).catch(() => {});
          } catch (_) { /* best-effort */ }
          // Warm TT during opponent's clock via parent-position ponder.
          this._ponderMove = null;
          this._lastMoveInfo = null;
          if (this._ponderEnabled) {
            const _bookPonderOnInfo = (history) => {
              const info = history[history.length - 1];
              if (info) store.ponderInfo(this.gameId, info, 'opponent');
            };
            this.engine.startPonder(this._initialFen, [...moves, bookMove], _bookPonderOnInfo);
            store.ponderStart(this.gameId, 0, null);
            console.log(`[game ${this.gameId}] book: started parent-position ponder`);
          }
          return;
        }
      }
    }

    store.searchStart(this.gameId, profile, this._lastMoveInfo);

    // Wrap onInfo to relay search data to store for dashboard.
    // C++ now manages all stop decisions; this is display-only.
    let _lastConf = null;
    const wrappedOnInfo = (h, e) => {
      const info = h.length > 0 ? h[h.length - 1] : null;
      _lastConf = policies.computeConfidence(h);
      if (info) store.searchInfo(this.gameId, info, _lastConf, e);
    };

    // ── Think (cancel parent-position ponder, restart with warm TT) ────
    // Ponderhit path: if opponent played exactly what we predicted, convert the
    // running ponder to our real search with no restart overhead.  On miss (or
    // no ponder active), cancel any ponder and start fresh — TT is still warm.
    const isPonderHit = this._ponderEnabled
      && this._ponderMove !== null
      && opMove === this._ponderMove
      && this.engine.isPondering();

    const thinkStart = Date.now();
    const ourClock = this.color === 'white' ? this._clock?.wtime : this._clock?.btime;
    if (isPonderHit) {
      console.log(`[game ${this.gameId}] ply=${moves.length + 1} ponderhit! predicted=${this._ponderMove} clock=${ourClock != null ? Math.floor(ourClock / 1000) + 's' : 'n/a'}`);
    } else {
      const ponderStatus = this._ponderMove
        ? `ponder-miss(exp=${this._ponderMove} got=${opMove ?? '?'})`
        : 'no-ponder';
      console.log(`[game ${this.gameId}] ply=${moves.length + 1} thinking clock=${ourClock != null ? Math.floor(ourClock / 1000) + 's' : 'n/a'} ${ponderStatus}`);
    }

    let result;
    try {
      if (isPonderHit) {
        // Seamless: C++ already searching this position; send ponderhit with live clocks.
        store.ponderEnd(this.gameId);
        result = await this.engine.ponderhit(this._clock);
      } else {
        // Standard: cancel any running ponder then search fresh.
        if (this.engine.isPondering()) {
          store.ponderEnd(this.gameId);
          await this.engine.cancelPonder(150);
        }
        if (!this.engine.isReady()) {
          console.warn(`[game ${this.gameId}] engine not ready, reinitialising...`);
          await this.engine.init();
        }
        result = await this.engine.thinkDynamic(this._initialFen, moves, {
          onInfo: wrappedOnInfo,
          clock: this._clock,
        });
      }
    } catch (err) {
      // If the engine has no legal moves (checkmate / stalemate on our side),
      // submit (none) so the self-play adapter can classify and end the game.
      // Attempting an engine restart on a terminal position would just throw again.
      if (err.message === 'Engine returned no move') {
        console.log(`[game ${this.gameId}] engine has no legal moves — submitting (none)`);
        await this.service.makeMove(this.gameId, '(none)', this.token, {}).catch(() => {});
        return;
      }
      // Engine crashed or ponderhit conversion failed — attempt a cold restart.
      console.error(`[game ${this.gameId}] engine error after ${Date.now() - thinkStart}ms (${err.message}), attempting restart...`);
      try {
        await this.engine.init();
        result = await this.engine.thinkDynamic(this._initialFen, moves, {
          onInfo: wrappedOnInfo,
          clock: this._clock,
        });
      } catch (err2) {
        console.error(`[game ${this.gameId}] engine restart failed:`, err2.message);
        return;
      }
    }

    // Guard: the game may have ended while the engine was thinking
    if (!this._running) return;

    // Wall-clock time spent on OUR clock (thinkStart is set when we receive the
    // opponent's move).  For ponderhit moves `result.time_ms` is the engine's
    // cumulative search time which includes free ponder on the opponent's clock —
    // that inflates PGN %time and wasted-time analysis.  Use wall-clock instead.
    const wallElapsedMs = Date.now() - thinkStart;

    // C++ manages stopping; record a static label for PGN diagnostics.
    const _lastStopReason = 'c++';

    console.log(`[game ${this.gameId}] ply=${moves.length + 1} done: move=${result.move} eval=${result.eval_cp ?? (result.mate != null ? 'M' + result.mate : '?')} depth=${result.depth} elapsed=${wallElapsedMs}ms`);

    // Emit search completion to dashboard
    // Prefer C++'s reported time over JS wall-clock; fall back to wall-clock on
    // ponderhit where result.time_ms includes free ponder time on opponent's clock.
    const engineTimeMs = result.time_ms ?? wallElapsedMs;
    store.searchEnd(this.gameId, result.move, result.ponderMove, engineTimeMs, wallElapsedMs, _lastConf);

    // Save info snapshot for seeding the dashboard at the start of the next search.
    // This is emitted immediately after the next search_start so there's no blank
    // period while waiting for the engine's first depth line.
    this._lastMoveInfo = {
      depth:    result.depth,
      seldepth: result.seldepth,
      nodes:    result.nodes,
      nps:      result.nps,
      time_ms:  result.time_ms,
      eval_cp:  result.eval_cp,
      mate:     result.mate,
      pv0:      result.pv0,
      pv:       result.pv,
    };

    // Track result for draw-offer streak logic
    this._recentResults.push({
      eval_cp: result.eval_cp,
      mate:    result.mate,
    });
    if (this._recentResults.length > 10) this._recentResults.shift();

    // â”€â”€ Resign check â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if (policies.shouldResign(result)) {
      console.log(`[game ${this.gameId}] resigning (mate ${result.mate})`);
      const resignMsg = personality.onResign();
      if (resignMsg) this.service.chat(this.gameId, this.token, resignMsg, 'player').catch(() => {});
      await this.service.resignGame(this.gameId, this.token);
      return;
    }

    // â”€â”€ Respond to opponent draw offer (now that we have eval) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if (this._drawOffered) {
      this._drawOffered = false;  // clear immediately so a re-delivered event doesn't double-fire
      const acceptDraw = policies.shouldAcceptDraw(result);
      console.log(`[game ${this.gameId}] opponent offered draw â†’ ${acceptDraw ? 'accept' : 'decline'}`);
      if (acceptDraw) {
        const acceptMsg = personality.onDrawAccept();
        if (acceptMsg) await this.service.chat(this.gameId, this.token, acceptMsg, 'player').catch(() => {});
      }
      try {
        await this.service.handleDraw(this.gameId, acceptDraw, this.token);
      } catch (err) {
        console.warn(`[game ${this.gameId}] handleDraw error (ignored):`, err.message ?? err);
      }
      if (acceptDraw) return;  // game will end via the stream
    }

    // â”€â”€ Should we offer a draw with this move? â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    // DISABLED: draw offers brick the move loop â€” offer is sent but Lichess
    // holds the connection open waiting for a response, starving the clock.
    const ply    = moves.length + 1;
    const offeringDraw = false; // draw offers disabled (Lichess holds connection open waiting for response)

    // â”€â”€ Spectator commentary when the position is decisive â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    const evalCp = result.eval_cp ?? 0;
    if (ply >= 20) {
      if (!this._dominancePosted && evalCp > 250) {
        this._dominancePosted = true;
        const domMsg = personality.onDominance();
        if (domMsg) this.service.chat(this.gameId, this.token, domMsg, 'spectator').catch(() => {});
      } else if (!this._strugglePosted && evalCp < -250) {
        this._strugglePosted = true;
        const strMsg = personality.onStruggle();
        if (strMsg) this.service.chat(this.gameId, this.token, strMsg, 'spectator').catch(() => {});
      }
    }

    // â”€â”€ Trash talk: mate sequence found â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    // Fire once the first time we find a forced mate in our favour.
    if (result.mate != null && result.mate > 0 && !this._mateAnnounced) {
      this._mateAnnounced = true;
      const msg = personality.onMateFound(result.mate);
      if (msg) this.service.chat(this.gameId, this.token, msg, 'player').catch(() => {});
    }

    // â”€â”€ Trash talk: opponent blunder detection â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    // Fire when our eval improves by >= 100cp since our last move (1 pawn).
    // Cooldown: at least 6 half-moves between taunts.
    // Hard cap: 2 taunts per game so the bot doesn't spam a struggling opponent.
    const BLUNDER_THRESHOLD_CP = 100;
    const BLUNDER_COOLDOWN_PLY = 6;
    const BLUNDER_MAX_TAUNTS   = 2;
    if (
      result.mate == null &&
      result.eval_cp != null &&
      this._prevSearchEval != null &&
      this._blunderTauntCount < BLUNDER_MAX_TAUNTS &&
      (result.eval_cp - this._prevSearchEval) >= BLUNDER_THRESHOLD_CP &&
      (ply - this._lastBlunderPly) >= BLUNDER_COOLDOWN_PLY
    ) {
      this._lastBlunderPly = ply;
      this._blunderTauntCount++;
      const msg = personality.onOpponentBlunder(result.eval_cp - this._prevSearchEval);
      if (msg) this.service.chat(this.gameId, this.token, msg, 'player').catch(() => {});
    }

    // â”€â”€ If offering a draw, say something about it â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if (offeringDraw) {
      const drawOfferMsg = personality.onDrawOffer();
      if (drawOfferMsg) this.service.chat(this.gameId, this.token, drawOfferMsg, 'player').catch(() => {});
    }

    // â”€â”€ Post move to Lichess â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    let resp = await this.service.makeMove(this.gameId, result.move, this.token, { offeringDraw });
    if (!resp.ok) {
      // Retry once on transient server/network errors.
      if (resp.status >= 500 || resp.status === 0) {
        console.warn(`[game ${this.gameId}] makeMove transient error (${resp.status}), retrying in 1sâ€¦`);
        await new Promise(r => setTimeout(r, 1_000));
        resp = await this.service.makeMove(this.gameId, result.move, this.token, { offeringDraw });
      }
      if (!resp.ok) {
        // 400 = illegal move or game already ended on server â€” either way unrecoverable.
        console.error(`[game ${this.gameId}] makeMove failed (${resp.status}):`, resp.data);
        return;
      }
    }

    if (offeringDraw) {
      console.log(`[game ${this.gameId}] offered draw with move ${result.move}`);
    }

    // â”€â”€ Record stats in store â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    // Record in full move timeline (ours) — BEFORE recordMove so the dashboard's
    // 'move' event sees our UCI already in g.fullMoves for correct animation.
    store.recordFullMove(this.gameId, result.move, moves.length);

    // Update FEN to post-move position — also BEFORE recordMove so g.fen is
    // correct when Board.render() runs inside the 'move' event handler.
    let afterFen = null;
    try {
      afterFen = applyMoves(this._initialFen, [...moves, result.move]);
      store.updateFen(this.gameId, afterFen);
    } catch (_) { /* best-effort */ }

    // Fire the 'move' SSE event immediately so the dashboard board updates
    // without waiting for the eval trace round-trip.
    store.recordMove(this.gameId, {
      move:        result.move,
      depth:       result.depth,
      seldepth:    result.seldepth,
      nodes:       result.nodes,
      nps:         result.nps,
      time_ms:     wallElapsedMs,
      eval_cp:     result.eval_cp,
      mate:        result.mate,
      ply:         moves.length + 1,
      confidence:  _lastConf,
      stop_reason: _lastStopReason,
      // Search profile used for this move
      min_ms:      profile.minTimeMs,
      max_ms:      profile.maxTimeMs,
      conf_thresh: profile.confidenceThreshold,
      emergency:   profile.emergency ?? false,
      // Clock state at the start of this search
      clock_before: { wtime: this._clock?.wtime ?? null, btime: this._clock?.btime ?? null },
      // eval_vec / sf_eval patched in retroactively below
      eval_vec:      null,
      depth_history: result.depthHistory ?? null,
      // sf_eval / sf_depth filled retroactively by SelfPlayAdapter.updateLastMoveSfEval
      fen:           afterFen,
    });

    // -- Eval trace: fetch structured eval vector in the background so it
    // does NOT delay the move event. Patched into the last move record when ready.
    if (afterFen) {
      const gameId = this.gameId;
      this.engine.getEvalVec(this._initialFen, [...moves, result.move]).then(ev => {
        if (ev) store.updateLastMoveEvalVec(gameId, ev);
      }).catch(() => {});
    }

    // Fire background SF analysis concurrently with pondering.
    // The promise resolves asynchronously and retroactively stamps sf_eval
    // on this move record via updateLastMoveSfEval.
    if (afterFen) {
      const gameId = this.gameId;
      analysisSf.queryEval(afterFen).then(sfResult => {
        if (sfResult) store.updateLastMoveSfEval(gameId, sfResult.eval_cp, sfResult.depth);
      }).catch(() => {});
    }

    // Update prev eval for blunder detection on the next move
    if (result.eval_cp != null) this._prevSearchEval = result.eval_cp;
    // Prefetch book candidates for the position after our engine move (opponent's turn).
    // Fire-and-forget: fills the cache while the opponent thinks.
    try {
      const pfFen = applyMoves(this._initialFen, [...moves, result.move]);
      book.prefetch(pfFen, moves.length + 1, this.token).catch(() => {});
    } catch (_) { /* best-effort */ }
    // ── Start pondering on opponent’s time ────────────────────────────────────
    // Skip pondering in emergency — every ms matters next move, and ponder
    // cancel overhead (up to 200ms+) is unacceptable at critical clock.
    if (this._ponderEnabled && !profile.emergency) {
      // Ponderhit ponder: position is [...moves, ourMove, predictedReply] — WE are
      // to move.  eval_cp is from OUR POV, same as a real search.
      const ponderHitOnInfo = (history) => {
        const info = history[history.length - 1];
        if (info) store.ponderInfo(this.gameId, info, 'ours');
      };

      // Parent-position ponder: position is [...moves, ourMove] — OPPONENT is
      // to move.  eval_cp is from the OPPONENT'S POV.
      const ponderParentOnInfo = (history) => {
        const info = history[history.length - 1];
        if (info) store.ponderInfo(this.gameId, info, 'opponent');
      };

      if (result.ponderMove) {
        // Ponderhit path: search the position after our move + predicted reply.
        // We are to move in that position.  On hit, engine.ponderhit() avoids
        // all restart overhead.  On miss, TT has deep coverage of the predicted line.
        const ponderHitMoves = [...moves, result.move, result.ponderMove];
        this._ponderMove = result.ponderMove;
        this.engine.startPonderHit(this._initialFen, ponderHitMoves, result.ponderMove, this._clock, ponderHitOnInfo);
        console.log(`[game ${this.gameId}] pondering predicted reply ${result.ponderMove}`);
      } else {
        // Engine gave no predicted reply — fall back to parent-position ponder
        // (all opponent replies explored at shallow depth, warms TT broadly).
        this._ponderMove = null;
        this.engine.startPonder(this._initialFen, [...moves, result.move], ponderParentOnInfo);
      }
      store.ponderStart(this.gameId, result.depth ?? 0, result.ponderMove ?? null);
    } else {
      this._ponderMove = null;
    }
  }

  // â”€â”€ chat â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

  _onChatLine(event) {
    const who  = event.username ?? '?';
    const room = event.room     ?? 'player';
    const text = event.text     ?? '';
    console.log(`[game ${this.gameId}] chat (${room}) ${who}: ${text}`);
    store.recordChat(this.gameId, room, who, text);
  }

  // â”€â”€ opponent abandonment â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

  async _onOpponentGone(event) {
    if (!this._running) return;

    const gone = event.gone ?? false;
    const claimableIn = event.claimWinInSeconds ?? null;

    if (!gone) return;

    console.log(`[game ${this.gameId}] opponent gone (claimable in ${claimableIn}s)`);

    if (claimableIn != null && claimableIn <= 0 && policies.shouldClaimVictory()) {
      console.log(`[game ${this.gameId}] claiming victory`);
      const resp = await this.service.claimVictory(this.gameId, this.token);
      if (!resp.ok) {
        console.warn(`[game ${this.gameId}] claim-victory failed:`, resp.status, resp.data);
      }
    }
  }

  // â”€â”€ cleanup â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

  async cleanup() {
    this._running = false;
    try {
      if (this.engine.isPondering()) {
        await this.engine.cancelPonder().catch(() => {});
      }
      await this.engine.quit();
    } catch (_) { /* already dead */ }
  }

  stop() {
    this._running = false;
    // Kill the engine process immediately — don't wait for cleanup() to be
    // called asynchronously.  This ensures no orphaned engine processes survive
    // a bot crash or SIGINT/SIGTERM shutdown.
    if (this.engine && this.engine._proc) {
      try { this.engine._proc.kill(); } catch (_) {}
    }
  }
}

// â”€â”€ helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

function deriveResult(status, winner) {
  if (status === 'draw' || status === 'stalemate' || status === 'repetition' ||
      status === 'insufficient' || status === 'insufficientMaterial' ||
      status === 'fiftyMoves') {
    return '1/2-1/2';
  }
  if (winner === 'white') return '1-0';
  if (winner === 'black') return '0-1';
  return null;
}

module.exports = GameHandler;
