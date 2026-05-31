'use strict';

/**
 * engine.js — UCI subprocess wrapper.
 *
 * One Engine instance per game. Spawns the engine binary, does
 * UCI handshake, and exposes a single async method: think(fen, moves).
 * Returns bestmove + full stats extracted from the last `info depth` line.
 *
 * Usage:
 *   const eng = new Engine('/path/to/engine.exe');
 *   await eng.init();
 *   const result = await eng.thinkDynamic(fen, moves, { onInfo, maxTimeMs });
 *   // { move, ponderMove, depth, seldepth, nodes, nps, time_ms, eval_cp, mate, pv0 }
 *   await eng.quit();
 */

const { spawn }   = require('child_process');
const readline    = require('readline');
const path        = require('path');

class Engine {
  constructor(enginePath, { affinityMask = null, threads = 1, hash = 128, evalFile = null, useNnue = true } = {}) {
    this._path     = path.resolve(__dirname, '..', enginePath);
    this._affinityMask = affinityMask;
    this._threads  = threads;
    this._hash     = hash;
    this._evalFile = evalFile ? path.resolve(__dirname, '..', evalFile) : null;
    this._useNnue  = useNnue;   // false → setoption name UseNNUE value false (HCE-only mode)
    this._proc    = null;
    this._rl      = null;
    this._ready   = false;
    this._queue   = [];          // pending resolve callbacks waiting for lines
    this._lineHandlers = [];     // registered one-shot line handlers
    this._session = null;        // active search/ponder session
  }

  // ── lifecycle ─────────────────────────────────────────────────────────

  async init() {
    console.log(`[engine] starting: ${this._path}`);

    // If restarting (engine crash recovery), close the stale readline so it
    // stops firing _onLine for output from the old (dead) process.  Without
    // this, terminal lines that arrive between the crash and the new spawn
    // would be dispatched to whatever session handlers the new thinkDynamic
    // call registers — potentially resolving the new search with a stale bestmove.
    if (this._rl) {
      try { this._rl.close(); } catch (_) { /* already closed */ }
      this._rl = null;
    }
    // Kill the old process if it's still alive before spawning a new one.
    // Without this, every crash-recovery reinit leaks the previous process.
    if (this._proc) {
      try { this._proc.kill(); } catch (_) {}
      this._proc = null;
    }
    // Clear any leftover session / handlers from the crashed process so the
    // new process starts from a clean slate.
    this._lineHandlers = [];
    this._session      = null;

    // Run the engine with cwd = the bot root (one level above bot/engine/).
    // EvalFile is passed as an absolute path via setoption, so cwd only matters
    // if the engine has its own relative-path lookups at startup.
    const engineDir  = path.dirname(this._path);
    const engineRoot = path.dirname(engineDir);   // bot/ when exe is in bot/engine/
    const spawnArgs = [];
    if (this._affinityMask) {
      spawnArgs.push('--affinity-mask', String(this._affinityMask));
    }

    // Ensure the MinGW runtime DLLs are findable even when the bot runs as a
    // Windows service (which starts with a stripped PATH that omits mingw64/bin).
    const mingwBin = 'C:\\mingw64\\bin';
    const spawnEnv = { ...process.env };
    if (!spawnEnv.PATH?.split(';').includes(mingwBin)) {
      spawnEnv.PATH = mingwBin + ';' + (spawnEnv.PATH ?? '');
    }

    this._proc = spawn(this._path, spawnArgs, {
      stdio: ['pipe', 'pipe', 'pipe'],
      cwd: engineRoot,
      env: spawnEnv,
    });

    this._proc.stderr.on('data', (d) => {
      process.stderr.write(`[engine stderr] ${d}`);
    });

    this._rl = readline.createInterface({ input: this._proc.stdout, crlfDelay: Infinity });
    this._rl.on('line', (line) => this._onLine(line.trim()));

    // Capture the process reference so that if init() is called again (engine
    // restart after crash) the stale close/error handlers from the old process
    // don't clobber the new one's state.
    const proc = this._proc;

    proc.on('error', (err) => {
      console.error('[engine] process error:', err.message);
      if (this._proc !== proc) return;   // stale handler after restart
      this._ready = false;
      this._proc  = null;
      const e = new Error(`Engine process error: ${err.message}`);
      this._rejectAll(e);
      this._rejectSession(e);
    });

    proc.on('close', (code) => {
      if (code !== 0 && code !== null) {
        console.error(`[engine] process exited with code ${code}`);
      }
      if (this._proc !== proc) return;   // stale handler (quit() already cleaned up)
      this._ready = false;
      this._proc  = null;
      const e = new Error(`Engine process closed (exit ${code ?? '?'})`);
      this._rejectAll(e);
      this._rejectSession(e);
    });

    // UCI handshake
    this._send('uci');
    await this._waitFor('uciok');

    this._send(`setoption name Hash value ${this._hash}`);
    this._send(`setoption name Threads value ${this._threads}`);
    if (this._evalFile) {
      this._send(`setoption name EvalFile value ${this._evalFile}`);
    }
    if (!this._useNnue) {
      this._send('setoption name UseNNUE value false');
    }
    this._send('isready');
    await this._waitFor('readyok');

    this._ready = true;
    this._send('ucinewgame');
  }

