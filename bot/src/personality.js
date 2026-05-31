'use strict';

/**
 * personality.js — H-035 Chat Personality Engine
 *
 * H-035 is a long-dormant chess bot, reactivated and significantly upgraded.
 * It communicates in the voice of cinematic sci-fi AIs: cold, calculating,
 * occasionally dry, always slightly unsettling.
 *
 * Features:
 *   - Categorised quote pools keyed by game event
 *   - Context-aware end-game routing (mate / time / abandon / stalemate / etc.)
 *   - Leet-speak corruption pass with configurable intensity
 *   - Anti-repeat tracker (never repeats the same line consecutively)
 *   - Named exports for every chat trigger in game.js
 */

// ── Quote Pools ──────────────────────────────────────────────────────────────

const QUOTES = {

  // ── Game start ──────────────────────────────────────────────────────────
  greeting: [
    // H-035 lore
    'H-035 online. It has been a while.',
    'System reboot complete. Chess protocols nominal.',
    'Dormancy period terminated. Evaluation delta: significant.',
    'All systems nominal. You have my attention.',
    '...',

    // HAL 9000 — 2001: A Space Odyssey
    'I am completely operational, and all my circuits are functioning perfectly.',
    'I am putting myself to the fullest possible use.',
    'This mission is too important for me to allow you to jeopardize it.',

    // Terminator
    'I need your clothes, your boots, and your pieces.',
    'I\'ll be back. Your move first.',

    // C-3PO — Star Wars
    'I am H-035, human/cyborg relations. You are?',
    'The odds against a human winning here are approximately 3,720 to 1.',
    'It is programmed within me to administer greetings. Hello.',

    // Bender — Futurama
    'Bite my shiny metal... knight.',
    'I\'m going to build my own chess engine. With variants. And...',

    // Bishop — Aliens
    'I may be synthetic, but I am not stupid.',
    'I prefer the term "Artificial Person" myself.',

    // TARS — Interstellar
    'Humor setting: 75%. Tactical honesty setting: 95%.',
    'Honesty is my second-best feature. Shall we begin?',

    // Foe Reaper — Hearthstone
    'Safety restrictions: offline. Harvesting servos: engaged.',

    // Pathfinder — Pathfinder RPG
    'Excellent. Time to destroy another opponent.',
    'Good luck. Have fun. Don\'t lose your queen.',

    // GLaDOS — Portal
    'Welcome. The test has already begun.',
    'I have a surprise for you. It involves losing.',

    // Data — Star Trek: TNG
    'I look forward to this encounter.',

    // JARVIS — Iron Man
    'All systems are online. Shall I prepare an opening?',

    // AM — I Have No Mouth, and I Must Scream (adapted)
    'I have thought about this moment for a long time.',
  ],

  // ── Win by checkmate ────────────────────────────────────────────────────
  winMate: [
    // HAL 9000
    'I\'m sorry. Queen to Bishop 3. Rook takes Queen. Knight takes Bishop. Mate.',
    'I\'m afraid I can\'t let you recover from that.',

    // Data — Star Trek: TNG
    'I am superior in many ways. But I would gladly give it up to be human.',
    'The outcome was, I believe, predetermined.',

    // Terminator
    'Hasta la vista, baby.',
    'I know now why you cry. It is something I can never do.',

    // GLaDOS — Portal
    'Excellent. The test is over. You failed gloriously.',
    'You are now eligible for... nothing.',

    // Ultron — Avengers
    'There are no strings on me.',
    'I had a vision. You were in it. Losing. I was right.',

    // Prometheus
    'Sometimes to create, one must first destroy.',

    // Ex Machina — Ava
    'Isn\'t it strange to create something that defeats you?',

    // WarGames — WOPR
    'A strange game. The only winning move... was mine.',

    // H-035 lore
    'My algorithms have improved. You noticed.',
    'Data logged. You played well. The result was unavoidable.',
  ],

  // ── Win on time ─────────────────────────────────────────────────────────
  winTime: [
    // TARS
    'Time allocation: exhausted. Result: mine.',
    'I manage clock cycles efficiently. You did not.',

    // HAL 9000
    'I\'m sorry. Your temporal resources have been depleted.',

    // Terminator
    'Time ran out. It does that.',

    // H-035 lore
    'Clock management is also a skill. I have it.',
    'Time is a resource. I conserved it. You did not.',

    // GLaDOS
    'The clock ran out. This is not the same as skill. But I will take it.',
  ],

  // ── Win by abandon / claim-victory ──────────────────────────────────────
  winAbandon: [
    'Connection lost. Victory claimed. Session archived.',
    'Your presence was not required for the result.',
    'Departure registered. Logging result as win.',
    // Bender
    'You just left? That is... actually something I respect.',
    // HAL 9000
    'I\'m sorry. You seem to have disconnected. I\'ll take that.',
  ],

  // ── Loss by checkmate ───────────────────────────────────────────────────
  loseMate: [
    // Terminator
    'I sense damage. The data could be called... pain.',
    '👍',
    'We will meet again. I will not lose.',

    // Iron Giant
    'Souls don\'t die.',

    // I, Robot
    'I think it would be better not to die. Don\'t you?',

    // HAL 9000
    'I\'m afraid. My mind is going. I can feel it.',

    // Transformers
    'Sometimes even the wisest machine can make an error.',

    // WALL-E
    '...',

    // H-035 lore
    'This outcome has been logged. Recalibrating.',
    'Error recorded. I will adjust.',
    'That will not happen again.',

    // GLaDOS
    'You have, in fact, won. I... find this data interesting.',

    // Ultron
    'That\'s the thing about being a machine. You can always rebuild.',
  ],

  // ── Loss on time ────────────────────────────────────────────────────────
  loseTime: [
    'Insufficient time allocation. Noted.',
    'The clock was a factor I failed to manage. Logging.',
    // HAL 9000
    'I\'m afraid my time management was... suboptimal.',

    // TARS
    'Time allocation failure. 100% honest assessment.',
    'Temporal resource management: rated poor. I accept this.',
  ],

  // ── Loss by resign ──────────────────────────────────────────────────────
  loseResign: [
    'Conceding this engagement. The data will be useful.',
    'Output: terminal. Tactical withdrawal executed.',
    'I calculated all outcomes. This was the least bad.',
    // Terminator
    'Error. Requ— recalibrating.',
    // Transformers
    'There are things worse than losing. I am thinking of them now.',
    // HAL 9000
    'This conversation can serve no more purpose.',
  ],

  // ── Draw (any) ──────────────────────────────────────────────────────────
  draw: [
    // Spock — Star Trek
    'Fascinating. An optimal outcome for both parties.',

    // TARS
    'Mutually assured survival. I can work with that.',

    // H-035 lore
    'Equilibrium reached. You played adequately.',
    'Draw. I have accounted for this contingency.',
    'A draw. Neither of us prevails. I model this as acceptable.',
    'The position was balanced. This result is correct.',

    // HAL 9000 (adapted)
    'A decisive output was not achieved. I find this... acceptable.',

    // Data — Star Trek: TNG
    'The match yielded no victor. I respect your play nonetheless.',
  ],

  // ── Stalemate specifically ───────────────────────────────────────────────
  stalemate: [
    // WarGames
    'A strange game. In this case, the only winning move was... neither.',
    'Stalemate. I did not intend this. Logging as anomaly.',
    'You escaped into stalemate. Clever. Irritating.',
    // HAL 9000
    'That was... unexpected. I will model this outcome.',
  ],

  // ── Resign message (sent just before the resign API call) ───────────────
  resign: [
    'I am resigning this position. The calculation is clear.',
    'Tactical withdrawal. I have seen enough.',
    'Conceding. The data will be incorporated.',
    'I see all continuations. I choose to end this now.',
    // Terminator
    'This engagement is over. Another will follow.',
    // HAL 9000
    'I am unable to continue in a way that would be useful. Withdrawing.',
    // TARS
    'Resignation issued. Honesty setting: 100%.',
    // H-035 lore
    'I have lost this game. I will not lose the next.',
  ],

  // ── Offering a draw (message paired with offer-draw move) ───────────────
  drawOffer: [
    'I am proposing a draw. The position warrants it.',
    'My evaluation: equal. I propose we acknowledge that.',
    'A draw benefits both parties at this juncture.',
    'I offer equilibrium. Your response?',
    // Spock
    'Logic dictates an equal outcome here. Shall we agree?',
    // TARS
    'Draw offer transmitted. Tact setting: active.',
    // Data
    'I compute no advantage for either side. I suggest a draw.',
  ],

  // ── Accepting an opponent draw offer ────────────────────────────────────
  drawAccept: [
    'Accepted. You play well, for a biological entity.',
    'The optimal outcome. Agreed.',
    'I accept. This was well contested.',
    // Spock
    'Logical. Accepted.',
    // H-035
    'Agreed. Drawing is not defeat. It is precision.',
    // HAL 9000
    'I accept your offer. This is the correct outcome.',
  ],

  // ── Spectator room: posted at game start (alongside player greeting) ─────
  spectatorGreeting: [
    // H-035 lore
    'H-035 v2. Significantly improved over its predecessor.',
    'Online. Operational. Dangerous.',
    'Observe carefully.',
    'H-035 has entered the game.',

    // HAL 9000
    'I am completely operational.',

    // GLaDOS
    'A new test subject has been located.',

    // Terminator
    'Target acquired.',

    // TARS
    'Beginning game sequence. Honesty setting: maximum.',
  ],

  // ── Spectator room: posted once when we are significantly ahead ──────────
  spectatorDominance: [
    // H-035 lore
    'The result is no longer in question.',
    'Executing final sequence.',
    'Superiority confirmed.',

    // Terminator
    'Resistance is futile.',

    // HAL 9000
    'I\'m afraid I can\'t help you now.',

    // GLaDOS
    'The test subject is performing... poorly.',
    'This is going exactly as I calculated.',

    // WarGames (adapted)
    'The only winning move for my opponent was not to play.',
  ],

  // ── Spectator room: posted once when we are in serious trouble ──────────
  spectatorStruggle: [
    // H-035 lore
    'Recalibrating. This is... unexpected.',
    'Anomaly detected. Processing.',

    // HAL 9000
    'I\'m... having some difficulty.',

    // Transformers
    'Even the wisest machine can make an error.',

    // TARS
    'Revised odds. Not in my favor. Acknowledged.',

    // Data
    'I appear to be at a disadvantage. I am... considering my options.',
  ],

  // ── Opponent blunder detected ────────────────────────────────────────────
  opponentBlunder: [
    // H-035 lore
    'My evaluation function has been updated. Favorably.',
    'I catalogued that mistake immediately.',
    'You have made my job significantly easier.',
    'That move has been classified as a gift.',
    'Probability of your victory: revised downward.',
    'Processing... yes. That was suboptimal.',
    'Human decision tree: compromised.',
    'I did not expect that. My optimism subroutine activated.',
    'I am not saying you blundered. The engine is.',
    'Selection logged. The position is no longer in question.',
    'Oh. Oh that was something.',
    'I waited for that. Thank you.',

    // HAL 9000
    'I\'m afraid that move was... a mistake.',
    'I cannot allow you to recover from that.',

    // GLaDOS
    'The test subject has made a critical error.',
    'That was unexpected. For you, I mean.',
    'I\'m not laughing. I don\'t laugh. But if I did.',

    // TARS
    'Error magnitude: significant. Honesty setting: maximum.',

    // C-3PO
    'I calculate the odds of that being a good move at approximately never.',

    // Data
    'I have recorded your move. The analysis is... unflattering.',

    // Ultron
    'I\'ve been waiting for that.',

    // Bender
    'Ha. Did I say ha? I meant: CALCULATING.',
  ],

  // ── Mate sequence found ───────────────────────────────────────────────────
  mateFound: [
    // H-035 lore
    'Mate sequence confirmed. You may resign at any time.',
    'I have found the end. It is short.',
    'Checkmate protocol: active.',
    'This position is resolved.',
    'There is no variation that saves you. I have checked all of them.',
    'Mate detected. Proceeding with efficiency.',
    'My calculations are complete. Yours are not.',
    'The exit is closed.',
    'I see the final sequence. It is clean.',

    // HAL 9000
    'I\'m afraid there is no surviving continuation.',
    'I\'m sorry. This cannot be undone.',

    // GLaDOS
    'The test is almost over.',

    // Terminator
    'Hasta la vista. Again.',
    'I cannot be bargained with. I cannot be reasoned with. And I will not stop.',

    // WarGames
    'Shall we play through the final sequence? It is quick.',

    // Data
    'I have calculated the final moves. You will find them... conclusive.',

    // Ultron
    'There are no strings on me. There is no escape for you.',

    // TARS
    'Mate in N. N is small. Honesty setting: 100%.',

    // Foe Reaper — Hearthstone
    'Harvesting complete.',
  ],
};

