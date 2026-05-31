'use strict';

/**
 * positionStore.js — CRUD layer for the positions / categories library.
 *
 * Data lives in data/positions.json at the workspace root.
 * Schema:
 *   { categories: [ { id, name, locked, positions: [ { id, name, fen } ] } ] }
 *
 * Locked categories (e.g. Start Position) cannot be deleted and their
 * positions cannot be deleted — only new positions can be added to them
 * if you choose, but the shipped locked positions themselves are immutable.
 */

const fs   = require('fs');
const path = require('path');

const DATA_DIR  = path.join(__dirname, '..', 'data');
const DATA_PATH = path.join(DATA_DIR, 'positions.json');
fs.mkdirSync(DATA_DIR, { recursive: true });

let _data = null;

function _load() {
  if (_data) return _data;
  try {
    _data = JSON.parse(fs.readFileSync(DATA_PATH, 'utf8'));
  } catch (e) {
    console.error('[positionStore] failed to load positions.json:', e.message);
    _data = { categories: [] };
  }
  return _data;
}

function _save() {
  try {
    fs.writeFileSync(DATA_PATH, JSON.stringify(_data, null, 2), 'utf8');
  } catch (e) {
    console.error('[positionStore] failed to save positions.json:', e.message);
  }
}

function _uniqueId(base, existingIds) {
  let i = 0;
  while (existingIds.has(`${base}-${i}`)) i++;
  return `${base}-${i}`;
}

// ── Read ──────────────────────────────────────────────────────────────────────

/** Return all categories (deep copy). */
function getCategories() {
  return JSON.parse(JSON.stringify(_load().categories));
}

/** Return a flat Map<posId, fen> for all positions across all categories. */
function getAllPositionMap() {
  const map = new Map();
  for (const cat of _load().categories) {
    for (const pos of cat.positions) {
      map.set(pos.id, pos.fen);
    }
  }
  return map;
}

// ── Category CRUD ─────────────────────────────────────────────────────────────

/** Create a new (non-locked) category. Returns the new category object. */
function createCategory(name) {
  const data = _load();
  const existingIds = new Set(data.categories.map(c => c.id));
  const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'category';
  const id = _uniqueId(slug, existingIds);
  const cat = { id, name, locked: false, positions: [] };
  data.categories.push(cat);
  _save();
  return { ok: true, category: cat };
}

/** Delete a non-locked category by id. */
function deleteCategory(catId) {
  const data = _load();
  const cat = data.categories.find(c => c.id === catId);
  if (!cat) return { ok: false, error: 'Category not found' };
  if (cat.locked) return { ok: false, error: 'Cannot delete a locked category' };
  data.categories = data.categories.filter(c => c.id !== catId);
  _save();
  return { ok: true };
}

// ── Position CRUD ─────────────────────────────────────────────────────────────

/** Add a position to a category. Returns the new position object. */
function addPosition(catId, name, fen) {
  const data = _load();
  const cat = data.categories.find(c => c.id === catId);
  if (!cat) return { ok: false, error: 'Category not found' };
  const allIds = new Set(data.categories.flatMap(c => c.positions.map(p => p.id)));
  const id = _uniqueId(catId, allIds);
  const pos = { id, name, fen };
  cat.positions.push(pos);
  _save();
  return { ok: true, position: pos };
}

/** Update name and/or fen of a position. */
function updatePosition(catId, posId, updates) {
  const data = _load();
  const cat = data.categories.find(c => c.id === catId);
  if (!cat) return { ok: false, error: 'Category not found' };
  const pos = cat.positions.find(p => p.id === posId);
  if (!pos) return { ok: false, error: 'Position not found' };
  if (updates.name !== undefined) pos.name = String(updates.name);
  if (updates.fen  !== undefined) pos.fen  = String(updates.fen);
  _save();
  return { ok: true, position: { ...pos } };
}

/** Delete a position. Locked categories block deletion. */
function deletePosition(catId, posId) {
  const data = _load();
  const cat = data.categories.find(c => c.id === catId);
  if (!cat) return { ok: false, error: 'Category not found' };
  if (cat.locked) return { ok: false, error: 'Cannot delete from a locked category' };
  const before = cat.positions.length;
  cat.positions = cat.positions.filter(p => p.id !== posId);
  if (cat.positions.length === before) return { ok: false, error: 'Position not found' };
  _save();
  return { ok: true };
}

module.exports = {
  getCategories,
  getAllPositionMap,
  createCategory,
  deleteCategory,
  addPosition,
  updatePosition,
  deletePosition,
};
