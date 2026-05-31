// Quick integration test: Exercise the bot's Engine class exactly like game.js does.
// Uses go infinite + stop (thinkDynamic), which is the real bot code path.
'use strict';
require('dotenv').config();

const { Engine } = require('./src/engine');
const ENGINE_PATH = process.env.ENGINE_PATH ?? './engine/lichess-bot.exe';

console.log(`ENGINE_PATH: ${ENGINE_PATH}`);

async function main() {
  const eng = new Engine(ENGINE_PATH, { threads: 8, hash: 512, useNnue: false });
  await eng.init();
  console.log('Engine ready');

  // Simulate a real game move: startpos, think for max 5s
  const result = await eng.thinkDynamic('startpos', [], {
    onInfo: (info) => {
      if (info.depth) process.stdout.write(`  depth=${info.depth} score=${info.score_cp ?? '?'}\n`);
    },
    maxTimeMs: 5000,
  });

  console.log(`\nResult: move=${result.move} ponder=${result.ponderMove ?? 'none'} depth=${result.depth} eval=${result.eval_cp ?? '?'} time=${result.time_ms}ms`);

  // Check the result is sane
  if (!result.move || result.move.length < 4) {
    console.error('FAIL: no valid move');
    process.exit(1);
  }

  // Now test a mid-game position
  const result2 = await eng.thinkDynamic('startpos', ['e2e4', 'e7e5', 'g1f3', 'b8c6'], {
    onInfo: (info) => {
      if (info.depth) process.stdout.write(`  depth=${info.depth} score=${info.score_cp ?? '?'}\n`);
    },
    maxTimeMs: 3000,
  });
  console.log(`Result2: move=${result2.move} depth=${result2.depth} eval=${result2.eval_cp ?? '?'} time=${result2.time_ms}ms`);

  await eng.quit();
  console.log('PASS');
  process.exit(0);
}

main().catch(err => {
  console.error('FAIL:', err.message);
  process.exit(1);
});