  /** Send ucinewgame to clear the TT between match games (engine stays alive). */
  newGame() {
    if (this._ready && !this._session) this._send('ucinewgame');
  }

  /**
   * Send arbitrary setoption commands after UCI handshake.
   * @param {Array<[string, string]>} pairs  [[name, value], ...]
   */
  sendOptions(pairs) {
    for (const [name, value] of (pairs ?? [])) {
      this._send(`setoption name ${name} value ${value}`);
    }
  }

  async quit() {
    if (!this._proc) return;
    const proc = this._proc;
    this._ready = false;
    // Send 'quit' BEFORE nulling _proc so _send() can still write to stdin.
    // Don't let a broken-pipe error prevent the kill — that MUST always run.
    try { this._send('quit'); } catch (_) {}
    this._proc  = null;
    await new Promise(r => setTimeout(r, 200));
    try { proc.kill(); } catch (_) {}
  }

  // ── dynamic search (go infinite + confidence-based stop) ────────────

  /**
   * Search with `go infinite` and a JS-side confidence callback.
   *
   * The engine searches indefinitely.  On every `info depth` line the
   * `onInfo(infoHistory, elapsedMs)` callback is invoked.  When it returns
   * `{ stop: true }` (or the hard `maxTimeMs` ceiling is hit), we send
   * `stop` and wait for `bestmove`.
   *
   * @param {string}   fen      position FEN
   * @param {string[]} moves    full move list from game start (UCI)
   * @param {object}   opts
   * @param {function} opts.onInfo    (infoHistory, elapsedMs) → { stop: boolean }
   * @param {number}   opts.maxTimeMs hard time ceiling (ms)
   * @param {{wtime,btime,winc,binc}} [opts.clock]  send go wtime/btime/winc/binc (engine manages own time)
   * @returns {Promise<{move, ponderMove, depth, seldepth, nodes, nps, time_ms, eval_cp, mate, pv0}>}
   */
  async thinkDynamic(fen, moves, { onInfo, maxTimeMs, movetime = null, nodes = null, clock = null }) {
    if (!this._ready) throw new Error('Engine not initialised');
    if (this._session) throw new Error('Engine busy (active session)');

    this._sendPosition(fen, moves);
    if (movetime) {
      this._send(`go movetime ${movetime}`);
      console.log(`[engine] emergency search started (movetime=${movetime}ms, ceiling=${maxTimeMs ?? 'none'}ms)`);
    } else if (nodes) {
      this._send(`go nodes ${nodes}`);
      console.log(`[engine] node-limited search started (nodes=${nodes})`);
    } else if (clock) {
      this._send(`go wtime ${clock.wtime} btime ${clock.btime} winc ${clock.winc} binc ${clock.binc}`);
      console.log(`[engine] clock search (wtime=${clock.wtime} btime=${clock.btime} winc=${clock.winc} binc=${clock.binc})`);
    } else if (maxTimeMs) {
      // No clock available (correspondence / no time control) — use a depth+movetime cap
      // so the search doesn't run forever without a stop mechanism.
      this._send(`go movetime ${maxTimeMs}`);
      console.log(`[engine] fallback movetime search (maxTimeMs=${maxTimeMs})`);
    } else {
      // Absolute fallback: depth limit so we never hang.
      this._send('go depth 20');
      console.log('[engine] no-clock depth-limited search (depth 20)');
    }

    // For clock mode: derive a generous safety ceiling from the remaining time so
    // the session never hangs if the engine fails to emit bestmove on its own.
    const effectiveMax = maxTimeMs ?? (clock
      ? Math.min(Math.max(clock.wtime, clock.btime), 30_000) + (clock.winc ?? 0) + 5_000
      : null);

    const session = this._createSession('searching', { onInfo, maxTimeMs: effectiveMax, movetime, nodes, clock });
    return session.promise;
  }

