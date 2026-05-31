// Track material trajectory and find the turning point
// For games where bot was materially ahead at midgame but lost:
// find the specific ply where the advantage collapsed and what structure existed

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
function fmt(m){ return Object.entries(m).map(([p,n])=>n+p).join('')||'(none)'; }

// Passed pawns board[0]=rank1, white advances toward rank8 (row7)
function passedPawns(board, isW) {
  let count = 0;
  for (let r = 0; r < 8; r++) for (let f = 0; f < 8; f++) {
    const pc = board[r][f];
    if (!(isW ? pc === 'P' : pc === 'p')) continue;
    const enemyPawn = isW ? 'p' : 'P';
    const ahead = isW
      ? [...Array(7-r).keys()].map(i => r+1+i)
      : [...Array(r).keys()];
    let blocked = false;
    for (const ar of ahead) {
      for (const af of [f-1, f, f+1]) {
        if (af < 0 || af > 7) continue;
        if (board[ar][af] === enemyPawn) { blocked = true; break; }
      }
      if (blocked) break;
    }
    if (!blocked) count++;
  }
  return count;
}

function pawnCount(board, isW) {
  let n = 0;
  for (let r = 0; r < 8; r++) for (let f = 0; f < 8; f++) {
    const pc = board[r][f];
    if (isW ? pc === 'P' : pc === 'p') n++;
  }
  return n;
}

const APRIL1 = new Date('2026-04-01T00:00:00Z').getTime();
const games = queryGames({ limit: 1000 });
const recentLosses = games.filter(g => g.bot_result === 'loss' && g.ply_count > 20 && g.ts >= APRIL1);

// For ALL losses: build full material trajectory, find peak and collapse
const trajectories = [];

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

  const matSeries = [];
  for (const fen of fens) {
    let bd; try { bd = parseFen(fen).board; } catch(_) { break; }
    const bm = matOf(bd, botIsW);
    const om = matOf(bd, !botIsW);
    matSeries.push({
      diff: score(bm) - score(om),
      botScore: score(bm),
      oppScore: score(om),
      botFmt: fmt(bm),
      oppFmt: fmt(om),
      botPassed: passedPawns(bd, botIsW),
      oppPassed: passedPawns(bd, !botIsW),
      botPawns: pawnCount(bd, botIsW),
      oppPawns: pawnCount(bd, !botIsW),
    });
  }

  if (matSeries.length < 15) continue;

  // Find peak bot advantage
  let peakDiff = -99, peakPly = 0;
  for (let i = 5; i < matSeries.length; i++) {
    if (matSeries[i].diff > peakDiff) { peakDiff = matSeries[i].diff; peakPly = i; }
  }

  // Find collapse: first ply after peak where diff drops by 3+ from peak
  let collapsePly = null, collapseDropSize = 0;
  for (let i = peakPly + 1; i < matSeries.length; i++) {
    const drop = peakDiff - matSeries[i].diff;
    if (drop >= 3 && collapsePly === null) {
      collapsePly = i;
      collapseDropSize = drop;
    }
  }

  const finalDiff = matSeries[matSeries.length - 1].diff;
  const peakSnap = matSeries[peakPly];
  const collapseSnap = collapsePly ? matSeries[collapsePly] : null;

  trajectories.push({
    id: g.id, opp: g.opponent, color: g.our_color, speed: g.speed,
    totalPlies: fens.length,
    peakDiff, peakPly,
    peakBot: peakSnap.botFmt, peakOpp: peakSnap.oppFmt,
    peakBotPassed: peakSnap.botPassed, peakOppPassed: peakSnap.oppPassed,
    peakBotPawns: peakSnap.botPawns, peakOppPawns: peakSnap.oppPawns,
    collapsePly, collapseDropSize,
    collapseBot: collapseSnap ? collapseSnap.botFmt : null,
    collapseOpp: collapseSnap ? collapseSnap.oppFmt : null,
    finalDiff,
    peakFen: fens[peakPly] ? fens[peakPly].split(' ')[0] : '',
    collapseFen: (collapsePly && fens[collapsePly]) ? fens[collapsePly].split(' ')[0] : '',
    moves: raw.full_moves,
  });
}

// ====== ANALYSIS ======

console.log(`Analyzed ${trajectories.length} games\n`);

