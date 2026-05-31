// PAWNOCITY SCORE
// Measures how much the bot is relying on pawn structure as compensation for material.
//
// Pawn Structure Value (PSV) per side:
//   connected pawns    × 1.0
//   passed pawns       × 2.5   (unblocked, no enemy pawn stopping it)
//   rank 5+ passed     × 2.0   (bonus for advanced threats)
//   rank 6+ passed     × 2.0   (double bonus for nearly-promoting)
//   total pawn count   × 0.5
//   isolated pawns     × -1.0
//   doubled pawns      × -0.5
//
// Pawnocity = botPSV - oppPSV  (positive = bot has structurally better pawns)
//
// Pawnocity RATIO = Pawnocity / max(1, materialDeficit)
//   Captures: "how much structural compensation is the bot holding per point of material it's down?"
//   High ratio with high deficit = bot bet big on pawns and is losing badly
//
// Per-game summary:
//   peakPawnocity    = max pawnocity score reached during the game
//   midPawnocity     = score at game midpoint
//   finalPawnocity   = score at resign
//   peakRatio        = max pawnocityRatio (highest pawn-compensation bet)
//
// THIS IS THE METRIC TO TRACK AFTER ANY ENGINE CHANGES.

const { queryGames, getGame } = require('./bot/src/gameDb');
const { parseFen, applyMove, posToFen } = require('./bot/src/fen');

const PIECE_VAL = { p:1, n:3, b:3, r:5, q:9 };
function matScore(board, isW) {
  let s = 0;
  for (let r = 0; r < 8; r++) for (let f = 0; f < 8; f++) {
    const pc = board[r][f];
    if (!pc || pc === '.') continue;
    const lc = pc.toLowerCase();
    if (lc === 'k') continue;
    const mine = isW ? pc === pc.toUpperCase() : pc === pc.toLowerCase();
    if (mine) s += PIECE_VAL[lc] || 0;
  }
  return s;
}

// board[0]=rank1, white pawns advance toward rank8 (row index 7)
function pawnStructureValue(board, isW) {
  const myPawn = isW ? 'P' : 'p';
  const enPawn = isW ? 'p' : 'P';

  let psv = 0;
  const files = [];

  for (let r = 0; r < 8; r++) for (let f = 0; f < 8; f++) {
    if (board[r][f] !== myPawn) continue;
    const rank = isW ? r + 1 : 8 - r;  // rank1..rank8
    files.push(f);

    // Passed?
    const ahead = isW
      ? [...Array(7-r).keys()].map(i => r+1+i)
      : [...Array(r).keys()];
    let passed = true;
    for (const ar of ahead) {
      for (const af of [f-1, f, f+1]) {
        if (af >= 0 && af <= 7 && board[ar][af] === enPawn) { passed = false; break; }
      }
      if (!passed) break;
    }
    if (passed) {
      psv += 2.5;
      if (rank >= 5) psv += 2.0;
      if (rank >= 6) psv += 2.0;
    }

    // Isolated? (no friendly pawn on adjacent file anywhere)
    let isolated = true;
    outer: for (let rr = 0; rr < 8; rr++) {
      for (const af of [f-1, f+1]) {
        if (af >= 0 && af <= 7 && board[rr][af] === myPawn) { isolated = false; break outer; }
      }
    }
    if (isolated) psv -= 1.0;

    // Doubled? (another friendly pawn on same file)
    let doubled = false;
    for (let rr = 0; rr < 8; rr++) {
      if (rr !== r && board[rr][f] === myPawn) { doubled = true; break; }
    }
    if (doubled) psv -= 0.5;

    psv += 0.5; // base per pawn
  }

  // Connected: pawns on adjacent files (count connected pairs)
  const fileSet = new Set(files);
  let connected = 0;
  for (const f of fileSet) {
    if (fileSet.has(f+1)) connected += files.filter(x=>x===f).length + files.filter(x=>x===f+1).length;
  }
  // Deduplicate — just count pawns that have at least one neighbor
  const connectedPawns = files.filter(f => fileSet.has(f-1) || fileSet.has(f+1)).length;
  psv += connectedPawns * 1.0;

  return psv;
}