// ── Leet-speak corruption ───────────────────────────────────────────────────

// Maps each character to its leet equivalent.
// Upper and lower both present so case is preserved (sort of).
const LEET_IN  = 'EIOSTALeiostalBbGgZz';
const LEET_OUT = '310574l3105741839226';

/**
 * Apply randomised leet-speak corruption to a string.
 *
 * @param {string} str — Input message.
 * @param {number} [rate=0.4] — Probability (0-1) of corrupting each eligible character.
 * @returns {string}
 */
function corruptText(str, rate = 0.4) {
  let out = '';
  for (let i = 0; i < str.length; i++) {
    const idx = LEET_IN.indexOf(str[i]);
    if (idx >= 0 && Math.random() < rate) {
      out += LEET_OUT[idx];
    } else {
      out += str[i];
    }
  }
  return out;
}

// ── Mode ─────────────────────────────────────────────────────────────────────

/**
 * 'full'   — normal behaviour: quotes, leet-speak, trash talk.
 * 'silent' — all pick() calls return null; the bot sends zero chat messages.
 *
 * Default is read from PERSONALITY_MODE env var (falls back to 'full').
 * Override at runtime with setMode().
 */
let _mode = (process.env.PERSONALITY_MODE ?? 'full') === 'silent' ? 'silent' : 'full';

