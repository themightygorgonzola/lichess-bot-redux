// Positional pattern analysis — not about queens
// For each loss since April 1: measure structural/positional features at key moments
// - pawn majorities, passed pawns, pawn advancement
// - piece imbalances (R vs B, N vs B, piece pairs)
// - king safety / exposure
// - rook activity (open files)
// Look for what the BOT is consistently relying on / fighting for

const { queryGames, getGame } = require('./bot/src/gameDb');
const { parseFen, applyMove, posToFen } = require('./bot/src/fen');

const PIECE_VAL = { p:1, n:3, b:3, r:5, q:9 };

function matOf(board, isW) {
  const m = {};
  for (let r = 0; r < 8; r++) for (let f = 0; f < 8; f++) {
    const pc = board[r][f];
    if (!pc || pc === '.') continue;
    const lc = pc.toLowerCase();
    if (lc === 'k') continue;
    const mine = isW ? pc === pc.toUpperCase() : pc === pc.toLowerCase();
    if (mine) m[lc] = (m[lc] || 0) + 1;
  }
  return m;
}
function score(m){ return Object.entries(m).reduce((s,[p,n])=>s+(PIECE_VAL[p]||0)*n,0); }

// Find king square
function kingSquare(board, isW) {
  for (let r = 0; r < 8; r++) for (let f = 0; f < 8; f++) {
    const pc = board[r][f];
    if (!pc) continue;
    if (isW ? pc === 'K' : pc === 'k') return [r, f];
  }
  return null;
}

// board[0]=rank1, board[7]=rank8
// White pawn rank = r+1, advances toward row 7
// Black pawn rank = 8-r, advances toward row 0

// Count passed pawns for a side
function passedPawns(board, isW) {
  const passed = [];
  for (let r = 0; r < 8; r++) for (let f = 0; f < 8; f++) {
    const pc = board[r][f];
    const isMyPawn = isW ? pc === 'P' : pc === 'p';
    if (!isMyPawn) continue;
    const enemyPawn = isW ? 'p' : 'P';
    // Ahead for white = rows r+1..7; for black = rows 0..r-1
    const ahead = isW
      ? [...Array(7-r).keys()].map(i=>r+1+i)
      : [...Array(r).keys()];
    let blocked = false;
    for (const ar of ahead) {
      for (const af of [f-1, f, f+1]) {
        if (af < 0 || af > 7) continue;
        if (board[ar][af] === enemyPawn) { blocked = true; break; }
      }
      if (blocked) break;
    }
    const rank = isW ? r + 1 : 8 - r; // white: rank1=r0, rank8=r7
    if (!blocked) passed.push({ r, f, rank });
  }
  return passed;
}

// Pawn advancement score
function pawnAdvancement(board, isW) {
  let total = 0, count = 0;
  for (let r = 0; r < 8; r++) for (let f = 0; f < 8; f++) {
    const pc = board[r][f];
    const isMyPawn = isW ? pc === 'P' : pc === 'p';
    if (!isMyPawn) continue;
    const rank = isW ? r + 1 : 8 - r;
    total += rank;
    count++;
  }
  return { total, count, avg: count > 0 ? total/count : 0 };
}

// Pawn islands
function pawnIslands(board, isW) {
  const pawnFiles = new Set();
  for (let r = 0; r < 8; r++) for (let f = 0; f < 8; f++) {
    const pc = board[r][f];
    if (isW ? pc === 'P' : pc === 'p') pawnFiles.add(f);
  }
  if (pawnFiles.size === 0) return 0;
  const sorted = [...pawnFiles].sort((a,b)=>a-b);
  let islands = 1;
  for (let i = 1; i < sorted.length; i++) {
    if (sorted[i] - sorted[i-1] > 1) islands++;
  }
  return islands;
}

