'use strict';

/**
 * pipeline_utils.js — Shared helper utilities for the data pipeline.
 *
 * This module extends utils.js with pipeline-specific helpers including
 * HTML cleaning and classification logic.
 */

const crypto = require('crypto');
const fs     = require('fs');
const path   = require('path');

const REPO_ROOT = path.resolve(__dirname, '..');

// ---------------------------------------------------------------------------
// Core helpers (from utils.js, kept for backward compatibility)
// ---------------------------------------------------------------------------

/**
 * Return a deterministic SHA-1 hex digest for a given source + url pair.
 */
function sha1Id(source, url) {
  return crypto.createHash('sha1').update(`${source}|${url}`).digest('hex');
}

/**
 * Return the current UTC time as an ISO 8601 string (no milliseconds).
 */
function nowIso() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
}

/**
 * Return today's UTC date as YYYY-MM-DD.
 */
function todayStr() {
  return new Date().toISOString().slice(0, 10);
}

/**
 * Create a directory (and all parents) if it does not already exist.
 */
function safeMkdir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
}

/**
 * Read a JSON Lines file and return an array of parsed objects.
 */
function readJsonl(filePath) {
  const content = fs.readFileSync(filePath, 'utf8');
  return content
    .split('\n')
    .map(l => l.trim())
    .filter(l => l.length > 0)
    .map(l => JSON.parse(l));
}

/**
 * Write an array of objects to a JSON Lines file.
 */
function writeJsonl(records, filePath) {
  const dir = path.dirname(filePath);
  if (dir) safeMkdir(dir);
  const content = records.map(r => JSON.stringify(r)).join('\n') + '\n';
  fs.writeFileSync(filePath, content, 'utf8');
}

/**
 * Write data to a pretty-printed JSON file (2-space indent).
 */
function writeJson(data, filePath) {
  const dir = path.dirname(filePath);
  if (dir) safeMkdir(dir);
  fs.writeFileSync(filePath, JSON.stringify(data, null, 2) + '\n', 'utf8');
}

/**
 * Try to parse a value into an ISO 8601 UTC string.
 */
function parseDateToIso(value) {
  if (!value) return null;
  try {
    const d = new Date(value);
    if (isNaN(d.getTime())) return null;
    return d.toISOString().replace(/\.\d{3}Z$/, 'Z');
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// HTML / text helpers
// ---------------------------------------------------------------------------

/**
 * Strip HTML tags and decode common HTML entities from text.
 */
function cleanHtml(text) {
  if (!text) return null;
  const stripped = text
    .replace(/<[^>]+>/g, ' ')
    .replace(/&[a-zA-Z#\d]+;/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return stripped || null;
}

// ---------------------------------------------------------------------------
// Classification
// ---------------------------------------------------------------------------

/**
 * Classify and write normalized records into separate files organized by
 * source, category, and date.
 *
 * Output structure under data/classified/:
 *   by-source/<source>/YYYY-MM-DD.jsonl
 *   by-category/<category>/YYYY-MM-DD.jsonl
 *   by-date/YYYY-MM-DD/<source>.jsonl
 *
 * @param {object[]} records  — normalized Opportunity records
 * @param {string}   source   — source name slug
 * @param {string}   today    — YYYY-MM-DD date string
 */
function classifyRecords(records, source, today) {
  const classifiedDir = path.join(REPO_ROOT, 'data', 'classified');

  // --- by-source ---
  const bySourceDir  = path.join(classifiedDir, 'by-source', source);
  const bySourcePath = path.join(bySourceDir, `${today}.jsonl`);
  writeJsonl(records, bySourcePath);
  console.log(`  ✓ Classified by-source → ${bySourcePath} (${records.length} records)`);

  // --- by-date ---
  const byDateDir  = path.join(classifiedDir, 'by-date', today);
  const byDatePath = path.join(byDateDir, `${source}.jsonl`);
  writeJsonl(records, byDatePath);
  console.log(`  ✓ Classified by-date → ${byDatePath} (${records.length} records)`);

  // --- by-category ---
  const categoryMap = new Map();
  for (const record of records) {
    const tags = record.tags || [];
    if (tags.length === 0) {
      // Records without tags go into "uncategorized"
      if (!categoryMap.has('uncategorized')) categoryMap.set('uncategorized', []);
      categoryMap.get('uncategorized').push(record);
    } else {
      for (const tag of tags) {
        const category = tag.toLowerCase().replace(/[^a-z0-9-]/g, '-').replace(/-+/g, '-');
        if (!categoryMap.has(category)) categoryMap.set(category, []);
        categoryMap.get(category).push(record);
      }
    }
  }

  for (const [category, catRecords] of categoryMap) {
    const byCatDir  = path.join(classifiedDir, 'by-category', category);
    const byCatPath = path.join(byCatDir, `${today}.jsonl`);

    // Append to existing file if other sources already wrote to this category today
    if (fs.existsSync(byCatPath)) {
      const existing = readJsonl(byCatPath);
      const merged   = [...existing, ...catRecords];
      writeJsonl(merged, byCatPath);
    } else {
      writeJsonl(catRecords, byCatPath);
    }
    console.log(`  ✓ Classified by-category/${category} → ${byCatPath} (${catRecords.length} records)`);
  }
}

module.exports = {
  sha1Id,
  nowIso,
  todayStr,
  safeMkdir,
  readJsonl,
  writeJsonl,
  writeJson,
  parseDateToIso,
  cleanHtml,
  classifyRecords,
};