// Peak advantage distribution
console.log('=== Bot peak material advantage during game ===');
const peakBuckets = { 'even_or_behind': 0, '1-3pts': 0, '4-6pts': 0, '7-9pts': 0, '10+pts': 0 };
for (const t of trajectories) {
  if (t.peakDiff <= 0) peakBuckets['even_or_behind']++;
  else if (t.peakDiff <= 3) peakBuckets['1-3pts']++;
  else if (t.peakDiff <= 6) peakBuckets['4-6pts']++;
  else if (t.peakDiff <= 9) peakBuckets['7-9pts']++;
  else peakBuckets['10+pts']++;
}
Object.entries(peakBuckets).forEach(([k,v]) => console.log(`  ${k.padEnd(15)} ${v}`));

// What PIECES does the bot have at its peak advantage?
console.log('\n=== Bot\'s pieces at peak advantage point ===');
const peakPieceConfig = {};
for (const t of trajectories) {
  peakPieceConfig[t.peakBot] = (peakPieceConfig[t.peakBot] || 0) + 1;
}
Object.entries(peakPieceConfig).sort((a,b)=>b[1]-a[1]).slice(0,12)
  .forEach(([k,v]) => console.log(`  ${k.padEnd(20)} ${v}`));

// What does the bot have at peak vs what opp has?
console.log('\n=== Piece imbalance at peak advantage ===');
function tag(fmt) {
  if (fmt.includes('1q')) return 'Q';
  if (fmt.includes('2r') && (fmt.includes('b') || fmt.includes('n'))) return '2R+minor';
  if (fmt.includes('2r')) return '2R';
  if (fmt.includes('1r') && (fmt.includes('2b') || fmt.includes('2n') || (fmt.includes('1b') && fmt.includes('1n')))) return 'R+2m';
  if (fmt.includes('1r') && (fmt.includes('1b') || fmt.includes('1n'))) return 'R+1m';
  if (fmt.includes('1r')) return 'R';
  if (fmt.includes('2b') || fmt.includes('2n') || (fmt.includes('1b') && fmt.includes('1n'))) return '2minor';
  if (fmt.includes('1b') || fmt.includes('1n')) return '1minor';
  return 'pawns';
}
const imbalMap = {};
for (const t of trajectories) {
  const k = `bot:${tag(t.peakBot)} opp:${tag(t.peakOpp)}`;
  imbalMap[k] = (imbalMap[k]||0)+1;
}
Object.entries(imbalMap).sort((a,b)=>b[1]-a[1]).forEach(([k,v])=>console.log(`  ${k.padEnd(30)} ${v}`));

// Passed pawn advantage at peak
console.log('\n=== Passed pawn counts at bot\'s peak advantage ===');
let botPassTotal=0, oppPassTotal=0, n=trajectories.length;
let botMorePass=0, oppMorePass=0, equalPass=0;
for (const t of trajectories) {
  botPassTotal += t.peakBotPassed;
  oppPassTotal += t.peakOppPassed;
  if (t.peakBotPassed > t.peakOppPassed) botMorePass++;
  else if (t.peakBotPassed < t.peakOppPassed) oppMorePass++;
  else equalPass++;
}
console.log(`  Avg bot passed: ${(botPassTotal/n).toFixed(2)}  Avg opp passed: ${(oppPassTotal/n).toFixed(2)}`);
console.log(`  Bot has more: ${botMorePass}  Opp has more: ${oppMorePass}  Equal: ${equalPass}`);

// Pawn count asymmetry at peak
console.log('\n=== Pawn counts at bot\'s peak advantage ===');
let botPawnTot=0, oppPawnTot=0;
for (const t of trajectories) { botPawnTot += t.peakBotPawns; oppPawnTot += t.peakOppPawns; }
console.log(`  Avg bot pawns: ${(botPawnTot/n).toFixed(2)}  Avg opp pawns: ${(oppPawnTot/n).toFixed(2)}`);

const pawnDiffBuckets = {};
for (const t of trajectories) {
  const d = t.peakBotPawns - t.peakOppPawns;
  const k = d > 0 ? `bot_up_${Math.min(d,4)}` : d < 0 ? `opp_up_${Math.min(-d,4)}` : 'equal';
  pawnDiffBuckets[k] = (pawnDiffBuckets[k]||0)+1;
}
Object.entries(pawnDiffBuckets).sort().forEach(([k,v])=>console.log(`  ${k.padEnd(15)} ${v}`));

// ====== Collapse analysis ======
console.log('\n=== When does the advantage collapse? ===');
const hasCollapse = trajectories.filter(t => t.collapsePly !== null);
console.log(`Games with identified collapse: ${hasCollapse.length}/${trajectories.length}`);