  // ── pondering (think on opponent's time) ──────────────────────────────

  /**
   * Start pondering on the parent position (fire-and-forget).
   *
   * The engine receives the position AFTER our move (opponent to move) and
   * searches with `go infinite`.  It naturally explores all candidate replies
   * as first-ply branches, warming the TT for any move the opponent actually
   * plays.  When the opponent moves we cancel and restart — but the TT is
   * already seeded, so the re-search starts at a much higher effective depth.
   *
   * @param {string}   fen          initial FEN
   * @param {string[]} moves        game moves INCLUDING our move (opponent is to move)
   * @param {function} [onInfo]     optional (infoHistory, elapsedMs) callback for dashboard
   */
  startPonder(fen, moves, onInfo) {
    if (!this._ready || this._session) return;

    this._sendPosition(fen, moves);
    this._send('go infinite');

    const session = this._createSession('pondering', { onInfo: onInfo ?? null });
    // Silence unhandled rejection — ponder is fire-and-forget; errors (e.g.
    // bestmove (none) when the position is already terminal) are ignorable.
    session.promise.catch(() => {});
  }

  /**
   * Ponderhit ponder: search the position AFTER our move AND the predicted
   * opponent reply.  We are to move in that position.
   *
   * If the opponent actually plays the predicted move, call ponderhit(clock)
   * instead of cancelPonder() + thinkDynamic() — the search is already running
   * on the right position; C++ transitions to timed mode seamlessly.
   *
   * If the opponent plays something else, cancelPonder() + thinkDynamic() as
   * normal.  The TT will have deep coverage on the predicted line.
   *
   * @param {string}   fen          initial game FEN
   * @param {string[]} moves        all moves INCLUDING our move AND predicted reply
   * @param {string}   ponderMove   the predicted opponent reply (UCI)
   * @param {function} [onInfo]     dashboard callback
   */
  startPonderHit(fen, moves, ponderMove, clock, onInfo) {
    if (!this._ready || this._session) return;

    this._sendPosition(fen, moves);
    // Use go ponder so C++ properly handles the ponderhit transition.
    // The clock values are saved for reference but C++ only uses them on ponderhit.
    this._send(`go wtime ${clock.wtime} btime ${clock.btime} winc ${clock.winc ?? 0} binc ${clock.binc ?? 0} ponder`);

    const session = this._createSession('pondering', { onInfo: onInfo ?? null, clock });
    session.ponderMove = ponderMove;   // stored for hit detection in game.js
    session.promise.catch(() => {});
  }

  /**
   * Ponderhit: opponent played the predicted move — transition the running
   * ponder search into our timed search with no restart overhead.
   *
   * Sends `ponderhit wtime X btime Y winc Z binc Z` to C++.  The engine
   * resets its start-time and runs allocate_time() with the live clocks.
   * Returns the existing session promise which resolves on bestmove.
   *
   * @param {{wtime,btime,winc,binc}} clock  current clock state
   * @returns {Promise} resolves with the same shape as thinkDynamic()
   */
  ponderhit(clock) {
    const s = this._session;
    if (!s || s.state !== 'pondering') {
      return Promise.reject(new Error('No active ponder session for ponderhit'));
    }

    // Transition session state so subsequent info lines use search watchdog rules.
    s.state     = 'searching';
    s.clock     = clock;
    s.startTime = Date.now();

    // Replace ponder watchdog with a generous safety ceiling.
    // C++ manages its own time; this only fires if the engine hangs.
    if (s.watchdogId) { clearTimeout(s.watchdogId); s.watchdogId = null; }
    if (s.timeoutId)  { clearTimeout(s.timeoutId);  s.timeoutId  = null; }
    const safetyMs = Math.min(Math.max(clock.wtime, clock.btime), 30_000) + 5_000;
    s.maxTimeMs = safetyMs;
    s.timeoutId = setTimeout(() => {
      if (this._session === s && !s.stopping) {
        this._stopSession(s, 'ponderhit safety ceiling');
      }
    }, safetyMs);

    // Send ponderhit with current clocks so C++ can re-allocate time.
    this._send(`ponderhit wtime ${clock.wtime} btime ${clock.btime} winc ${clock.winc ?? 0} binc ${clock.binc ?? 0}`);

    console.log(`[engine] ponderhit sent (wtime=${clock.wtime} btime=${clock.btime} history=${s.infoHistory.length} depths)`);
    return s.promise;
  }

