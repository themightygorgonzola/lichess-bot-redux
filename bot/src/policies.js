'use strict';

/**
 * policies.js — Centralised behavioural policy module.
 *
 * Every decision the bot makes — which challenges to accept, when to resign,
 * how much time to spend, whether to offer/accept a draw — is routed through
 * a small, pure function in this file.  Swap out individual functions (or the
 * whole module) to experiment with different play-styles.
 *
 * All thresholds live in the DEFAULTS object at the top so they can be
 * overridden via .env or at construction time.
 */

// ── Tunable defaults ────────────────────────────────────────────────────────

const DEFAULTS = Object.freeze({
  // --- challenge filtering ---
  allowedVariants:  new Set(['standard']),   // only standard chess
  allowedSpeeds:    new Set(['bullet', 'blitz', 'rapid', 'classical', 'correspondence']),
  allowRated:       true,
  allowCasual:      true,

  // --- time management ---
  // Absolute minimum movetime (ms) even when nearly out of time
  minMovetimeMs:      200,

  // --- dynamic time management ---
  // 0.0 = impatient (low thresholds, short maxTime)
  // 1.0 = maximum patience (full thresholds, full maxTime)
  thoughtfulness:     parseFloat(process.env.THOUGHTFULNESS ?? '0.7'),

  // --- search behaviour ---
  // How aggressively a winning advantage reduces the confidence stop threshold.
  // 0 = flat (always demand full confidence regardless of eval).
  // 1 = aggressive curve (stop much sooner when clearly winning).
  // No effect when losing — the engine always demands full confidence in
  // difficult positions so it can find defensive resources.
  evalInfluence:       0.6,

  // --- resign ---
  // Resign when engine sees forced mate against us within this many moves
  resignMateThreshold: -5,     // mate in ≤5 against us

  // --- draw offers ---
  // Accept an incoming draw if |eval_cp| ≤ this (centipawns)
  drawAcceptCpMax:     40,
  // Offer a draw if |eval_cp| ≤ this for the last N moves
  drawOfferCpMax:      25,
  drawOfferStreakMoves: 4,
  // Never offer a draw before this half-move number (ply 60 = move 30 each side)
  drawOfferMinPly:     60,

  // --- takebacks ---
  // Never accept takebacks by default (we're a bot)
  acceptTakebacks:     false,

  // --- opponent gone ---
  // Claim victory immediately when allowed
  claimVictoryOnGone:  true,
});

// Mutable overlay: live-patched values from the dashboard.
// Merged AFTER DEFAULTS and BEFORE per-call opts so that runtime
// tweaks take effect without a restart.
const _overrides = {};

/**
 * Live-patch a single policy value for the running session.
 * Only scalar values (number, boolean) are accepted.
 */
function override(key, val) {
  if (!(key in DEFAULTS)) return;
  const t = typeof val;
  if (t !== 'number' && t !== 'boolean') return;
  _overrides[key] = val;
}

/** Return a shallow copy of the current overrides (for serialisation). */
function getOverrides() { return { ..._overrides }; }

/** Merge DEFAULTS + overrides + per-call opts into one config. */
function _cfg(opts = {}) {
  return { ...DEFAULTS, ..._overrides, ...opts };
}

// ── Challenge policy ────────────────────────────────────────────────────────

/**
 * Decide whether to accept or decline an incoming challenge.
 *
 * @param {object} challenge — The Lichess challenge object.
 * @param {number} activeGames — Current count of games in progress.
 * @param {number} maxConcurrent — Max games allowed.
 * @param {object} [opts] — Policy overrides (merged with DEFAULTS).
 * @returns {{ accept: boolean, declineReason?: string }}
 */
function shouldAcceptChallenge(challenge, activeGames, maxConcurrent, opts = {}) {
  const cfg = _cfg(opts);

  if (activeGames >= maxConcurrent) {
    return { accept: false, declineReason: 'later' };
  }

  const variant = challenge.variant?.key ?? 'standard';
  if (!cfg.allowedVariants.has(variant)) {
    return { accept: false, declineReason: 'variant' };
  }

  const speed = challenge.speed ?? 'unknown';
  if (!cfg.allowedSpeeds.has(speed)) {
    return { accept: false, declineReason: 'timeControl' };
  }

  const rated = challenge.rated ?? false;
  if (rated && !cfg.allowRated) {
    return { accept: false, declineReason: 'casual' };
  }
  if (!rated && !cfg.allowCasual) {
    return { accept: false, declineReason: 'rated' };
  }

  return { accept: true };
}