const collapseAtPhase = { 'early-1-20': 0, 'mid-21-50': 0, 'late-51+': 0 };
for (const t of hasCollapse) {
  if (t.collapsePly <= 20) collapseAtPhase['early-1-20']++;
  else if (t.collapsePly <= 50) collapseAtPhase['mid-21-50']++;
  else collapseAtPhase['late-51+']++;
}
Object.entries(collapseAtPhase).forEach(([k,v])=>console.log(`  ${k.padEnd(15)} ${v}`));

// Key: what piece configuration does the bot collapse FROM?
console.log('\n=== What piece config does bot have JUST BEFORE the collapse? ===');
const fromConfig = {};
for (const t of hasCollapse) {
  fromConfig[t.peakBot] = (fromConfig[t.peakBot]||0)+1;
}
Object.entries(fromConfig).sort((a,b)=>b[1]-a[1]).slice(0,10)
  .forEach(([k,v])=>console.log(`  ${k.padEnd(25)} ${v}`));

// What's the bot left WITH after collapse?
console.log('\n=== What is bot left with AFTER collapse? ===');
const afterConfig = {};
for (const t of hasCollapse) {
  afterConfig[t.collapseBot] = (afterConfig[t.collapseBot]||0)+1;
}
Object.entries(afterConfig).sort((a,b)=>b[1]-a[1]).slice(0,10)
  .forEach(([k,v])=>console.log(`  ${k.padEnd(25)} ${v}`));

// The KEY: what does bot have MORE of at peak (what it's relying on)
console.log('\n=== Bot advantage type at peak (what it has MORE of than opp) ===');
const advantageType = {};
for (const t of trajectories) {
  const botFmt = t.peakBot;
  const oppFmt = t.peakOpp;
  // Does bot have more major pieces? more minors? more pawns?
  const botQ = (botFmt.match(/(\d+)q/) || [,0])[1]*1;
  const oppQ = (oppFmt.match(/(\d+)q/) || [,0])[1]*1;
  const botR = (botFmt.match(/(\d+)r/) || [,0])[1]*1;
  const oppR = (oppFmt.match(/(\d+)r/) || [,0])[1]*1;
  const botMinor = ((botFmt.match(/(\d+)b/) || [,0])[1]*1) + ((botFmt.match(/(\d+)n/) || [,0])[1]*1);
  const oppMinor = ((oppFmt.match(/(\d+)b/) || [,0])[1]*1) + ((oppFmt.match(/(\d+)n/) || [,0])[1]*1);
  const botP = t.peakBotPawns;
  const oppP = t.peakOppPawns;

  const majDiff = (botQ*9 + botR*5) - (oppQ*9 + oppR*5);
  const minDiff = botMinor - oppMinor;
  const pawnDiff = botP - oppP;

  let atype;
  if (majDiff > 0 && pawnDiff >= 0) atype = 'major+pawns';
  else if (majDiff > 0 && pawnDiff < 0) atype = 'major_only';
  else if (majDiff < 0 && pawnDiff > 0) atype = 'Q-sacrifice_for_pawns';
  else if (majDiff === 0 && minDiff > 0) atype = 'extra_minor';
  else if (majDiff === 0 && pawnDiff > 0) atype = 'pawn_majority';
  else if (majDiff < 0 && minDiff > 0) atype = 'Q-sac_for_minors';
  else atype = 'even_or_other';

  advantageType[atype] = (advantageType[atype]||0)+1;
}
Object.entries(advantageType).sort((a,b)=>b[1]-a[1]).forEach(([k,v])=>console.log(`  ${k.padEnd(25)} ${v}`));

// Show details for Q-sacrifice games
console.log('\n=== Q-sacrifice games: what does bot have vs opp at peak? ===');
for (const t of trajectories) {
  const botQ = (t.peakBot.match(/(\d+)q/) || [,0])[1]*1;
  const oppQ = (t.peakOpp.match(/(\d+)q/) || [,0])[1]*1;
  if (botQ < oppQ) {
    console.log(`  ${t.opp.slice(0,22).padEnd(24)} peak+${t.peakDiff} ply:${t.peakPly} bot:[${t.peakBot}] vs opp:[${t.peakOpp}] passed(b/o):${t.peakBotPassed}/${t.peakOppPassed} pawns(b/o):${t.peakBotPawns}/${t.peakOppPawns}`);
    console.log(`    → after: bot:[${t.collapseBot||'?'}] vs opp:[${t.collapseOpp||'?'}] finalDiff:${t.finalDiff}`);
    console.log(`    peak FEN: ${t.peakFen}`);
  }
}