// Rooks on open/half-open files
function rookOpenFiles(board, isW) {
  let open = 0, halfOpen = 0;
  const myRook = isW ? 'R' : 'r';
  const myPawn = isW ? 'P' : 'p';
  const enPawn = isW ? 'p' : 'P';
  for (let r = 0; r < 8; r++) for (let f = 0; f < 8; f++) {
    if (board[r][f] !== myRook) continue;
    let hasMine = false, hasEnemy = false;
    for (let rr = 0; rr < 8; rr++) {
      if (board[rr][f] === myPawn) hasMine = true;
      if (board[rr][f] === enPawn) hasEnemy = true;
    }
    if (!hasMine && !hasEnemy) open++;
    else if (!hasMine && hasEnemy) halfOpen++;
  }
  return { open, halfOpen };
}

// King shelter: pawns in front of king
// White king at row r: shelter pawns at rows r+1, r+2 on files f-1..f+1
// Black king at row r: shelter pawns at rows r-1, r-2
function kingShelter(board, isW, kingR, kingF) {
  let count = 0;
  const pawn = isW ? 'P' : 'p';
  for (let df = -1; df <= 1; df++) {
    const f = kingF + df;
    if (f < 0 || f > 7) continue;
    for (let dist = 1; dist <= 2; dist++) {
      const rr = isW ? kingR + dist : kingR - dist;
      if (rr < 0 || rr > 7) continue;
      if (board[rr][f] === pawn) count++;
    }
  }
  return count;
}

// Piece imbalance classification
function imbalanceTag(mat) {
  const q = mat.q || 0, r = mat.r || 0, b = mat.b || 0, n = mat.n || 0;
  if (q >= 1) return 'Q';
  if (r >= 2 && b + n >= 1) return '2R+piece';
  if (r >= 2) return '2R';
  if (r === 1 && b + n >= 2) return 'R+2minor';
  if (r === 1 && b + n === 1) return 'R+minor';
  if (r === 1) return 'R-alone';
  if (b >= 2) return 'Bpair';
  if (b + n >= 2) return '2minor';
  if (b === 1 || n === 1) return '1minor';
  return 'pawns-only';
}

// ---- main analysis ----
const APRIL1 = new Date('2026-04-01T00:00:00Z').getTime();

const games = queryGames({ limit: 1000, offset: 0 });
const recentLosses = games.filter(g =>
  g.bot_result === 'loss' && g.ply_count > 20 && g.ts >= APRIL1
);

console.log(`Recent losses (since April 1): ${recentLosses.length}`);

const snapshots = []; // positional snapshot objects

for (const g of recentLosses) {
  const raw = getGame(g.id);
  if (!raw || !raw.full_moves || !raw.full_moves.length) continue;
  const startFen = raw.initial_fen || 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
  const botIsW = g.our_color === 'white';
  let pos;
  try { pos = parseFen(startFen); } catch(_) { continue; }
  const fens = [startFen];
  for (const uci of raw.full_moves) {
    try { pos = applyMove(pos, uci); fens.push(posToFen(pos)); } catch(_) { break; }
  }
  if (fens.length < 10) continue;

  // Snapshot at: ply 20, midpoint, resign
  const keyPlies = [
    20,
    Math.floor(fens.length * 0.5),
    fens.length - 1,
  ].filter((v, i, a) => a.indexOf(v) === i && v < fens.length && v >= 0);

  const gameSnaps = [];
  for (const ply of keyPlies) {
    let bd;
    try { bd = parseFen(fens[ply]).board; } catch(_) { continue; }

    const botMat = matOf(bd, botIsW);
    const oppMat = matOf(bd, !botIsW);
    const matDiff = score(botMat) - score(oppMat);

    const botPassed = passedPawns(bd, botIsW);
    const oppPassed = passedPawns(bd, !botIsW);
    const botAdv = pawnAdvancement(bd, botIsW);
    const oppAdv = pawnAdvancement(bd, !botIsW);
    const botIslands = pawnIslands(bd, botIsW);
    const oppIslands = pawnIslands(bd, !botIsW);
    const botRooks = rookOpenFiles(bd, botIsW);
    const oppRooks = rookOpenFiles(bd, !botIsW);
    const kSq = kingSquare(bd, botIsW);
    const okSq = kingSquare(bd, !botIsW);
    const botShelter = kSq ? kingShelter(bd, botIsW, kSq[0], kSq[1]) : 0;
    const oppShelter = okSq ? kingShelter(bd, !botIsW, okSq[0], okSq[1]) : 0;

    gameSnaps.push({
      opp: g.opponent, color: g.our_color, speed: g.speed,
      ply,
      matDiff,
      botImbalance: imbalanceTag(botMat),
      oppImbalance: imbalanceTag(oppMat),
      botPassed: botPassed.length,
      oppPassed: oppPassed.length,
      botAdvPawns: botPassed.filter(p => p.rank >= 5).length,  // rank 5,6,7
      oppAdvPawns: oppPassed.filter(p => p.rank >= 5).length,
      botPawnAvgRank: botAdv.avg.toFixed(1),
      oppPawnAvgRank: oppAdv.avg.toFixed(1),
      botIslands, oppIslands,
      botOpenFiles: botRooks.open, botHalfOpenFiles: botRooks.halfOpen,
      oppOpenFiles: oppRooks.open, oppHalfOpenFiles: oppRooks.halfOpen,
      botShelter, oppShelter,
      fen: fens[ply].split(' ')[0],
    });
  }

  // Tag the game's midgame imbalance pattern
  const midSnap = gameSnaps.find(s => s.ply === Math.floor(fens.length * 0.5));
  const resignSnap = gameSnaps[gameSnaps.length - 1];

  if (midSnap && resignSnap) {
    snapshots.push({ gameId: g.id, opp: g.opponent, color: g.our_color,
      speed: g.speed, totalPlies: fens.length,
      mid: midSnap, resign: resignSnap });
  }
}