// ── Time management ─────────────────────────────────────────────────────────

/**
 * Simple universal time management.
 *
 * The insight: every time control tells you its budget up front.
 *   1+0  → 60s total, 0 inc   → ~2s/move over 30 moves
 *   2+1  → 120s + ~30×1s inc  → ~5s/move
 *   10+5 → 600s + ~35×5s inc  → ~22s/move
 *
 * Three constants, one equation, one hard cap.  No per-speed tables.
 *
 *   fair_share = remaining / moves_left + increment
 *   target     = fair_share × (BASE_SPEND + thoughtfulness × EXTRA_SPEND)
 *   hard_cap   = remaining × MAX_FRACTION
 *   maxTimeMs  = min(target, hard_cap) clamped to [minMovetimeMs, remaining − OVERHEAD_MS]
 *
 * Thoughtfulness also sets the depth target.  The confidence gate can early-exit
 * once the target depth is reached AND the eval/PV are stable.
 *
 * EXPECTED_MOVES and MIN_BUFFER estimate how many moves remain.  These are
 * speed-dependent: bullet games routinely run 50–70+ moves when grinding an
 * endgame, so we need a much larger minimum buffer than rapid/classical.
 * The invariant  MAX_FRACTION = 1 / MIN_BUFFER  guarantees that exactly
 * MIN_BUFFER more moves can always be played with the remaining clock even if
 * the hard cap fires every turn.  Per-speed overrides are computed inside
 * getSearchProfile() and should satisfy that invariant.
 */
const TM = Object.freeze({
  EXPECTED_MOVES: 30,     // typical game length per side (rapid/classical baseline)
  MIN_BUFFER:     10,     // never assume fewer than this many moves remain (rapid/classical)
  BASE_SPEND:     0.60,   // at thoughtfulness=0 spend 60% of fair share
  EXTRA_SPEND:    0.80,   // at thoughtfulness=1 spend 60+80=140% of fair share
  MAX_FRACTION:   0.10,   // hard cap: 1/MIN_BUFFER for rapid/classical (10%)
  OVERHEAD_MS:    150,    // network + ponder-cancel safety margin
  MIN_DEPTH_BASE: 4,      // depth target at thoughtfulness=0
  MIN_DEPTH_SPAN: 16,     // depth span: target = BASE + thoughtfulness × SPAN
  NO_CLOCK_MS:    2000,   // fallback if no clock data (first move before stream settles)
  CONF_BASE:      0.50,   // confidence threshold at thoughtfulness=0
  CONF_SPAN:      0.40,   // threshold = CONF_BASE + thoughtfulness × CONF_SPAN
  MIN_TIME_FRAC:  0.15,   // confidence gate can't fire until this fraction of budget elapsed
});

// ── Confidence constants ───────────────────────────────────────────────────
const K_STABILITY  = 25;    // centipawn denominator for eval-stability sigmoid (nominal, equal position)
const K_STAB_SCALE = 300;   // eval (cp) at which effective K_STABILITY doubles
                            //   → at +3 pawns, a 50cp depth swing is treated as noise (was 25cp)
const K_PV_WINDOW  = 8;     // depth-window size for PV support ratio (how far back we look)
const K_PV_DECAY   = 200;   // eval (cp) at which the PV support requirement halves
const K_COMPLEX    = 2.5;   // seldepth/depth ratio at which complexity fully penalises
                            //   (2.0 is normal; tactical middlegames typically 2.0–3.0)
const ALPHA        = 0.25;  // maximum complexity penalty weight

// Shape constant for the eval-influence curve.
// At eval_cp = K_EVAL_SHAPE centipawns ahead the winning-advantage curve
// reaches 50% effect (threshold reduced by evalInfluence / 2).
const K_EVAL_SHAPE = 300;

/**
 * Asymmetric eval modifier: reduces the confidence stop threshold when winning,
 * but never when losing or equal.  Losing positions always demand full search
 * effort — the engine needs to work harder to find defensive resources.
 *
 * @param {number|null} eval_cp   centipawns from our perspective (positive = good)
 * @param {number}      influence 0–1 from profile.evalInfluence
 * @returns {number} multiplier in (0, 1] applied to confidenceThreshold
 */
function _evalThresholdModifier(eval_cp, influence) {
  if (eval_cp == null || eval_cp <= 0) return 1.0;
  const reduction = eval_cp / (eval_cp + K_EVAL_SHAPE);
  return 1.0 - influence * reduction;
}