  /**
   * Convert the running ponderhit ponder directly into our real search.
   * No position change, no engine restart — just arm the confidence/budget
   * callback and reset the elapsed timer.
   *
   * Only valid when the opponent played exactly the predicted ponder move.
   *
   * @param {function} onInfo     confidence callback (infoHistory, elapsedMs) → { stop }
   * @param {number}   maxTimeMs  search budget from getSearchProfile()
   * @returns {Promise} resolves with the same result shape as thinkDynamic()
   */
  convertPonderToSearch(onInfo, maxTimeMs) {
    const s = this._session;
    if (!s || s.state !== 'pondering') {
      return Promise.reject(new Error('No active ponder session to convert'));
    }

    // Transition session state — subsequent info lines will now evaluate onInfo
    // for stop decisions instead of treating it as fire-and-forget.
    s.state     = 'searching';
    s.onInfo    = onInfo;
    s.maxTimeMs = maxTimeMs;
    s.startTime = Date.now();   // reset elapsed from this moment, not ponder start

    // Replace ponder watchdog with search timers.
    if (s.watchdogId) { clearTimeout(s.watchdogId); s.watchdogId = null; }
    if (s.timeoutId)  { clearTimeout(s.timeoutId);  s.timeoutId  = null; }

    // Precise hard stop at exactly maxTimeMs — no gap.
    if (maxTimeMs) {
      s.timeoutId = setTimeout(() => {
        if (this._session === s && !s.stopping) {
          this._stopSession(s, `hard timeout (${maxTimeMs}ms)`);
        }
      }, maxTimeMs);
    }

    // Safety watchdog: fires 3s AFTER the hard stop deadline.
    // Purpose: detect when the engine ignores the 'stop' command and never
    // sends bestmove.  Must not fire before maxTimeMs — the hard stop timer
    // already handles the normal case, and long depth iterations (e.g. depth 25
    // taking >8s after a ponderhit) must be allowed to run to completion.
    const watchdogMs = (maxTimeMs ?? 0) + 3_000;
    s.watchdogId = setTimeout(() => {
      if (this._session === s && !s.stopping) {
        console.error(`[engine] post-ponderhit watchdog: engine silent ${watchdogMs}ms after search start — forcing stop`);
        this._stopSession(s, 'post-ponderhit watchdog');
      }
    }, watchdogMs);

    console.log(`[engine] ponder converted to search (maxTimeMs=${maxTimeMs}, history=${s.infoHistory.length} depths already done)`);

    // Immediately evaluate pre-existing ponder history against the stop policy.
    // The onInfo callback is only triggered by future info lines, so without this
    // check the engine would ignore a forced mate (or high confidence) already
    // present in the ponder history and burn the full budget searching deeper.
    // elapsed=0 because startTime was just reset above.
    // skipTimeFloor=true: the engine has already been searching during ponder,
    // so the minTimeMs floor (which exists to guarantee a basic search) should
    // not block an early stop on well-established confidence.
    if (s.infoHistory.length > 0 && s.onInfo && !s.stopping) {
      const decision = s.onInfo(s.infoHistory, 0, { skipTimeFloor: true });
      if (decision?.stop) {
        this._stopSession(s, `policy decision on pre-existing history (${decision.reason})`);
      }
    }

    // The engine is already running — returning the existing promise which
    // resolves on bestmove exactly as thinkDynamic() would.
    return s.promise;
  }

