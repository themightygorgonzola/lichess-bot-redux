// What compensation was the bot aiming for when it "lost" its queen?
// Show: what the bot played the turn BEFORE the queen loss (its intention)
//        what the bot played the turn AFTER (what it thought it was getting)
//        what was the queen threatening when it walked into capture
//        what did the bot play instead when it left queen hanging

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
function score(m) { return Object.entries(m).reduce((s,[p,n])=>s+(PIECE_VAL[p]||0)*n,0); }
function fmt(m) { return Object.entries(m).map(([p,n])=>n+p).join('')||'(none)'; }

function queenSquare(board, isW) {
  for (let r = 0; r < 8; r++) for (let f = 0; f < 8; f++) {
    const pc = board[r][f];
    if (!pc) continue;
    const isQ = isW ? pc === 'Q' : pc === 'q';
    if (isQ) return 'abcdefgh'[f] + (8 - r);
  }
  return null;
}

// What squares does a queen on sq attack/reach?
function queenTargets(board, sq, isW) {
  const f = sq.charCodeAt(0) - 97;
  const r = 8 - parseInt(sq[1]);
  const dirs = [[0,1],[0,-1],[1,0],[-1,0],[1,1],[1,-1],[-1,1],[-1,-1]];
  const targets = [];
  for (const [dr,df] of dirs) {
    let cr=r+dr, cf=f+df;
    while(cr>=0&&cr<8&&cf>=0&&cf<8) {
      const pc = board[cr][cf];
      if (pc && pc !== '.') {
        targets.push({ sq:'abcdefgh'[cf]+(8-cr), piece:pc });
        break;
      }
      cr+=dr; cf+=df;
    }
  }
  return targets;
}

const games = queryGames({ limit: 500, offset: 0 });
const losses = games.filter(g => g.bot_result === 'loss' && g.ply_count > 20);

// Separate the pawn-odds ply:12 cluster from real games
const realLosses = losses.filter(g =>
  !g.opponent.includes('PawnOdds') &&
  !g.opponent.includes('AllQueens') &&
  !g.opponent.includes('KnightOdds') &&
  !g.opponent.includes('AllBishops')
);

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
  if (!botHadQ || qLostPly === null || qLostPly < 2) return null;

  const bdBefore = parseFen(fens[qLostPly - 1]).board;
  const qSq = queenSquare(bdBefore, botIsW);
  const qTargets = qSq ? queenTargets(bdBefore, qSq, botIsW) : [];

  // What the queen was attacking (enemy pieces in line of sight)
  const oppColor = botIsW ? 'lowercase' : 'uppercase';
  const attacked = qTargets.filter(t => {
    const pc = t.piece;
    return botIsW ? pc === pc.toLowerCase() : pc === pc.toUpperCase();
  });

  // Move before capture: bot's "intention" move (qLostPly-2 is bot's move)
  const botIntentionMove = qLostPly >= 2 ? moves[qLostPly - 2] : null;

  // Move that captured queen
  const captureMove = moves[qLostPly - 1];

  // Move AFTER queen captured: bot's response (qLostPly is bot's next move)
  const botResponseMove = qLostPly < moves.length ? moves[qLostPly] : null;

  // What the bot had AFTER queen loss
  const bdAfter = parseFen(fens[qLostPly]).board;
  const botAfterMat = matOf(bdAfter, botIsW);
  const oppAfterMat = matOf(bdAfter, !botIsW);
  const matDiff = score(botAfterMat) - score(oppAfterMat); // negative means behind

  // What response move targeted
  let botResponseTargets = null;
  if (botResponseMove && fens[qLostPly]) {
    try {
      const posAfter = parseFen(fens[qLostPly]);
      const posResp = applyMove(posAfter, botResponseMove);
      // just show the destination square
      botResponseTargets = botResponseMove;
    } catch(_) {}
  }

  // For LEFT_HANGING cases: what was bot's "intention" move doing instead of saving queen?
  // Detect: was it a capture? Was it a check?
  let intentionTag = '';
  if (botIntentionMove) {
    // If the destination has a piece, it's a capture
    if (fens[qLostPly - 2]) {
      try {
        const bdIntent = parseFen(fens[qLostPly - 2]).board;
        const toF = botIntentionMove.charCodeAt(2) - 97;
        const toR = 8 - parseInt(botIntentionMove[3]);
        const target = bdIntent[toR][toF];
        if (target && target !== '.') intentionTag = 'captures-' + target;
      } catch(_) {}
    }
  }

  return {
    id: g.id, opp: g.opponent, color: g.our_color, speed: g.speed,
    plies: g.ply_count, qLostPly,
    qSq, attacked: attacked.map(t => t.piece + '@' + t.sq),
    botIntentionMove, captureMove, botResponseMove,
    matDiff, // after queen loss
    botLeft: fmt(botAfterMat),
    intentionTag,
  };
}

