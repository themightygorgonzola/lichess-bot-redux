'use strict';

/**
 * botDb.js — Lightweight SQLite store for the auto-challenger.
 *
 * Two tables:
 *   bots — every bot we've ever discovered, with check_back timestamps
 *   log  — event audit trail
 *
 * That's it.  No queue, no accounts, no pending/active/resolved states.
 */

const path     = require('path');
const Database = require('better-sqlite3');

const DB_PATH = path.join(__dirname, '..', 'data', 'challenger.db');

let _db = null;

function db() {
  if (_db) return _db;
  require('fs').mkdirSync(path.dirname(DB_PATH), { recursive: true });
  _db = new Database(DB_PATH);
  _db.pragma('journal_mode = WAL');
  _migrate();
  return _db;
}

function _migrate() {
  db().exec(`
    CREATE TABLE IF NOT EXISTS bots (
      username        TEXT    NOT NULL,
      service         TEXT    NOT NULL DEFAULT 'lichess',
      elo_bullet      INTEGER,
      elo_blitz       INTEGER,
      elo_rapid       INTEGER,
      check_back      INTEGER DEFAULT 0,
      last_challenged INTEGER,
      last_outcome    TEXT,
      games_played    INTEGER DEFAULT 0,
      timeout_streak  INTEGER DEFAULT 0,
      decline_streak  INTEGER DEFAULT 0,
      selected        INTEGER DEFAULT 1,
      online          INTEGER DEFAULT 0,
      discovered_at   INTEGER,
      PRIMARY KEY (username, service)
    );

    CREATE TABLE IF NOT EXISTS log (
      id       INTEGER PRIMARY KEY AUTOINCREMENT,
      ts       INTEGER NOT NULL,
      event    TEXT    NOT NULL,
      username TEXT,
      detail   TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_bots_cb  ON bots(check_back);
    CREATE INDEX IF NOT EXISTS idx_log_ts   ON log(ts DESC);
  `);

  // ── Incremental migrations ──────────────────────────────────────────
  const cols = db().prepare("PRAGMA table_info(bots)").all().map(c => c.name);
  if (!cols.includes('timeout_streak')) {
    db().exec("ALTER TABLE bots ADD COLUMN timeout_streak INTEGER DEFAULT 0");
  }
  if (!cols.includes('decline_streak')) {
    db().exec("ALTER TABLE bots ADD COLUMN decline_streak INTEGER DEFAULT 0");
  }
  if (!cols.includes('selected')) {
    db().exec("ALTER TABLE bots ADD COLUMN selected INTEGER DEFAULT 1");
  }
}

/* ══════════════════════════════════════════════════════════════════════════
   Bots
   ══════════════════════════════════════════════════════════════════════════ */

/**
 * Bulk-sync online bots from a fetch.
 * Expects normalized bot objects: { username, service, ratings: { bullet, blitz, rapid } }
 * Marks fetched bots online with fresh elo, marks others offline.
 * Returns count of online bots after sync.
 */
function syncOnlineBots(bots, service = 'lichess') {
  const d = db();
  const now = Date.now();

  const upsert = d.prepare(`
    INSERT INTO bots (username, service, elo_bullet, elo_blitz, elo_rapid, online, discovered_at)
    VALUES (?, ?, ?, ?, ?, 1, ?)
    ON CONFLICT(username, service) DO UPDATE SET
      elo_bullet = excluded.elo_bullet,
      elo_blitz  = excluded.elo_blitz,
      elo_rapid  = excluded.elo_rapid,
      online     = 1
  `);

  d.prepare('UPDATE bots SET online = 0 WHERE service = ?').run(service);

  const tx = d.transaction(() => {
    for (const b of bots) {
      const name = b.username;
      if (!name) continue;
      upsert.run(
        name, service,
        b.ratings?.bullet ?? null,
        b.ratings?.blitz  ?? null,
        b.ratings?.rapid  ?? null,
        now,
      );
    }
  });
  tx();

  return d.prepare(
    'SELECT COUNT(*) as n FROM bots WHERE service = ? AND online = 1'
  ).get(service).n;
}

function getBot(username, service = 'lichess') {
  return db().prepare(
    'SELECT * FROM bots WHERE username = ? AND service = ?'
  ).get(username, service);
}

function getAllBots(service = 'lichess') {
  return db().prepare('SELECT * FROM bots WHERE service = ?').all(service);
}

function getReadyBots(service = 'lichess') {
  return db().prepare(
    'SELECT * FROM bots WHERE service = ? AND online = 1 AND selected = 1 AND check_back <= ?'
  ).all(service, Date.now());
}