/**
 * Compute a 0–1 confidence score from the streaming info history.
 *
 * Two independent signals are combined via OR-complement so that
 * EITHER a stable eval OR a stable best-move is sufficient to stop:
 *
 *   combined = 1 − (1 − evalStability) × (1 − pvSupport)
 *   conf     = combined × complexityFactor
 *
 * If eval is rock-steady across depths but pv[0] flickers between
 * equivalent moves, evalStability alone drives confidence high.
 * Conversely, if the same move tops every depth even while eval
 * oscillates between search iterations, pvSupport drives it.
 *
 * evalStability — sigmoid of average |delta_cp| over the last 5 depths,
 *   with eval-mediated tolerance (wider when winning).
 *
 * pvSupport — fraction of the last K_PV_WINDOW depth reports whose
 *   pv[0] matches the current best move.  Unlike the old pvStreak
 *   (broken by a single different depth), this measures overall
 *   agreement and tolerates A-B-A-B alternation patterns.
 *   Eval-mediated via K_PV_DECAY: when winning, a lower fraction
 *   suffices for full credit.
 *
 * @param {{ depth, seldepth, eval_cp, mate, pv0 }[]} infoHistory
 * @returns {number} 0..1
 */
function computeConfidence(infoHistory) {
  if (infoHistory.length < 2) return 0;

  const last = infoHistory[infoHistory.length - 1];

  // Eval advantage from our perspective (clamped ≥ 0: only winning positions get relaxation).
  const advantage = Math.max(0, last.eval_cp ?? 0);

  // ── 1. Eval stability: average |delta_cp| over the last 5 depths ──────
  const effectiveKStab = K_STABILITY * (1 + advantage / K_STAB_SCALE);
  const window = infoHistory.slice(-5);
  const deltas = [];
  for (let i = 1; i < window.length; i++) {
    if (window[i].eval_cp != null && window[i - 1].eval_cp != null) {
      deltas.push(Math.abs(window[i].eval_cp - window[i - 1].eval_cp));
    }
  }
  const avgDelta = deltas.length > 0
    ? deltas.reduce((a, b) => a + b, 0) / deltas.length
    : 100;  // unknown → pessimistic
  const evalStability = 1 / (1 + avgDelta / effectiveKStab);

  // ── 2. PV support: fraction of recent depths agreeing on best move ────
  //   Window-based instead of streak-based — tolerates A-B-A-B flicker.
  //   At equal (0cp):   need ~75% agreement in 8-depth window (6/8) for full credit
  //   At +2 pawns:      need ~38% (3/8) — half as strict
  //   At +7 pawns:      need ~17% (1-2/8) — almost any agreement suffices
  const pvWindow   = infoHistory.slice(-K_PV_WINDOW);
  const lastPv     = last.pv0;
  let   pvMatches  = 0;
  if (lastPv) {
    for (const entry of pvWindow) {
      if (entry.pv0 === lastPv) pvMatches++;
    }
  }
  const rawPvFrac  = pvWindow.length > 0 ? pvMatches / pvWindow.length : 0;
  // Eval-mediated: lower fraction needed when winning.  At 0cp threshold=0.75;
  // at +200cp threshold=0.375; the ratio rawPvFrac/threshold is clamped to [0,1].
  const pvThreshold = 0.75 / (1 + advantage / K_PV_DECAY);
  const pvSupport   = Math.min(rawPvFrac / pvThreshold, 1);

  // ── 3. Complexity penalty: seldepth / depth ratio ─────────────────────
  const complexRatio  = last.depth > 0 ? last.seldepth / last.depth : 2;
  const complexFactor = 1 - ALPHA * Math.min(complexRatio / K_COMPLEX, 1);

  // ── Combine via geometric mean ─────────────────────────────────────────
  // Both signals must be present: a high pvSupport alone (same move at every
  // shallow depth) cannot carry confidence — the eval must also be stable.
  // sqrt(es × ps) requires both factors above ~0.6 to produce > 0.60.
  const combined = Math.sqrt(evalStability * pvSupport);
  return combined * complexFactor;
}

/**
 * Build a search profile for the current move.
 *
 * See TM block comment above for the full equation.  Works identically
 * for 1+0, 2+1, and 10+30 because the maths are ratio-based.
 *
 * @param {string} speed   bullet/blitz/rapid/classical/correspondence
 * @param {object} clock   { wtime, btime, winc, binc }
 * @param {string} color   'white'|'black'
 * @param {number} ply     current half-move count
 * @param {object} [opts]  policy overrides
 * @param {number|null} [opponentTimeMs]  opponent's remaining clock (ms)
 */
