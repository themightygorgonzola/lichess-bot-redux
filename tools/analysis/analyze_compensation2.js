// Deep dive: what position/compensation did the bot see when it "chose" to lose its queen?
// Focus on cases where bot made a CAPTURE leaving its queen hanging — the clearest intentional case.

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

function qSqOf(board, isW) {
  for (let r = 0; r < 8; r++) for (let f = 0; f < 8; f++) {
    const pc = board[r][f];
    if (!pc) continue;
    const isQ = isW ? pc === 'Q' : pc === 'q';
    if (isQ) return 'abcdefgh'[f] + (8 - r);
  }
  return null;
}

// --- Walk a game, collect full queen-loss context ---
function analyzeGame(g) {
  const raw = getGame(g.id);
  if (!raw || !raw.full_moves || !raw.full_moves.length) return null;
  const startFen = raw.initial_fen || 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
  const botIsW = g.our_color === 'white';
  let pos;
  try { pos = parseFen(startFen); } catch(_) { return null; }
  const fens = [startFen];
  const moves = raw.full_moves;
  for (const uci of moves) {
    try { pos = applyMove(pos, uci); fens.push(posToFen(pos)); } catch(_) { break; }
  }

  let botHadQ = false, qLostPly = null;
  for (let i = 0; i < fens.length; i++) {
    let bd; try { bd = parseFen(fens[i]).board; } catch(_) { break; }
    const bm = matOf(bd, botIsW);
    if (bm.q > 0) botHadQ = true;
    if (botHadQ && !bm.q && qLostPly === null) { qLostPly = i; break; }
  }
  if (!botHadQ || qLostPly === null || qLostPly < 4) return null;

  // Ply indices:
  //   qLostPly-2: FEN before bot's intention move
  //   qLostPly-1: FEN before capture (after bot's intention move — queen is still alive but perhaps hanging)
  //   qLostPly:   FEN after queen captured
  //   qLostPly+1: FEN after bot's response
  //   qLostPly+2: FEN after opp response to bot's response

  const fenT2 = fens[qLostPly - 2]; // before bot moved
  const fenT1 = fens[qLostPly - 1]; // after bot moved (queen maybe hanging)
  const fenT0 = fens[qLostPly];     // after queen captured
  const fenR1 = fens[qLostPly + 1]; // after bot's recovery move

  let bdT2, bdT1, bdT0, bdR1;
  try { bdT2 = parseFen(fenT2).board; } catch(_) {}
  try { bdT1 = parseFen(fenT1).board; } catch(_) {}
  try { bdT0 = parseFen(fenT0).board; } catch(_) {}
  try { if (fenR1) bdR1 = parseFen(fenR1).board; } catch(_) {}

  if (!bdT2 || !bdT1 || !bdT0) return null;

  const botIntentionMove = moves[qLostPly - 2];
  const captureMove = moves[qLostPly - 1];
  const botRecoveryMove = moves[qLostPly] || null;

  // Where was queen before bot's intention move?
  const qSqBefore = qSqOf(bdT2, botIsW);
  // Where is queen after bot's intention move (should still be there)?
  const qSqAfter1 = qSqOf(bdT1, botIsW);
  // Did bot's intention move the queen?
  const queenMoved = qSqBefore && qSqAfter1 && qSqBefore !== qSqAfter1;

  // What did bot capture with intention move?
  const intentToF = botIntentionMove.charCodeAt(2) - 97;
  const intentToR = 8 - parseInt(botIntentionMove[3]);
  const capturedPiece = bdT2[intentToR][intentToF]; // what was on destination before bot moved
  const capturedVal = capturedPiece && capturedPiece !== '.' ? (PIECE_VAL[capturedPiece.toLowerCase()] || 0) : 0;

  // Material situation at each stage
  const matT2 = { bot: score(matOf(bdT2, botIsW)), opp: score(matOf(bdT2, !botIsW)) };
  const matT1 = { bot: score(matOf(bdT1, botIsW)), opp: score(matOf(bdT1, !botIsW)) };
  const matT0 = { bot: score(matOf(bdT0, botIsW)), opp: score(matOf(bdT0, !botIsW)) };

  const botT0Mat = matOf(bdT0, botIsW);
  const oppT0Mat = matOf(bdT0, !botIsW);
  const matDiff = matT0.bot - matT0.opp;

  // What piece captured the queen?
  const capToF = captureMove.charCodeAt(2) - 97;
  const capToR = 8 - parseInt(captureMove[3]);
  const captureByPiece = bdT1[capToR][capToF]; // piece that moved to queen's square

  // Was the queen on a square where it was attacking something valuable?
  // (what did bot gain from the intention move swap)
  const swapGain = capturedVal; // what bot captured with intention move
  const swapLoss = 9; // queen always = 9

  return {
    id: g.id, opp: g.opponent, color: g.our_color, speed: g.speed,
    plies: g.ply_count, qLostPly,
    botIntentionMove, captureMove, botRecoveryMove,
    queenMoved,
    qSqBefore, qSqAfter: qSqAfter1,
    capturedPiece: capturedPiece || '-',
    capturedVal,
    captureByPiece: captureByPiece || '?',
    swapGain, swapLoss, netSwap: swapGain - swapLoss,
    matBefore: matT2.bot - matT2.opp,
    matAfterIntention: matT1.bot - matT1.opp,
    matAfterQLoss: matDiff,
    botLeftPieces: fmt(botT0Mat),
    oppLeftPieces: fmt(oppT0Mat),
    fenBeforeIntention: fenT2,
    fenAfterIntention: fenT1,
  };
}