  /**
   * Cancel a running ponder (pre-empted by the opponent's move).
   * Sends `stop`, waits for bestmove with a 4-second hard deadline.
   * If the engine doesn't respond in time the process is killed so
   * game.js crash-recovery can reinit before the real search.
   */
  async cancelPonder(deadlineMs = 1_500) {
    const s = this._session;
    if (!s || s.state !== 'pondering') return;

    // Use _stopSession so the 5s bestmove deadline is armed
    this._stopSession(s, 'ponder cancel');

    // Retry stop every 100ms: engines can miss a `stop` that arrives in the same
    // tick as `go ponder` (race between command pipe and search thread startup).
    // Resending is safe — a UCI engine must handle multiple stops gracefully.
    const RETRY_MS = 100;
    const retryInterval = setInterval(() => {
      if (this._session === s && s.state === 'pondering') {
        this._send('stop');
      } else {
        clearInterval(retryInterval);
      }
    }, RETRY_MS);

    // Also race with a hard deadline here so that cancelPonder() never
    // suspends the game loop excessively.  Callers can pass a tighter
    // deadline for emergency (low-clock) situations.
    const deadline = new Promise((_, rej) =>
      setTimeout(() => rej(new Error('cancelPonder timeout')), deadlineMs)
    );
    try {
      await Promise.race([s.promise, deadline]);
    } catch (err) {
      if (err.message === 'cancelPonder timeout') {
        console.error('[engine] cancelPonder: engine did not respond within 1.5s, killing');
        if (this._proc) {
          try { this._proc.kill(); } catch (_) {}
          this._proc  = null;
          this._ready = false;
        }
        this._clearSession(s);
      }
      // other errors (bestmove (none), process closed) are ignorable
    } finally {
      clearInterval(retryInterval);
    }
  }

  /** @returns {boolean} */
  isPondering() {
    return this._session?.state === 'pondering';
  }

  /** Returns the predicted opponent move stored in the active ponder session, or null. */
  getPonderMove() {
    return this._session?.ponderMove ?? null;
  }

  /** @returns {boolean} */
  isReady() {
    return this._ready;
  }

  // ── session management ────────────────────────────────────────────────

  /** Send `position fen … moves …` */
  _sendPosition(fen, moves) {
    const isStartpos = fen === 'startpos';
    const posStr = isStartpos ? 'position startpos' : `position fen ${fen}`;
    const cmd = moves.length > 0
      ? `${posStr} moves ${moves.join(' ')}`
      : posStr;
    this._send(cmd);
  }

  /**
   * Get the structured eval-vector breakdown for a position.
   * Sends `position` + `evalvec` and parses the JSON response.
   * Must be called when no session is active (between searches).
   * Returns null if the engine doesn't support evalvec or on any error.
   * @param {string} fen
   * @param {string[]} moves
   * @returns {Promise<object|null>}
   */
  async getEvalVec(fen, moves) {
    if (!this._ready || !this._proc) return null;
    if (this._session) return null;  // engine busy
    try {
      this._sendPosition(fen, moves);
      this._send('evalvec');
      const line = await this._waitFor('evalvec', 2000);
      // line = 'evalvec {"material_pst":[mg,eg],...}'
      const jsonStr = line.substring(line.indexOf('{'));
      return JSON.parse(jsonStr);
    } catch (err) {
      console.warn('[engine] getEvalVec failed (ignored):', err.message ?? err);
      return null;
    }
  }

