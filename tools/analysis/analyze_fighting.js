// What does the bot's fighting position look like near the end?
// Focus on 15-20 plies before resign, and look at:
// - pawn structure quality (connected/isolated/passed)
// - piece cooperation
// - the specific IMBALANCE at that moment

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

// board[0]=rank1, white to rank8, black to rank1
function detailedPawnAnalysis(board, isW) {
  const pawns = [];
  for (let r = 0; r < 8; r++) for (let f = 0; f < 8; f++) {
    const pc = board[r][f];
    if (!(isW ? pc === 'P' : pc === 'p')) continue;
    const rank = isW ? r + 1 : 8 - r;
    const enemyPawn = isW ? 'p' : 'P';
    const myPawn = isW ? 'P' : 'p';

    // Passed?
    const ahead = isW ? [...Array(7-r).keys()].map(i=>r+1+i) : [...Array(r).keys()];
    let passed = true;
    for (const ar of ahead) {
      for (const af of [f-1, f, f+1]) {
        if (af >= 0 && af <= 7 && board[ar][af] === enemyPawn) { passed = false; break; }
      }
      if (!passed) break;
    }

    // Isolated? (no friendly pawns on adjacent files)
    let isolated = true;
    for (let rr = 0; rr < 8; rr++) {
      if ((f > 0 && board[rr][f-1] === myPawn) || (f < 7 && board[rr][f+1] === myPawn)) {
        isolated = false; break;
      }
    }

    // Doubled? (another pawn on same file)
    let doubled = false;
    for (let rr = 0; rr < 8; rr++) {
      if (rr !== r && board[rr][f] === myPawn) { doubled = true; break; }
    }

    // Protected (another pawn defends it)
    let protected_ = false;
    const behind = isW ? r - 1 : r + 1;
    if (behind >= 0 && behind <= 7) {
      if ((f > 0 && board[behind][f-1] === myPawn) || (f < 7 && board[behind][f+1] === myPawn))
        protected_ = true;
    }

    pawns.push({ r, f, rank, passed, isolated, doubled, protected_ });
  }

  const connected = pawns.filter(p => !p.isolated).length;
  const passedCount = pawns.filter(p => p.passed).length;
  const isolatedCount = pawns.filter(p => p.isolated).length;
  const doubledCount = pawns.filter(p => p.doubled).length;
  const advancedPassed = pawns.filter(p => p.passed && p.rank >= 5).length;
  const total = pawns.length;
  const maxRank = pawns.length ? Math.max(...pawns.map(p=>p.rank)) : 0;

  return { total, connected, passedCount, isolatedCount, doubledCount, advancedPassed, maxRank };
}

function kingRow(board, isW) {
  for (let r = 0; r < 8; r++) for (let f = 0; f < 8; f++) {
    const pc = board[r][f];
    if (isW ? pc === 'K' : pc === 'k') return isW ? r + 1 : 8 - r; // rank
  }
  return null;
}

const APRIL1 = new Date('2026-04-01T00:00:00Z').getTime();
const games = queryGames({ limit: 1000 });
const recentLosses = games.filter(g => g.bot_result === 'loss' && g.ply_count > 20 && g.ts >= APRIL1);

const LOOK_BACK = 15; // plies before resign