console.log('\n=== REAL OPPONENT games — queen loss with compensation analysis ===\n');
const results = realLosses.map(analyzeGame).filter(Boolean);

// Group by what the queen was attacking at time of loss
const byAttack = {};
for (const r of results) {
  const atk = r.attacked.length > 0 ? r.attacked.join(',') : 'nothing';
  if (!byAttack[atk]) byAttack[atk] = [];
  byAttack[atk].push(r);
}

// Show attack groupings
console.log('What was the bot\'s queen attacking when it was captured?');
const sorted = Object.entries(byAttack).sort((a,b) => b[1].length - a[1].length);
for (const [atk, rows] of sorted.slice(0, 20)) {
  console.log(`  [${atk}] — ${rows.length} games`);
}

// Special: queen walking into capture — what was it targeting?
console.log('\n=== WALKED_INTO_CAPTURE cases — bot\'s last move before queen died ===');
const walked = results.filter(r => {
  // queen moved (botIntentionMove is a queen move — from queen's previous square)
  // We need to detect if the piece moving in botIntentionMove was the queen
  // Heuristic: if qSq matches the destination of botIntentionMove
  if (!r.botIntentionMove || !r.qSq) return false;
  const toF = r.botIntentionMove.charCodeAt(2) - 97;
  const toR = 8 - parseInt(r.botIntentionMove[3]);
  return 'abcdefgh'[toF] + (8 - toR) === r.qSq;
});

for (const r of walked) {
  const atk = r.attacked.length > 0 ? r.attacked.join(' ') : 'nothing';
  console.log(`  ${r.opp.slice(0,20).padEnd(22)} ply:${String(r.qLostPly).padEnd(4)} Q moved to ${r.qSq} attacking:[${atk}] then captured by ${r.captureMove}`);
  console.log(`    intention:${r.botIntentionMove} (${r.intentionTag||'dev'})  response after loss:${r.botResponseMove||'none'}  matDiff:${r.matDiff}`);
}

// What did the bot play immediately after losing the queen?
console.log('\n=== What did bot play AFTER queen was taken? (LEFT_HANGING) ===');
const hanging = results.filter(r => {
  if (!r.botIntentionMove || !r.qSq) return false;
  const toF = r.botIntentionMove.charCodeAt(2) - 97;
  const toR = 8 - parseInt(r.botIntentionMove[3]);
  return !('abcdefgh'[toF] + (8 - toR) === r.qSq);
});

// Count what the bot played instead of saving the queen
const insteadOf = {};
for (const r of hanging) {
  const tag = r.intentionTag || 'other-dev';
  insteadOf[tag] = (insteadOf[tag] || 0) + 1;
}
console.log('Bot\'s move that left queen hanging:');
Object.entries(insteadOf).sort((a,b)=>b[1]-a[1]).forEach(([k,v]) => console.log('  ' + k.padEnd(25), v));

// Show sample of hanging cases with what bot was doing
console.log('\nSample hanging cases — what did bot play instead of saving queen?');
for (const r of hanging.slice(0, 15)) {
  const atk = r.attacked.length > 0 ? r.attacked.join(' ') : 'nothing';
  console.log(`  ${r.opp.slice(0,20).padEnd(22)} ply:${String(r.qLostPly).padEnd(4)} Q@${r.qSq} attacked:[${atk}]`);
  console.log(`    instead played:${r.botIntentionMove}(${r.intentionTag||'dev'})  opp took:${r.captureMove}  bot responded:${r.botResponseMove||'?'}  matAfter:${r.matDiff}`);
}