const games = queryGames({ limit: 500, offset: 0 });
const losses = games.filter(g => g.bot_result === 'loss' && g.ply_count > 20);

// Exclude the deterministic odds-opening-trap cases for this analysis
const realLosses = losses.filter(g =>
  !(g.opponent.includes('PawnOdds') && g.our_color === 'white')
);

const results = realLosses.map(analyzeGame).filter(Boolean);

// Separate: cases where bot actually moved its queen (walked into capture)
// vs cases where bot played something else (left queen hanging)
const walkedIn  = results.filter(r => r.queenMoved);
const leftHang  = results.filter(r => !r.queenMoved);

console.log(`Analyzed ${results.length} games (excl PawnOdds white)`);
console.log(`  Queen walked into capture: ${walkedIn.length}`);
console.log(`  Queen left hanging:        ${leftHang.length}`);

// ===== WALKED INTO CAPTURE: what was the queen targeting? =====
console.log('\n\n=== WALKED INTO CAPTURE — what was the queen going for? ===');
console.log('Format: opp  ply  Q moved from→to  captured piece  material context  then captured by\n');
for (const r of walkedIn) {
  const qFrom = r.qSqBefore || '??';
  const qTo   = r.qSqAfter || r.botIntentionMove.slice(2,4);
  const gained = r.capturedPiece !== '-' ? `took ${r.capturedPiece}(${r.capturedVal}pts)` : 'empty square(0)';
  const netStr = r.netSwap >= 0 ? `net +${r.netSwap}?` : `net ${r.netSwap}`;
  console.log(`  ${r.opp.slice(0,22).padEnd(24)} ply:${String(r.qLostPly).padEnd(4)} Q:${qFrom}→${qTo}  ${gained.padEnd(18)} ${netStr.padEnd(10)} byPiece:${r.captureByPiece}  matDiff:${r.matAfterQLoss}`);
  if (r.capturedPiece === '-' || r.capturedPiece === '.') {
    console.log(`    !! Queen moved to EMPTY square and was taken — FEN: ${r.fenAfterIntention.split(' ')[0]}`);
  }
}

// ===== LEFT HANGING: what did bot do instead? =====
console.log('\n\n=== LEFT HANGING — what move did bot make instead of saving queen? ===');
console.log('Format: opp  ply  what bot played  captured:  queen was on  then opp took  matDiff\n');

// Group by what bot captured
const captureGroups = {};
for (const r of leftHang) {
  const key = r.capturedPiece === '-' || r.capturedPiece === '.' ? 'non-capture' : r.capturedPiece.toLowerCase();
  if (!captureGroups[key]) captureGroups[key] = [];
  captureGroups[key].push(r);
}

console.log('What did bot capture (or do) instead of saving queen:');
Object.entries(captureGroups).sort((a,b)=>b[1].length-a[1].length).forEach(([k,v]) =>
  console.log(`  ${k.padEnd(15)} ${v.length} games  avg matDiff after: ${(v.reduce((s,r)=>s+r.matAfterQLoss,0)/v.length).toFixed(1)}`));

console.log('\nDetailed left-hanging samples:');
for (const r of leftHang) {
  const whatBot = r.capturedPiece && r.capturedPiece !== '-' && r.capturedPiece !== '.'
    ? `captured ${r.capturedPiece}(${r.capturedVal}pts) via ${r.botIntentionMove}`
    : `played ${r.botIntentionMove} (positional)`;
  console.log(`  ${r.opp.slice(0,22).padEnd(24)} ply:${String(r.qLostPly).padEnd(4)} Q@${r.qSqBefore}  bot:${whatBot.padEnd(30)} then opp:${r.captureMove}  matAfter:${r.matAfterQLoss}`);
}

// ===== OVERALL MATERIAL SWAP ANALYSIS =====
console.log('\n\n=== Material swap analysis: what did bot get for its queen? ===');
let totalGained = 0, count = 0;
const swapBuckets = {};
for (const r of results) {
  totalGained += r.capturedVal;
  count++;
  const bucketKey = r.capturedVal === 0 ? '0-nothing'
    : r.capturedVal === 1 ? '1-pawn'
    : r.capturedVal === 3 ? '3-minor'
    : r.capturedVal === 5 ? '5-rook'
    : r.capturedVal === 9 ? '9-queen'
    : `${r.capturedVal}-other`;
  swapBuckets[bucketKey] = (swapBuckets[bucketKey] || 0) + 1;
}
console.log(`Average material gained on move before queen lost: ${(totalGained/count).toFixed(1)} pts (queen = 9)`);
Object.entries(swapBuckets).sort().forEach(([k,v]) =>
  console.log(`  ${k.padEnd(15)} ${v} games`));

// Separate pawn-odds white games to show their specific sequence
console.log('\n\n=== SF-PawnOdds white opening trap (ply:12) — specific sequence ===');
const pawnOddsGames = losses.filter(g => g.opponent.includes('PawnOdds') && g.our_color === 'white');
console.log(`Count: ${pawnOddsGames.length} games — all play identical opening:`);
console.log('  1.e4 e6  2.d4 d5  3.exd5 exd5  4.Qh5!? g6  5.Qe5?? Qe7!  6.?? Qxe5');
console.log('  Bot plays early Qh5 vs missing f-pawn, retreats to e5, misses Qe7 threat');
console.log('  This is the same search-baked line played in every game');