const fightingStats = [];

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

  // Snapshot at LOOK_BACK plies before end
  const snapPly = Math.max(10, fens.length - 1 - LOOK_BACK);
  let bd; try { bd = parseFen(fens[snapPly]).board; } catch(_) { continue; }

  const botMat = matOf(bd, botIsW);
  const oppMat = matOf(bd, !botIsW);
  const matDiff = score(botMat) - score(oppMat);

  const botPawns = detailedPawnAnalysis(bd, botIsW);
  const oppPawns = detailedPawnAnalysis(bd, !botIsW);

  const botKingRank = kingRow(bd, botIsW);
  const oppKingRank = kingRow(bd, !botIsW);

  // Bot's piece composition
  const botQ = botMat.q || 0, botR = botMat.r || 0;
  const botB = botMat.b || 0, botN = botMat.n || 0;
  const oppQ = oppMat.q || 0, oppR = oppMat.r || 0;
  const oppB = oppMat.b || 0, oppN = oppMat.n || 0;

  // Imbalance
  const majDiff = (botQ*9 + botR*5) - (oppQ*9 + oppR*5);
  const minDiff = (botB + botN) - (oppB + oppN);
  const pawnDiff = botPawns.total - oppPawns.total;
  const passedDiff = botPawns.passedCount - oppPawns.passedCount;

  fightingStats.push({
    id: g.id, opp: g.opponent, color: g.our_color,
    ply: snapPly, totalPlies: fens.length,
    matDiff,
    botQ, botR, botB, botN, oppQ, oppR, oppB, oppN,
    majDiff, minDiff, pawnDiff, passedDiff,
    botPawns, oppPawns,
    botKingRank, oppKingRank,
    fen: fens[snapPly].split(' ')[0],
  });
}

const n = fightingStats.length;
console.log(`Fighting position snapshots: ${n} (at ~${LOOK_BACK} plies before resign)\n`);

// What is the bot fighting WITH?
console.log('=== Bot piece composition in fighting position ===');
const botMajCfg = {};
for (const s of fightingStats) {
  const k = `Q:${s.botQ} R:${s.botR} B:${s.botB} N:${s.botN}`;
  botMajCfg[k] = (botMajCfg[k]||0)+1;
}
Object.entries(botMajCfg).sort((a,b)=>b[1]-a[1]).slice(0,10)
  .forEach(([k,v])=>console.log(`  ${k.padEnd(20)} ${v}`));

// Imbalance type at fighting position
console.log('\n=== Piece imbalance in fighting position ===');
const imbalTypes = {};
for (const s of fightingStats) {
  // Classify the fundamental imbalance
  let k;
  if (s.botQ > 0 && s.oppQ > 0) k = 'Q vs Q (symmetric majors)';
  else if (s.botQ === 0 && s.oppQ > 0) k = 'bot no Q, opp has Q';
  else if (s.botQ > 0 && s.oppQ === 0) k = 'bot has Q, opp no Q';
  else if (s.majDiff > 0) k = 'bot ahead in majors (rookish)';
  else if (s.majDiff < 0) k = 'opp ahead in majors';
  else k = 'no majors';
  imbalTypes[k] = (imbalTypes[k]||0)+1;
}
Object.entries(imbalTypes).sort((a,b)=>b[1]-a[1]).forEach(([k,v])=>console.log(`  ${k.padEnd(35)} ${v}`));

// Pawn count differential
console.log('\n=== Pawn count differential in fighting position (bot - opp) ===');
let pawnDiffSum = 0, passedDiffSum = 0;
const pawnDiffBuckets = {};
for (const s of fightingStats) {
  pawnDiffSum += s.pawnDiff;
  passedDiffSum += s.passedDiff;
  const k = s.pawnDiff > 3 ? 'bot_up_4+' : s.pawnDiff > 0 ? `bot_up_${s.pawnDiff}` : s.pawnDiff < 0 ? `opp_up_${-s.pawnDiff}` : 'equal';
  pawnDiffBuckets[k] = (pawnDiffBuckets[k]||0)+1;
}
console.log(`Avg pawn diff (bot - opp): ${(pawnDiffSum/n).toFixed(2)}`);
console.log(`Avg passed pawn diff (bot - opp): ${(passedDiffSum/n).toFixed(2)}`);
Object.entries(pawnDiffBuckets).sort().forEach(([k,v])=>console.log(`  ${k.padEnd(12)} ${v}`));

