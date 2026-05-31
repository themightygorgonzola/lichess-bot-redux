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

const games = queryGames({ limit: 500, offset: 0 });
const losses = games.filter(g => g.bot_result === 'loss' && g.ply_count > 20);

// Identify the queen's file and rank from the FEN board
function queenSquare(board, isW) {
  for (let r = 0; r < 8; r++) for (let f = 0; f < 8; f++) {
    const pc = board[r][f];
    if (!pc) continue;
    const isQ = isW ? pc === 'Q' : pc === 'q';
    if (isQ) return { r, f, sq: 'abcdefgh'[f] + (8 - r) };
  }
  return null;
}

// Classify what the queen was doing the move it got captured
// by looking at the previous move made by the BOT (it moved somewhere and it got taken)
// OR the opponent moved to the queen's square
function classify(fens, moves, qLostPly, botIsW) {
  // The queen disappears at fens[qLostPly] after move moves[qLostPly-1]
  // The move that captured the queen is moves[qLostPly-1]
  const captureMoveIdx = qLostPly - 1;
  const captureMove = moves[captureMoveIdx]; // opponent's move that took the queen

  let bdBefore;
  try { bdBefore = parseFen(fens[qLostPly - 1]).board; } catch(_) { return 'err'; }
  const qSq = queenSquare(bdBefore, botIsW);
  if (!qSq) return 'err';

  // Where did the queen go the move BEFORE it was captured (bot's last move)?
  // Find the last bot move (2 plies before capture)
  const botLastMoveIdx = qLostPly - 2;
  const botLastMove = botLastMoveIdx >= 0 ? moves[botLastMoveIdx] : null;

  // Did the bot just move its queen?
  let queenJustMoved = false;
  if (botLastMoveIdx >= 0 && fens[botLastMoveIdx]) {
    let bdTwoBefore;
    try { bdTwoBefore = parseFen(fens[botLastMoveIdx]).board; } catch(_) {}
    if (bdTwoBefore) {
      const qBefore2 = queenSquare(bdTwoBefore, botIsW);
      // If queen's square changed, bot moved it
      if (qBefore2 && qSq && qBefore2.sq !== qSq.sq) queenJustMoved = true;
    }
  }

  // Was the queen en prise for multiple moves? Check 2 moves ago
  let queenWasAlreadyHanging = false;
  if (qLostPly >= 4 && fens[qLostPly - 3]) {
    let bdEarlier;
    try { bdEarlier = parseFen(fens[qLostPly - 3]).board; } catch(_) {}
    if (bdEarlier) {
      const qEarlier = queenSquare(bdEarlier, botIsW);
      // If queen was already on same square 2 plies ago (before bot's last move),
      // the bot moved something else and left queen hanging
      if (qEarlier && qEarlier.sq === qSq.sq) queenWasAlreadyHanging = true;
    }
  }

  return {
    queenSq: qSq.sq,
    captureMove,
    queenJustMoved,
    queenWasAlreadyHanging,
    botLastMove,
    tag: queenJustMoved ? 'WALKED_INTO_CAPTURE' : (queenWasAlreadyHanging ? 'LEFT_HANGING_2PLY' : 'HUNG_1PLY')
  };
}

const tagCounts = {};
const samples = { WALKED_INTO_CAPTURE: [], LEFT_HANGING_2PLY: [], HUNG_1PLY: [] };

// Also separate by ply bucket (ply:12 are OpeningTrap suspects)
const plyBuckets = { ply12: 0, early13_30: 0, mid31_60: 0, late61plus: 0 };

let processed = 0;
for (const g of losses) {
  const raw = getGame(g.id);
  if (!raw || !raw.full_moves || !raw.full_moves.length) continue;
  const startFen = raw.initial_fen || 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
  const botIsW = g.our_color === 'white';
  let pos;
  try { pos = parseFen(startFen); } catch(_) { continue; }
  const fens = [startFen];
  const moves = raw.full_moves;
  for (const uci of moves) {
    try { pos = applyMove(pos, uci); fens.push(posToFen(pos)); } catch(_) { break; }
  }

  let botHadQ = false, qLostPly = null;
  for (let i = 0; i < fens.length; i++) {
    let bd;
    try { bd = parseFen(fens[i]).board; } catch(_) { break; }
    const bm = matOf(bd, botIsW);
    if (bm.q > 0) botHadQ = true;
    if (botHadQ && !bm.q && qLostPly === null) { qLostPly = i; break; }
  }
  if (qLostPly === null || qLostPly < 2) continue;
  processed++;

  // Ply bucket
  if (qLostPly <= 12) plyBuckets.ply12++;
  else if (qLostPly <= 30) plyBuckets.early13_30++;
  else if (qLostPly <= 60) plyBuckets.mid31_60++;
  else plyBuckets.late61plus++;

  const info = classify(fens, moves, qLostPly, botIsW);
  tagCounts[info.tag] = (tagCounts[info.tag] || 0) + 1;

  if (samples[info.tag] && samples[info.tag].length < 6) {
    samples[info.tag].push({
      opp: g.opponent, color: g.our_color, qLostPly,
      queenSq: info.queenSq,
      botLastMove: info.botLastMove,
      captureMove: info.captureMove,
      fenBefore: fens[qLostPly - 1],
    });
  }
}

console.log(`\nProcessed ${processed} games\n`);

console.log('=== Queen loss ply distribution ===');
console.log('  ply<=12 (opening trap?):  ', plyBuckets.ply12);
console.log('  ply 13-30 (early middle): ', plyBuckets.early13_30);
console.log('  ply 31-60 (middlegame):   ', plyBuckets.mid31_60);
console.log('  ply 61+   (late):         ', plyBuckets.late61plus);

console.log('\n=== How queen was hanging ===');
Object.entries(tagCounts).sort((a,b)=>b[1]-a[1]).forEach(([k,v]) => console.log('  ' + k.padEnd(25), v));

for (const [tag, list] of Object.entries(samples)) {
  console.log(`\n=== Sample: ${tag} ===`);
  for (const s of list) {
    console.log(`  ${s.opp} (${s.color}) ply:${s.qLostPly} Q@${s.queenSq}`);
    console.log(`    bot's last move: ${s.botLastMove}  opp captured: ${s.captureMove}`);
    console.log(`    FEN before: ${s.fenBefore}`);
  }
}

// --- Special focus: ply:12 pattern for SF-PawnOdds ---
console.log('\n=== SF-PawnOdds ply:12 cluster — first 3 game move sequences ===');
let sfPawnPly12 = 0;
for (const g of losses) {
  if (!g.opponent.includes('PawnOdds')) continue;
  const raw = getGame(g.id);
  if (!raw || !raw.full_moves) continue;
  const startFen = raw.initial_fen || 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
  const botIsW = g.our_color === 'white';
  let pos;
  try { pos = parseFen(startFen); } catch(_) { continue; }
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
  if (qLostPly === null || qLostPly > 14) continue;
  if (sfPawnPly12++ >= 3) continue;

  console.log(`\n  ${g.opponent} ${g.our_color} ply:${qLostPly}`);
  console.log('  Moves:', moves.slice(0, qLostPly).join(' '));
  console.log('  Start FEN:', startFen.split(' ')[0]);
}