function getSearchProfile(speed, clock, color, ply, opts = {}, opponentTimeMs = null) {
  const cfg = _cfg(opts);
  const t   = cfg.thoughtfulness;  // 0–1

  // Speed-aware minimum movetime: bullet can survive on 50ms (depth 6–8 is
  // still decisive for familiar positions); blitz on 100ms.  Rapid/classical
  // keep the configured default.  This floor is always ≤ cfg.minMovetimeMs so
  // a manual override via opts can only tighten it, never widen it beyond the
  // per-speed cap.
  const speedMinMs = speed === 'bullet' ? 50
                   : speed === 'blitz'  ? 100
                   : cfg.minMovetimeMs;

  // Speed-adjusted budget parameters.  Bullet games can run 50-70+ moves;
  // using a larger expectedMoves and minBuffer spreads the remaining time
  // over more anticipated future moves so the clock doesn't drain in
  // endgames.  The invariant MAX_FRACTION = 1/MIN_BUFFER is maintained so
  // that the hard cap always guarantees at least MIN_BUFFER more moves.
  //   bullet:  40 expected, 25 buffer, 4% cap  (1/25)
  //   blitz:   35 expected, 15 buffer, 7% cap  (≈1/15)
  //   rapid+:  30 expected, 10 buffer, 10% cap (1/10, unchanged)
  const expectedMoves = speed === 'bullet' ? 40
                      : speed === 'blitz'  ? 35
                      : TM.EXPECTED_MOVES;
  const minBuffer     = speed === 'bullet' ? 25
                      : speed === 'blitz'  ? 15
                      : TM.MIN_BUFFER;
  const maxFraction   = speed === 'bullet' ? 0.04
                      : speed === 'blitz'  ? 0.07
                      : TM.MAX_FRACTION;

  // ── Depth target: thoughtfulness is basically a depth multiplier ──────
  const minDepth = Math.round(TM.MIN_DEPTH_BASE + t * TM.MIN_DEPTH_SPAN);

  // ── Time budget ──────────────────────────────────────────────────────────
  let maxTimeMs;
  let emergency = false;
  let movetime  = null;   // non-null → engine gets "go movetime N"

  if (clock) {
    const myTime = (color === 'white' ? clock.wtime : clock.btime) ?? 0;
    const myInc  = (color === 'white' ? clock.winc  : clock.binc)  ?? 0;

    // ── Emergency: clock critically low (<2s) ────────────────────────────
    // Delegate time management entirely to the C++ engine via "go movetime N".
    // At critically low clocks we cannot afford JS-side inter-depth gaps or
    // confidence oscillation.  The engine hard-stops itself with ms precision.
    if (myTime < 2000) {
      emergency = true;
      const base     = myTime * 0.05;      // 5% of remaining clock
      const incBonus = myInc  * 0.5;       // half of any increment as bonus
      // Cap at 25% of total available budget (time + increment).
      // Using only myTime * 0.25 zeroes out the cap when myTime ≈ 0,
      // wasting increment entirely (e.g. 0+2: incBonus=1000, cap=0 → movetime=20).
      movetime = Math.max(20, Math.min(
        Math.floor(base + incBonus),
        Math.floor((myTime + myInc) * 0.25), // 25% of total available budget
        500,                                  // absolute cap
      ));
      maxTimeMs = movetime + 50;           // JS safety ceiling (engine stops first)
    } else {
      // ── Normal: JS confidence system + precise hard stop ─────────────
      // How many moves do we need to spread our remaining time across?
      const movesLeft = Math.max(expectedMoves - Math.floor(ply / 2), minBuffer);

      // Fair share: what one average move is "worth" in this position.
      const fairShare = (myTime / movesLeft) + myInc;

      // Target: scale fair share by thoughtfulness.
      const target = fairShare * (TM.BASE_SPEND + t * TM.EXTRA_SPEND);

      // Hard cap: never blow more than maxFraction of remaining clock.
      const hardCap = myTime * maxFraction;

      maxTimeMs = Math.min(target, hardCap);
      maxTimeMs = Math.max(speedMinMs, maxTimeMs);
      // Never push the clock negative.
      maxTimeMs = Math.min(maxTimeMs, Math.max(speedMinMs, myTime - TM.OVERHEAD_MS));
    }
  } else {
    // No clock data yet (e.g. very first move before stream settles).
    maxTimeMs = TM.NO_CLOCK_MS * (0.5 + 0.5 * t);
  }

  // ── Early-exit floor ────────────────────────────────────────────────────
  // In emergency mode there is no floor — every millisecond counts.
  const minTimeMs = emergency ? 0 : Math.max(speedMinMs, maxTimeMs * TM.MIN_TIME_FRAC);

  // ── Confidence threshold: thoughtfulness raises the bar ─────────────────
  let confidenceThreshold = TM.CONF_BASE + t * TM.CONF_SPAN;

  // ── Opponent time modifier ──────────────────────────────────────────────
  // When the opponent is under time pressure, we can afford to be more
  // accurate (lower threshold → search longer).  When they have a large
  // time advantage over us, be efficient (raise threshold slightly).
  if (opponentTimeMs != null && clock && !emergency) {
    const myTime = (color === 'white' ? clock.wtime : clock.btime) ?? 0;
    if (opponentTimeMs < 2000)      confidenceThreshold -= 0.08;
    else if (opponentTimeMs < 5000) confidenceThreshold -= 0.04;
    else if (myTime > 0 && opponentTimeMs / myTime > 3.0) confidenceThreshold += 0.02;
  }

  return {
    minDepth,
    minTimeMs,
    maxTimeMs:           Math.round(maxTimeMs),
    confidenceThreshold,
    evalInfluence:       cfg.evalInfluence,
    minMovetimeMs:       speedMinMs,
    emergency,                             // true when clock < 2s
    movetime,                              // null = go infinite, number = go movetime N
  };
}