function setCheckBack(username, service, ts) {
  db().prepare(
    'UPDATE bots SET check_back = ? WHERE username = ? AND service = ?'
  ).run(ts, username, service);
}

function setSelected(username, service, selected) {
  db().prepare(
    'UPDATE bots SET selected = ? WHERE username = ? AND service = ?'
  ).run(selected ? 1 : 0, username, service);
}

function recordChallenge(username, service, outcome) {
  db().prepare(
    'UPDATE bots SET last_challenged = ?, last_outcome = ? WHERE username = ? AND service = ?'
  ).run(Date.now(), outcome, username, service);
}

function bumpTimeoutStreak(username, service) {
  const row = db().prepare(
    'UPDATE bots SET timeout_streak = timeout_streak + 1 WHERE username = ? AND service = ? RETURNING timeout_streak'
  ).get(username, service);
  return row?.timeout_streak ?? 1;
}

function resetTimeoutStreak(username, service) {
  db().prepare(
    'UPDATE bots SET timeout_streak = 0 WHERE username = ? AND service = ?'
  ).run(username, service);
}

function bumpDeclineStreak(username, service) {
  const row = db().prepare(
    'UPDATE bots SET decline_streak = decline_streak + 1 WHERE username = ? AND service = ? RETURNING decline_streak'
  ).get(username, service);
  return row?.decline_streak ?? 1;
}

function resetDeclineStreak(username, service) {
  db().prepare(
    'UPDATE bots SET decline_streak = 0 WHERE username = ? AND service = ?'
  ).run(username, service);
}

function recordGame(username, service) {
  db().prepare(
    'UPDATE bots SET games_played = games_played + 1 WHERE username = ? AND service = ?'
  ).run(username, service);
}

/* ══════════════════════════════════════════════════════════════════════════
   Log
   ══════════════════════════════════════════════════════════════════════════ */

function log(event, username, detail) {
  const d = typeof detail === 'string' ? detail : JSON.stringify(detail ?? null);
  db().prepare(
    'INSERT INTO log (ts, event, username, detail) VALUES (?, ?, ?, ?)'
  ).run(Date.now(), event, username ?? null, d);
}

function getLog(limit = 60) {
  return db().prepare('SELECT * FROM log ORDER BY ts DESC LIMIT ?').all(limit);
}

/* ══════════════════════════════════════════════════════════════════════════
   Stats
   ══════════════════════════════════════════════════════════════════════════ */

function getStats(service = 'lichess') {
  const d   = db();
  const now = Date.now();
  const total   = d.prepare('SELECT COUNT(*) as n FROM bots WHERE service = ?').get(service).n;
  const online  = d.prepare('SELECT COUNT(*) as n FROM bots WHERE service = ? AND online = 1').get(service).n;
  const ready   = d.prepare('SELECT COUNT(*) as n FROM bots WHERE service = ? AND online = 1 AND check_back <= ?').get(service, now).n;
  const cooling = d.prepare('SELECT COUNT(*) as n FROM bots WHERE service = ? AND online = 1 AND check_back > ?').get(service, now).n;

  const dayStart = new Date();
  dayStart.setUTCHours(0, 0, 0, 0);
  const gamesToday = d.prepare(
    "SELECT COUNT(*) as n FROM log WHERE event = 'game_started' AND ts >= ?"
  ).get(dayStart.getTime()).n;

  return { total, online, ready, cooling, gamesToday };
}

/* ══════════════════════════════════════════════════════════════════════════
   Lifecycle
   ══════════════════════════════════════════════════════════════════════════ */

function purgeUnknownBots(service, keepUsernames) {
  if (!keepUsernames.length) return;
  const placeholders = keepUsernames.map(() => '?').join(',');
  db().prepare(
    `DELETE FROM bots WHERE service = ? AND username NOT IN (${placeholders})`
  ).run(service, ...keepUsernames);
}

function nuke() {
  db().exec('DELETE FROM bots; DELETE FROM log;');
}

function close() {
  if (_db) { _db.close(); _db = null; }
}

module.exports = {
  db,
  syncOnlineBots,
  getBot,
  getAllBots,
  getReadyBots,
  setCheckBack,
  setSelected,
  recordChallenge,
  recordGame,
  bumpTimeoutStreak,
  resetTimeoutStreak,
  bumpDeclineStreak,
  resetDeclineStreak,
  log,
  getLog,
  getStats,
  purgeUnknownBots,
  nuke,
  close,
};