console.log(`Snapshots collected: ${snapshots.length}\n`);

// === What imbalances does the bot consistently fight with? ===
console.log('=== Bot\'s piece configuration at MIDGAME ===');
const midImbal = {};
for (const s of snapshots) {
  const k = `bot:${s.mid.botImbalance} vs opp:${s.mid.oppImbalance}`;
  midImbal[k] = (midImbal[k] || 0) + 1;
}
Object.entries(midImbal).sort((a,b)=>b[1]-a[1]).slice(0,15)
  .forEach(([k,v])=>console.log(`  ${k.padEnd(35)} ${v}`));

console.log('\n=== Bot\'s piece configuration at RESIGNATION ===');
const resignImbal = {};
for (const s of snapshots) {
  const k = `bot:${s.resign.botImbalance} vs opp:${s.resign.oppImbalance}`;
  resignImbal[k] = (resignImbal[k] || 0) + 1;
}
Object.entries(resignImbal).sort((a,b)=>b[1]-a[1]).slice(0,15)
  .forEach(([k,v])=>console.log(`  ${k.padEnd(35)} ${v}`));

// === Passed pawn patterns ===
console.log('\n=== Passed pawn differential at midgame (bot - opp) ===');
let botMorePassed = 0, oppMorePassed = 0, equal = 0;
let totalBotPassed = 0, totalOppPassed = 0;
let botHasAdvanced = 0;
for (const s of snapshots) {
  const diff = s.mid.botPassed - s.mid.oppPassed;
  if (diff > 0) botMorePassed++;
  else if (diff < 0) oppMorePassed++;
  else equal++;
  totalBotPassed += s.mid.botPassed;
  totalOppPassed += s.mid.oppPassed;
  if (s.mid.botAdvPawns > 0) botHasAdvanced++;
}
const n = snapshots.length;
console.log(`  Bot has more passed pawns: ${botMorePassed}  Opp has more: ${oppMorePassed}  Equal: ${equal}`);
console.log(`  Avg bot passed: ${(totalBotPassed/n).toFixed(2)}  Avg opp passed: ${(totalOppPassed/n).toFixed(2)}`);
console.log(`  Games where bot has advanced passed (rank 5+): ${botHasAdvanced}`);

// === Pawn advancement ===
console.log('\n=== Average pawn rank at MIDGAME ===');
let sumBotRank=0, sumOppRank=0, cnt=0;
for (const s of snapshots) {
  sumBotRank += parseFloat(s.mid.botPawnAvgRank);
  sumOppRank += parseFloat(s.mid.oppPawnAvgRank);
  cnt++;
}
console.log(`  Bot avg pawn rank: ${(sumBotRank/cnt).toFixed(2)}  Opp avg pawn rank: ${(sumOppRank/cnt).toFixed(2)}`);