function pawnocity(board, botIsW) {
  const botPSV = pawnStructureValue(board, botIsW);
  const oppPSV = pawnStructureValue(board, !botIsW);
  const botMat = matScore(board, botIsW);
  const oppMat = matScore(board, !botIsW);
  const matDiff = botMat - oppMat; // positive = bot ahead

  const score = botPSV - oppPSV;
  const deficit = Math.max(0, -matDiff); // how much material bot is DOWN
  const ratio = score / Math.max(1, deficit);

  return { botPSV, oppPSV, score, matDiff, deficit, ratio };
}

// ---- run on recent losses ----
const APRIL1 = new Date('2026-04-01T00:00:00Z').getTime();
const games = queryGames({ limit: 1000 });
const recentGames = games.filter(g => g.ts >= APRIL1 && g.ply_count > 20);
const recentLosses = recentGames.filter(g => g.bot_result === 'loss');
const recentWins   = recentGames.filter(g => g.bot_result === 'win');
const recentDraws  = recentGames.filter(g => g.bot_result === 'draw');

function analyzeGame(g) {
  const raw = getGame(g.id);
  if (!raw || !raw.full_moves || !raw.full_moves.length) return null;
  const startFen = raw.initial_fen || 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
  const botIsW = g.our_color === 'white';
  let pos;
  try { pos = parseFen(startFen); } catch(_) { return null; }
  const fens = [startFen];
  for (const uci of raw.full_moves) {
    try { pos = applyMove(pos, uci); fens.push(posToFen(pos)); } catch(_) { break; }
  }
  if (fens.length < 15) return null;

  const midPly = Math.floor(fens.length * 0.5);
  const endPly = fens.length - 1;

  let peakScore = -99, peakPly = 0;
  let midScore = 0, midMatDiff = 0;
  let finalScore = 0, finalMatDiff = 0;

  // Core metric: max pawnocity score reached WHILE bot is materially behind (deficit >= 2)
  // = "how large a structural bet did the bot make while down on material?"
  let peakBetScore = 0;   // pawnocity score at moment of biggest structural bet
  let peakBetDeficit = 0; // how far behind materially at that moment
  let peakBetPly = -1;

  // Also track: peak pawnocity score regardless of material state
  for (let i = 10; i < fens.length; i++) {
    let bd; try { bd = parseFen(fens[i]).board; } catch(_) { continue; }
    const p = pawnocity(bd, botIsW);

    if (p.score > peakScore) { peakScore = p.score; peakPly = i; }

    // "Structural bet" = pawnocity SCORE while deficit >= 2
    if (p.deficit >= 2 && p.score > peakBetScore) {
      peakBetScore = p.score;
      peakBetDeficit = p.deficit;
      peakBetPly = i;
    }

    if (i === midPly) { midScore = p.score; midMatDiff = p.matDiff; }
    if (i === endPly) { finalScore = p.score; finalMatDiff = p.matDiff; }
  }

  // Was the bot ever materially behind by 2+?
  const everBehind = peakBetPly >= 0;

  return {
    id: g.id, opp: g.opponent, color: g.our_color,
    result: g.bot_result, plies: fens.length,
    peakScore, peakPly,
    peakBetScore, peakBetDeficit, peakBetPly, everBehind,
    midScore, midMatDiff,
    finalScore, finalMatDiff,
  };
}

console.log('Computing pawnocity scores...\n');

const lossStats   = recentLosses.map(analyzeGame).filter(Boolean);
const winStats    = recentWins.map(analyzeGame).filter(Boolean);
const drawStats   = recentDraws.map(analyzeGame).filter(Boolean);

function avg(arr, fn) { return arr.length ? arr.reduce((s,x)=>s+fn(x),0)/arr.length : 0; }
function pct(n, d) { return d ? ((n/d)*100).toFixed(1)+'%' : 'n/a'; }

// Filter to games where bot was ever materially behind (the ones that matter)
const lossBehind = lossStats.filter(s => s.everBehind);
const winBehind  = winStats.filter(s => s.everBehind);