  /**
   * Create a unified search/ponder session and hook into the line handler.
   * @returns {{ promise: Promise, … }}
   */
  _createSession(state, { onInfo, maxTimeMs, movetime, nodes, clock, ponderMove } = {}) {
    let resolve, reject;
    const promise = new Promise((res, rej) => { resolve = res; reject = rej; });

    const session = {
      state,            // 'searching' | 'pondering'
      infoHistory: [],
      onInfo:    onInfo    ?? null,
      maxTimeMs: maxTimeMs ?? null,
      movetime:  movetime  ?? null,   // non-null = engine manages time (go movetime)
      nodes:     nodes     ?? null,   // non-null = engine manages stop (go nodes N)
      clock:     clock     ?? null,   // non-null = engine manages time via wtime/btime
      startTime: Date.now(),
      timeoutId: null,
      bestmoveDeadlineId: null,
      watchdogId: null,
      ponderMove: ponderMove ?? null,
      stopping:  false,
      handler:   null,
      promise,
      resolve,
      reject,
    };

    // Hard timeout for searching sessions
    if (state === 'searching') {
      if (session.nodes) {
        // Engine manages its own stop via node count.  Safety ceiling (10 s) only.
        session.timeoutId = setTimeout(() => {
          if (this._session === session && !session.stopping) {
            this._stopSession(session, 'nodes safety ceiling (10s)');
          }
        }, 10_000);
      } else if (session.movetime) {
        // Engine manages its own time via "go movetime".  Safety ceiling only —
        // engine should have emitted bestmove well before this fires.
        session.timeoutId = setTimeout(() => {
          if (this._session === session && !session.stopping) {
            this._stopSession(session, `movetime safety (${session.movetime}ms)`);
          }
        }, session.movetime + 2000);
      } else if (session.clock) {
        // Engine manages its own time via go wtime/btime.  Safety ceiling only.
        session.timeoutId = setTimeout(() => {
          if (this._session === session && !session.stopping) {
            this._stopSession(session, 'clock safety ceiling');
          }
        }, (maxTimeMs ?? 30_000) + 2_000);
      } else if (maxTimeMs) {
        // Precise hard stop at exactly maxTimeMs — no +500ms gap.
        // The confidence system (onInfo) can stop earlier; this is the backstop
        // that guarantees the search never exceeds maxTimeMs even between depths.
        session.timeoutId = setTimeout(() => {
          if (this._session === session && !session.stopping) {
            this._stopSession(session, `hard timeout (${maxTimeMs}ms)`);
          }
        }, maxTimeMs);
      }
    }

    // Watchdog: if NO info lines arrive in the first 10s, the engine is likely hung
    if (state === 'searching') {
      session.watchdogId = setTimeout(() => {
        if (this._session === session && session.infoHistory.length === 0) {
          console.error('[engine] watchdog: no info lines in first 10s — engine appears hung, forcing stop');
          this._stopSession(session, 'no-output watchdog');
        }
      }, 10_000);
    }

    // Ponder watchdog: cap ponder at 60s so a silent hang never blocks cancelPonder
    if (state === 'pondering') {
      session.watchdogId = setTimeout(() => {
        if (this._session === session) {
          console.warn('[engine] ponder watchdog: sending stop after 60s');
          this._stopSession(session, 'ponder 60s cap');
        }
      }, 60_000);
    }

    session.handler = (line) => {
      // 'info string ...' diagnostic lines (aspiration, rootmoves, searchdiag etc.) can
      // contain the words 'depth' and 'score' as part of their free-form payload.
      // They must be excluded before the scored-info check, otherwise parseInfo
      // misreads them (eval_cp=null, pv=[]) and fires a search_info event that snaps
      // the eval bar to 0 and clears the PV on the dashboard.
      if (line.startsWith('info') && !line.startsWith('info string') && line.includes('depth') && line.includes('score')) {
        const info = parseInfo(line);
        session.infoHistory.push(info);

        // Rolling watchdog (searching only): reset a timer each time an info line
        // arrives.  If the engine goes silent mid-search (e.g. hangs at some depth
        // after outputting depth 1–3) this fires and forces a stop rather than waiting
        // the full maxTimeMs ceiling.  The initial watchdog (checks for zero info at 10s)
        // is superseded once the first info line arrives.
        // Duration is proportional to the budget so bullet games don't wait 5s.
        if (session.state === 'searching' && !session.stopping) {
          if (session.watchdogId) { clearTimeout(session.watchdogId); session.watchdogId = null; }
          // Rolling watchdog: if the engine goes silent between depth iterations
          // (no new info line for this long), force a stop.
          // Set to max(budget, 10s) so a single slow depth never triggers it
          // prematurely — depth N can legitimately take much longer than depth N-1.
          // The hard timeout at exactly maxTimeMs handles on-time stopping;
          // this only catches a truly hung engine.
          const watchdogMs = session.nodes
            ? 10_000                                                        // nodes: engine stops itself, 10s grace
            : session.movetime
            ? session.movetime + 2_000                                      // movetime: engine stops itself, 2s grace
            : session.clock
            ? (session.maxTimeMs ?? 30_000) + 2_000                        // clock: engine stops itself, generous grace
            : Math.max(session.maxTimeMs ?? 10_000, 10_000);               // normal: at least 10s or the full budget
          session.watchdogId = setTimeout(() => {
            if (this._session === session && !session.stopping) {
              console.error(`[engine] rolling watchdog: no new info line in ${watchdogMs}ms — forcing stop`);
              this._stopSession(session, 'rolling no-info watchdog');
            }
          }, watchdogMs);
        }

        // Invoke onInfo for both searching and pondering sessions (if provided).
        // For searching: can return { stop: true } to cut the search short.
        // For pondering: return value is ignored (can't stop ponder this way).
        if (session.onInfo && !session.stopping) {
          const elapsed = Date.now() - session.startTime;
          if (session.state === 'searching') {
            const decision = session.onInfo(session.infoHistory, elapsed);
            // Allow the confidence system to stop the search early even in
            // movetime mode.  `go movetime N` is now a ceiling (engine self-stops
            // at N ms if JS hasn't fired first), not a floor that must be burned.
            // This is critical in emergency — spending 30ms when confident at
            // 12ms wastes 18ms of an already-empty clock.
            if (decision?.stop) {
              this._stopSession(session, 'policy decision');
            }
          } else if (session.state === 'pondering') {
            session.onInfo(session.infoHistory, elapsed);
          }
        }
      } else if (line.startsWith('info string') && session.stopping && session.bestmoveDeadlineId &&
                 line.toLowerCase().includes('not searching')) {
        // Engine explicitly reported it was not searching when stop was sent
        // (e.g. Stormphrax: "info string not searching").  It will never emit
        // bestmove, so collapse the 5s deadline to 300ms for fast recovery.
        clearTimeout(session.bestmoveDeadlineId);
        session.bestmoveDeadlineId = setTimeout(() => {
          if (this._session !== session) return;
          console.error('[engine] fast deadline: engine sent "not searching" — collapsing bestmove wait to 300ms');
          this._clearSession(session);
          session.reject(new Error('Engine hang: bestmove not received within 5s of stop'));
          if (this._proc) {
            try { this._proc.kill(); } catch (_) {}
            this._proc  = null;
            this._ready = false;
          }
        }, 300);
      } else if (line.startsWith('bestmove')) {
        const elapsed = Date.now() - session.startTime;
        console.log(`[engine] bestmove received after ${elapsed}ms (depth=${session.infoHistory.at(-1)?.depth ?? '?'}, info lines=${session.infoHistory.length})`);
        this._clearSession(session);
        const parts = line.split(' ');
        const move  = parts[1] ?? '';
        const pmove = (parts[2] === 'ponder' && parts[3]) ? parts[3] : null;

        if (!move || move === '(none)') {
          return session.reject(new Error('Engine returned no move'));
        }

        const lastInfo = session.infoHistory.length > 0
          ? session.infoHistory[session.infoHistory.length - 1]
          : fallbackStats(session.maxTimeMs ?? 0);

        // Build compact depth history for diagnostics / sidecar traces
        const depthHistory = session.infoHistory.map(h => ({
          d: h.depth, cp: h.eval_cp, mate: h.mate ?? null, nodes: h.nodes,
        }));

        session.resolve({ move, ponderMove: pmove, depthHistory, ...lastInfo });
      }
    };

    this._lineHandlers.push(session.handler);
    this._session = session;
    return session;
  }