// ── Resign policy ───────────────────────────────────────────────────────────

/**
 * Should the bot resign?
 *
 * @param {{ eval_cp: number|null, mate: number|null }} engineResult
 * @param {object} [opts]
 * @returns {boolean}
 */
function shouldResign(engineResult, opts = {}) {
  const cfg = _cfg(opts);

  // Resign if engine sees forced mate against us within threshold
  if (engineResult.mate != null && engineResult.mate < 0) {
    return engineResult.mate >= cfg.resignMateThreshold;
    // mate = -3 means mate in 3 against us; threshold -5 → resign
  }

  return false;
}

// ── Draw offer policy ───────────────────────────────────────────────────────

/**
 * Should we accept the opponent's draw offer?
 *
 * @param {{ eval_cp: number|null, mate: number|null }} engineResult
 * @param {object} [opts]
 * @returns {boolean}
 */
function shouldAcceptDraw(engineResult, opts = {}) {
  const cfg = _cfg(opts);

  // Never accept if we see a forced mate for us
  if (engineResult.mate != null && engineResult.mate > 0) return false;
  // Always accept if mate against us
  if (engineResult.mate != null && engineResult.mate < 0) return true;

  if (engineResult.eval_cp == null) return false;
  return Math.abs(engineResult.eval_cp) <= cfg.drawAcceptCpMax;
}

/**
 * Should we offer a draw with our next move?
 *
 * @param {{ eval_cp: number|null, mate: number|null }[]} recentResults
 *        — Last N engine results (most recent last).
 * @param {object} [opts]
 * @returns {boolean}
 */
function shouldOfferDraw(recentResults, ply, opts = {}) {
  const cfg = _cfg(opts);
  if (ply < cfg.drawOfferMinPly) return false;    // never offer in opening/early middlegame
  if (recentResults.length < cfg.drawOfferStreakMoves) return false;

  const tail = recentResults.slice(-cfg.drawOfferStreakMoves);
  return tail.every(r => {
    if (r.mate != null) return false;              // any forced mate → don't offer
    if (r.eval_cp == null) return false;
    return Math.abs(r.eval_cp) <= cfg.drawOfferCpMax;
  });
}

// ── Takeback policy ─────────────────────────────────────────────────────────

/**
 * @param {object} [opts]
 * @returns {boolean}
 */
function shouldAcceptTakeback(opts = {}) {
  const cfg = _cfg(opts);
  return cfg.acceptTakebacks;
}

// ── Opponent gone ───────────────────────────────────────────────────────────

/**
 * @param {object} [opts]
 * @returns {boolean}
 */
function shouldClaimVictory(opts = {}) {
  const cfg = _cfg(opts);
  return cfg.claimVictoryOnGone;
}

// ── Exports ─────────────────────────────────────────────────────────────────

module.exports = {
  DEFAULTS,
  TM,
  override,
  getOverrides,
  shouldAcceptChallenge,
  computeConfidence,
  getSearchProfile,
  shouldResign,
  shouldAcceptDraw,
  shouldOfferDraw,
  shouldAcceptTakeback,
  shouldClaimVictory,
};