console.log('=== PAWNOCITY SUMMARY (since April 1) ===\n');
console.log(`Results: ${lossStats.length} losses, ${winStats.length} wins, ${drawStats.length} draws`);
console.log(`Games where bot went behind in material: losses ${lossBehind.length}/${lossStats.length}, wins ${winBehind.length}/${winStats.length}\n`);

console.log('THE METRIC: "Structural Bet Score" = peak pawn structure advantage held while 2+ pts behind in material');
console.log('Higher = bigger bet on pawns as compensation. This is the crack.\n');

console.log(`                            LOSSES(behind)   WINS(behind)`);
console.log(`Avg structural bet score    ${avg(lossBehind,s=>s.peakBetScore).toFixed(2).padStart(15)}  ${avg(winBehind,s=>s.peakBetScore).toFixed(2).padStart(12)}`);
console.log(`Avg deficit when bet peaks  ${avg(lossBehind,s=>s.peakBetDeficit).toFixed(2).padStart(15)}  ${avg(winBehind,s=>s.peakBetDeficit).toFixed(2).padStart(12)}`);
console.log(`Avg peak pawn score (any)   ${avg(lossBehind,s=>s.peakScore).toFixed(2).padStart(15)}  ${avg(winBehind,s=>s.peakScore).toFixed(2).padStart(12)}`);
console.log(`Avg final mat diff          ${avg(lossBehind,s=>s.finalMatDiff).toFixed(2).padStart(15)}  ${avg(winBehind,s=>s.finalMatDiff).toFixed(2).padStart(12)}`);

// Distribution of structural bet score in losses
console.log('\n=== Structural bet score distribution (LOSSES where bot went behind) ===');
const betBuckets = { '0 (never behind)': 0, '0.1-5': 0, '5-10': 0, '10-15': 0, '15-20': 0, '20+': 0 };
for (const s of lossStats) {
  const b = s.peakBetScore;
  if (!s.everBehind) betBuckets['0 (never behind)']++;
  else if (b < 5) betBuckets['0.1-5']++;
  else if (b < 10) betBuckets['5-10']++;
  else if (b < 15) betBuckets['10-15']++;
  else if (b < 20) betBuckets['15-20']++;
  else betBuckets['20+']++;
}
Object.entries(betBuckets).forEach(([k,v])=>console.log(`  ${k.padEnd(18)} ${v} games`));

// Per-game table
console.log('\n=== Per-game pawnocity (LOSSES, sorted by structural bet score desc) ===');
console.log('opp                      color  plies  betScore  betDeficit  betPly  finalMat');
lossStats.sort((a,b)=>b.peakBetScore-a.peakBetScore).forEach(s => {
  const bet = s.everBehind ? s.peakBetScore.toFixed(1) : '-';
  const def = s.everBehind ? s.peakBetDeficit.toString() : '-';
  const ply = s.everBehind ? s.peakBetPly.toString() : '-';
  console.log(
    s.opp.slice(0,24).padEnd(26) +
    s.color[0] + '  ' +
    String(s.plies).padStart(5) + '  ' +
    bet.padStart(8) + '  ' +
    def.padStart(10) + '  ' +
    ply.padStart(6) + '  ' +
    String(s.finalMatDiff >= 0 ? '+'+s.finalMatDiff : s.finalMatDiff).padStart(8)
  );
});

console.log('\n=== BASELINE NUMBERS (copy these, compare after engine changes) ===');
const baseline = {
  lossAvgBetScore:    avg(lossBehind, s => s.peakBetScore),
  lossAvgBetDeficit:  avg(lossBehind, s => s.peakBetDeficit),
  winAvgBetScore:     avg(winBehind,  s => s.peakBetScore),
  winAvgBetDeficit:   avg(winBehind,  s => s.peakBetDeficit),
  lossCount: lossStats.length,
  winCount:  winStats.length,
};
console.log(JSON.stringify(baseline, null, 2));
console.log('');
console.log(`SEPARATION: losses bet ${baseline.lossAvgBetScore.toFixed(2)} vs wins bet ${baseline.winAvgBetScore.toFixed(2)}`);
console.log(`Goal: reduce avg bet score in losses toward win level.`);
console.log(`If losses avg bet score drops by 30%+ after a change → meaningful improvement.`);