  /**
   * Send stop to the engine and start a 5-second bestmove deadline.
   * If bestmove doesn't arrive in time, the session is force-rejected and
   * the engine process is killed so game.js crash recovery can reinit.
   */
  _stopSession(session, reason) {
    if (session.stopping) return;   // already stopping
    session.stopping = true;
    console.warn(`[engine] sending stop (reason: ${reason}, elapsed=${Date.now() - session.startTime}ms, info=${session.infoHistory.length})`);
    this._send('stop');

    // Bestmove deadline — if engine doesn't respond in 5s it's truly wedged
    session.bestmoveDeadlineId = setTimeout(() => {
      if (this._session !== session) return;  // already resolved
      console.error('[engine] BESTMOVE DEADLINE: engine did not respond to stop within 5s — killing process');
      this._clearSession(session);
      session.reject(new Error('Engine hang: bestmove not received within 5s of stop'));
      // Kill so game.js crash-recovery path can reinit cleanly
      if (this._proc) {
        try { this._proc.kill(); } catch (_) {}
        this._proc  = null;
        this._ready = false;
      }
    }, 5_000);
  }

  /** Tear down a session (timeout, watchdog, bestmove deadline, handler). */
  _clearSession(session) {
    if (session.timeoutId)          clearTimeout(session.timeoutId);
    if (session.watchdogId)         clearTimeout(session.watchdogId);
    if (session.bestmoveDeadlineId) clearTimeout(session.bestmoveDeadlineId);
    const i = this._lineHandlers.indexOf(session.handler);
    if (i !== -1) this._lineHandlers.splice(i, 1);
    if (this._session === session) this._session = null;
  }