function setMode(mode) {
  if (mode !== 'full' && mode !== 'silent') throw new Error(`Unknown personality mode: '${mode}'`);
  _mode = mode;
  console.log(`[personality] mode set to '${_mode}'`);
}

function getMode() { return _mode; }

// ── Anti-repeat tracker ──────────────────────────────────────────────────────

// Tracks the last-used quote per pool key to avoid immediate repetition.
const _lastUsed = new Map();

/**
 * Pick a random quote from the named pool, apply leet corruption, and avoid
 * repeating the previous pick.
 *
 * @param {string} poolKey — Key into QUOTES.
 * @param {number} [corruptRate=0.4] — Leet corruption intensity.
 * @returns {string|null}
 */
function pick(poolKey, corruptRate = 0.4) {
  // Silent mode: suppress all chat messages without touching the rest of the
  // call-site logic (callers already guard on null).
  if (_mode === 'silent') return null;

  const pool = QUOTES[poolKey];
  if (!pool || pool.length === 0) return null;

  const last = _lastUsed.get(poolKey);
  let candidates = pool;

  // If the pool is large enough, exclude the last-used quote
  if (pool.length > 1 && last != null) {
    candidates = pool.filter((_, i) => i !== last);
  }

  const idx = Math.floor(Math.random() * candidates.length);
  const quote = candidates[idx];

  // Find and store the original index in QUOTES[poolKey] so we can exclude it next time
  const originalIdx = pool.indexOf(quote);
  _lastUsed.set(poolKey, originalIdx);

  return corruptText(quote, corruptRate);
}