// Pawn QUALITY
console.log('\n=== Bot pawn structure quality in fighting position ===');
let sumBotPassed=0, sumOppPassed=0, sumBotAdv=0, sumOppAdv=0;
let sumBotIso=0, sumOppIso=0, sumBotConn=0, sumOppConn=0;
let sumBotMaxRank=0, sumOppMaxRank=0;
for (const s of fightingStats) {
  sumBotPassed += s.botPawns.passedCount;
  sumOppPassed += s.oppPawns.passedCount;
  sumBotAdv += s.botPawns.advancedPassed;
  sumOppAdv += s.oppPawns.advancedPassed;
  sumBotIso += s.botPawns.isolatedCount;
  sumOppIso += s.oppPawns.isolatedCount;
  sumBotConn += s.botPawns.connected;
  sumOppConn += s.oppPawns.connected;
  sumBotMaxRank += s.botPawns.maxRank;
  sumOppMaxRank += s.oppPawns.maxRank;
}
console.log(`  Avg passed:         bot ${(sumBotPassed/n).toFixed(2)}  opp ${(sumOppPassed/n).toFixed(2)}`);
console.log(`  Avg adv passed(5+): bot ${(sumBotAdv/n).toFixed(2)}  opp ${(sumOppAdv/n).toFixed(2)}`);
console.log(`  Avg isolated:       bot ${(sumBotIso/n).toFixed(2)}  opp ${(sumOppIso/n).toFixed(2)}`);
console.log(`  Avg connected:      bot ${(sumBotConn/n).toFixed(2)}  opp ${(sumOppConn/n).toFixed(2)}`);
console.log(`  Avg farthest pawn:  bot ${(sumBotMaxRank/n).toFixed(2)}  opp ${(sumOppMaxRank/n).toFixed(2)}`);

// Bot king position (is it active or passive?)
console.log('\n=== King rank in fighting position (1=own back rank, 8=opp back rank) ===');
let sumBotKing=0, sumOppKing=0;
for (const s of fightingStats) {
  if (s.botKingRank) sumBotKing += s.botKingRank;
  if (s.oppKingRank) sumOppKing += s.oppKingRank;
}
console.log(`  Avg bot king rank: ${(sumBotKing/n).toFixed(2)}  Avg opp king rank: ${(sumOppKing/n).toFixed(2)} (opponent relative)`);
// Note: for opponent's king rank we compute from THEIR side, not from our FEN perspective

// Games where bot has material advantage but fights with fewer pawns
console.log('\n=== Games: bot ahead in pieces, behind in pawns ===');
const pieceAheadPawnBehind = fightingStats.filter(s => s.minDiff + (s.majDiff > 0 ? 1 : 0) > 0 && s.pawnDiff < 0);
console.log(`Count: ${pieceAheadPawnBehind.length}`);
for (const s of pieceAheadPawnBehind.slice(0,5)) {
  console.log(`  ${s.opp.slice(0,22).padEnd(24)} matDiff:${s.matDiff} pieces(b/o):Q${s.botQ}R${s.botR}B${s.botB}N${s.botN} vs Q${s.oppQ}R${s.oppR}B${s.oppB}N${s.oppN} pawnDiff:${s.pawnDiff}`);
}

// The ACTUAL FEN positions near resign — just print them for inspection
console.log('\n=== Sample fighting positions (FEN ~15 plies before resign) ===');
for (const s of fightingStats.slice(0, 20)) {
  const imbal = s.botQ > 0 && s.oppQ === 0 ? 'BOT_Q'
    : s.botQ === 0 && s.oppQ > 0 ? 'OPP_Q'
    : s.botQ > 0 && s.oppQ > 0 ? 'BOTH_Q'
    : 'NO_Q';
  console.log(`  [${imbal}] ${s.opp.slice(0,18).padEnd(20)} ${s.color[0]} ply:${s.ply}/${s.totalPlies} mat:${s.matDiff>=0?'+':''}${s.matDiff} pawns(b/o):${s.botPawns.total}/${s.oppPawns.total} passed(b/o):${s.botPawns.passedCount}/${s.oppPawns.passedCount} advPass:${s.botPawns.advancedPassed} botK:rank${s.botKingRank}`);
  console.log(`     FEN: ${s.fen}`);
}
