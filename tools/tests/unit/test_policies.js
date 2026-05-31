/**
 * Quick smoke test: verify getSearchProfile emergency mode produces correct values.
 */
const { getSearchProfile, TM } = require('../../../bot/src/policies');

function check(desc, cond) {
  const status = cond ? 'PASS' : 'FAIL';
  console.log(`  [${status}] ${desc}`);
  if (!cond) process.exitCode = 1;
}

console.log('=== Emergency mode tests ===\n');

// Test 1: Clock 1500ms, no increment → emergency
{
  const p = getSearchProfile('bullet', { wtime: 1500, btime: 1500, winc: 0, binc: 0 }, 'white', 40);
  console.log('Clock 1500ms, inc=0:', JSON.stringify(p));
  check('emergency=true', p.emergency === true);
  check('movetime is number', typeof p.movetime === 'number');
  check('movetime = max(20, floor(1500*0.05+0)) = 75', p.movetime === 75);
  check('maxTimeMs = movetime+50 = 125', p.maxTimeMs === 125);
  check('minTimeMs = 0', p.minTimeMs === 0);
}

console.log();

// Test 2: Clock 500ms, no increment → emergency
{
  const p = getSearchProfile('bullet', { wtime: 500, btime: 500, winc: 0, binc: 0 }, 'white', 60);
  console.log('Clock 500ms, inc=0:', JSON.stringify(p));
  check('emergency=true', p.emergency === true);
  check('movetime = max(20, min(25, 125, 500)) = 25', p.movetime === 25);
  check('maxTimeMs = 75', p.maxTimeMs === 75);
}

console.log();

// Test 3: Clock 100ms → extreme emergency
{
  const p = getSearchProfile('bullet', { wtime: 100, btime: 100, winc: 0, binc: 0 }, 'white', 80);
  console.log('Clock 100ms, inc=0:', JSON.stringify(p));
  check('emergency=true', p.emergency === true);
  check('movetime = max(20, min(5, 25, 500)) = 20 (floor)', p.movetime === 20);
}

console.log();

// Test 4: Clock 1800ms with 2000ms increment → emergency but generous movetime
{
  const p = getSearchProfile('blitz', { wtime: 1800, btime: 1800, winc: 2000, binc: 2000 }, 'white', 40);
  console.log('Clock 1800ms, inc=2000:', JSON.stringify(p));
  check('emergency=true', p.emergency === true);
  // base=90, incBonus=1000, min(1090, 450, 500) = 450
  check('movetime = min(1090, 450, 500) = 450', p.movetime === 450);
}

console.log();

// Test 5: Clock 3000ms → NOT emergency (normal mode)
{
  const p = getSearchProfile('blitz', { wtime: 3000, btime: 3000, winc: 0, binc: 0 }, 'white', 40);
  console.log('Clock 3000ms, inc=0:', JSON.stringify(p));
  check('emergency=false', p.emergency === false);
  check('movetime=null', p.movetime === null);
  check('minTimeMs > 0', p.minTimeMs > 0);
}

console.log();

// Test 6: Clock 60000ms (1+0 bullet start) → NOT emergency
{
  const p = getSearchProfile('bullet', { wtime: 60000, btime: 60000, winc: 0, binc: 0 }, 'white', 0);
  console.log('Clock 60000ms (1+0 start):', JSON.stringify(p));
  check('emergency=false', p.emergency === false);
  check('movetime=null', p.movetime === null);
  check('maxTimeMs reasonable', p.maxTimeMs > 500 && p.maxTimeMs < 10000);
}

console.log();

// Test 7: No clock data → normal mode, fallback
{
  const p = getSearchProfile('rapid', null, 'white', 0);
  console.log('No clock:', JSON.stringify(p));
  check('emergency=false', p.emergency === false);
  check('movetime=null', p.movetime === null);
}

console.log('\n=== Done ===');