// === Rook activity ===
console.log('\n=== Rook open-file activity at midgame ===');
let botTotalOpen=0, oppTotalOpen=0;
for (const s of snapshots) {
  botTotalOpen += s.mid.botOpenFiles + s.mid.botHalfOpenFiles * 0.5;
  oppTotalOpen += s.mid.oppOpenFiles + s.mid.oppHalfOpenFiles * 0.5;
}
console.log(`  Avg bot rook-file score: ${(botTotalOpen/n).toFixed(2)}  Opp: ${(oppTotalOpen/n).toFixed(2)}`);

// === King safety ===
console.log('\n=== King shelter (pawn cover) at midgame ===');
let botShelt=0, oppShelt=0;
for (const s of snapshots) { botShelt += s.mid.botShelter; oppShelt += s.mid.oppShelter; }
console.log(`  Avg bot shelter: ${(botShelt/n).toFixed(2)}  Opp shelter: ${(oppShelt/n).toFixed(2)}`);

// === Pawn islands ===
console.log('\n=== Pawn islands at midgame (fewer = more connected) ===');
let botIsl=0, oppIsl=0;
for (const s of snapshots) { botIsl += s.mid.botIslands; oppIsl += s.mid.oppIslands; }
console.log(`  Avg bot islands: ${(botIsl/n).toFixed(2)}  Opp islands: ${(oppIsl/n).toFixed(2)}`);

// === Material diff at midgame ===
console.log('\n=== Material differential at midgame (bot - opp) ===');
const matBuckets = { 'behind-3+': 0, 'behind-1-2': 0, 'even': 0, 'ahead-1-2': 0, 'ahead-3+': 0 };
let sumMatMid = 0;
for (const s of snapshots) {
  const d = s.mid.matDiff;
  sumMatMid += d;
  if (d <= -3) matBuckets['behind-3+']++;
  else if (d < 0) matBuckets['behind-1-2']++;
  else if (d === 0) matBuckets['even']++;
  else if (d <= 2) matBuckets['ahead-1-2']++;
  else matBuckets['ahead-3+']++;
}
Object.entries(matBuckets).forEach(([k,v])=>console.log(`  ${k.padEnd(15)} ${v}`));
console.log(`  Avg matDiff at midgame: ${(sumMatMid/n).toFixed(2)}`);

// === Sample: games where bot is MATERIALLY AHEAD at midgame but loses ===
console.log('\n=== Games where bot is materially ahead (3+ pts) at midgame but loses ===');
const aheadLosses = snapshots.filter(s => s.mid.matDiff >= 3);
console.log(`Count: ${aheadLosses.length}`);
for (const s of aheadLosses.slice(0, 10)) {
  console.log(`  ${s.opp.slice(0,22).padEnd(24)} ${s.color} ply:${s.mid.ply}/${s.totalPlies} matDiff:+${s.mid.matDiff} bot:${s.mid.botImbalance} vs opp:${s.mid.oppImbalance} passed(b/o):${s.mid.botPassed}/${s.mid.oppPassed}`);
  console.log(`    → resign: matDiff:${s.resign.matDiff} bot:${s.resign.botImbalance} vs opp:${s.resign.oppImbalance}`);
  console.log(`    → mid FEN: ${s.mid.fen}`);
}

// === Piece imbalance flows: what changes between midgame and resign? ===
console.log('\n=== How bot piece configuration CHANGES from mid to resign ===');
const flows = {};
for (const s of snapshots) {
  const k = `${s.mid.botImbalance}→${s.resign.botImbalance}`;
  if (!flows[k]) flows[k] = { count: 0, avgMatChange: 0 };
  flows[k].count++;
  flows[k].avgMatChange += (s.resign.matDiff - s.mid.matDiff);
}
Object.entries(flows).sort((a,b)=>b[1].count-a[1].count).slice(0,12)
  .forEach(([k,v])=>console.log(`  ${k.padEnd(25)} ${v.count} games  avg mat change: ${(v.avgMatChange/v.count).toFixed(1)}`));