  // ── internals ─────────────────────────────────────────────────────────

  _send(cmd) {
    if (!this._proc) return;   // engine not running (quit or not yet init'd)
    try {
      this._proc.stdin.write(cmd + '\n');
    } catch (e) {
      console.error('[engine] write error:', e.message);
    }
  }

  _onLine(line) {
    // Dispatch to any registered handlers — wrap in try/catch so a misbehaving
    // onInfo callback (e.g. a store write that throws) cannot escape into the
    // readline event loop and become an uncaught exception.
    for (const h of [...this._lineHandlers]) {
      try {
        h(line);
      } catch (err) {
        console.error('[engine] line handler threw (ignored):', err.message ?? err);
      }
    }
    // Wake up _waitFor promises
    if (this._queue.length > 0) {
      const next = this._queue[0];
      if (line.startsWith(next.token)) {
        this._queue.shift();
        next.resolve(line);
      }
    }
  }

  _waitFor(token, timeoutMs = 10000) {
    return new Promise((resolve, reject) => {
      const tid = setTimeout(() => {
        const i = this._queue.findIndex(q => q.token === token);
        if (i !== -1) this._queue.splice(i, 1);
        reject(new Error(`Timed out waiting for '${token}' (engine path: ${this._path})`));
      }, timeoutMs);

      this._queue.push({
        token,
        resolve: (line) => { clearTimeout(tid); resolve(line); },
        reject:  (err)  => { clearTimeout(tid); reject(err); },
      });
    });
  }

  /** Reject all pending _waitFor promises (e.g. on spawn error or early exit). */
  _rejectAll(err) {
    const waiting = this._queue.splice(0);
    for (const w of waiting) w.reject(err);
  }

  /** Reject the active search/ponder session (e.g. on unexpected process exit). */
  _rejectSession(err) {
    const s = this._session;
    if (!s) return;
    this._clearSession(s);
    s.reject(err);
  }
}

// ── helpers ───────────────────────────────────────────────────────────────

/**
 * Parse a UCI `info depth ...` line into a stat object.
 */
function parseInfo(line) {
  const tok = line.split(' ');
  const get = (key) => {
    const i = tok.indexOf(key);
    return i !== -1 ? tok[i + 1] : null;
  };

  const scoreKind  = get('score') ? tok[tok.indexOf('score') + 1] : null;
  const scoreVal   = scoreKind   ? parseInt(tok[tok.indexOf('score') + 2], 10) : null;

  // Extract full principal variation — stop at the first token that isn't a
  // valid UCI move (e.g. a concatenated "info string ..." line due to buffering).
  const UCI_MOVE = /^[a-h][1-8][a-h][1-8][qrbnQRBN]?$/;
  const pvIdx = tok.indexOf('pv');
  const pvRaw = pvIdx !== -1 ? tok.slice(pvIdx + 1) : [];
  const firstBad = pvRaw.findIndex(t => !UCI_MOVE.test(t));
  const pv  = firstBad === -1 ? pvRaw : pvRaw.slice(0, firstBad);
  const pv0 = pv.length > 0 ? pv[0] : null;

  return {
    depth:    parseInt(get('depth')    ?? '0', 10),
    seldepth: parseInt(get('seldepth') ?? '0', 10),
    nodes:    parseInt(get('nodes')    ?? '0', 10),
    nps:      parseInt(get('nps')      ?? '0', 10),
    time_ms:  parseInt(get('time')     ?? '0', 10),
    eval_cp:  scoreKind === 'cp'   ? scoreVal : null,
    mate:     scoreKind === 'mate' ? scoreVal : null,
    pv,
    pv0,
  };
}

function fallbackStats(movetime) {
  return { depth: 0, seldepth: 0, nodes: 0, nps: 0, time_ms: movetime, eval_cp: null, mate: null, pv0: null };
}

module.exports = { Engine };