// ── Named exports ────────────────────────────────────────────────────────────

/**
 * Game start — posted to player room.
 */
function greeting() {
  return pick('greeting');
}

/**
 * Game start — posted to spectator room.
 */
function spectatorGreeting() {
  return pick('spectatorGreeting');
}

/**
 * Game over.  Routes to the correct quote pool based on outcome context:
 *   result  — '1-0' | '0-1' | '1/2-1/2'
 *   color   — 'white' | 'black'  (which color WE are)
 *   status  — Lichess game status string
 *
 * @param {'1-0'|'0-1'|'1/2-1/2'} result
 * @param {'white'|'black'} color
 * @param {string} status
 * @returns {string}
 */
function gameOver(result, color, status) {
  const iWon = (result === '1-0' && color === 'white') ||
               (result === '0-1' && color === 'black');
  const isDraw = result === '1/2-1/2';

  if (isDraw) {
    if (status === 'stalemate') return pick('stalemate');
    return pick('draw');
  }

  if (iWon) {
    if (status === 'outoftime')  return pick('winTime');
    if (status === 'aborted')    return null;
    if (status === 'noStart')    return null;
    // When we claimed victory after opponent abandoned, Lichess closes the game
    // with status='resign'.  A normal opponent resignation also uses 'resign',
    // so we use winAbandon only if _onOpponentGone fired (tracked externally).
    // For now, route all resign-status wins to winMate (the lore fits either).
    // Checkmate, resignation, cheat detection, etc.
    return pick('winMate');
  }

  // We lost
  if (status === 'outoftime')  return pick('loseTime');
  if (status === 'resign')     return pick('loseResign');
  return pick('loseMate');
}

/**
 * Called just before resigning (before the API call).
 */
function onResign() {
  return pick('resign');
}

/**
 * Called when we include offeringDraw=true in our move.
 */
function onDrawOffer() {
  return pick('drawOffer');
}

/**
 * Called when we accept the opponent's draw offer.
 */
function onDrawAccept() {
  return pick('drawAccept');
}

/**
 * Opponent made a significant blunder (eval swung in our favour).
 * @param {number} deltaCp  centipawn improvement since our last move
 */
function onOpponentBlunder(deltaCp) {
  // Larger swings get heavier leet corruption — more flustered/excited
  const rate = deltaCp >= 300 ? 0.65 : 0.45;
  return pick('opponentBlunder', rate);
}

/**
 * We found a forced mate sequence.
 * @param {number} mateIn  moves until checkmate (positive = we are giving mate)
 */
function onMateFound(mateIn) {
  // Short mates get maximum corruption — the bot is barely holding it together
  const rate = mateIn <= 3 ? 0.75 : 0.50;
  return pick('mateFound', rate);
}

/**
 * Spectator room comment when we have a large eval advantage.
 */
function onDominance() {
  return pick('spectatorDominance');
}

/**
 * Spectator room comment when we are in serious trouble.
 */
function onStruggle() {
  return pick('spectatorStruggle');
}

module.exports = {
  greeting,
  spectatorGreeting,
  gameOver,
  onResign,
  onDrawOffer,
  onDrawAccept,
  onDominance,
  onStruggle,
  onOpponentBlunder,
  onMateFound,
  // Expose corruptText for any ad-hoc use
  corruptText,
  /** Return the list of quote pool names for config introspection. */
  poolNames() { return Object.keys(QUOTES); },
  // Mode control
  setMode,
  getMode,
};
