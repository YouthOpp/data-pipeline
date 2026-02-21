'use strict';

/**
 * utils.js — Shared helper utilities for the data pipeline.
 */

const crypto = require('crypto');
const fs     = require('fs');
const path   = require('path');

/**
 * Return a deterministic SHA-1 hex digest for a given source + url pair.
 * @param {string} source
 * @param {string} url
 * @returns {string}
 */
function sha1Id(source, url) {
  return crypto.createHash('sha1').update(`${source}|${url}`).digest('hex');
}

/**
 * Return the current UTC time as an ISO 8601 string (no milliseconds).
 * @returns {string}  e.g. "2026-02-21T10:00:00Z"
 */
function nowIso() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
}

/**
 * Return today's UTC date as YYYY-MM-DD.
 * @returns {string}
 */
function todayStr() {
  return new Date().toISOString().slice(0, 10);
}

/**
 * Create a directory (and all parents) if it does not already exist.
 * @param {string} dirPath
 */
function safeMkdir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
}

/**
 * Read a JSON Lines file and return an array of parsed objects.
 * Blank lines are silently skipped.
 * @param {string} filePath
 * @returns {object[]}
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
 * Write an array of objects to a JSON Lines file (one JSON object per line).
 * Parent directory is created automatically.
 * @param {object[]} records
 * @param {string}   filePath
 */
function writeJsonl(records, filePath) {
  const dir = path.dirname(filePath);
  if (dir) safeMkdir(dir);
  const content = records.map(r => JSON.stringify(r)).join('\n') + '\n';
  fs.writeFileSync(filePath, content, 'utf8');
}

/**
 * Write data to a pretty-printed JSON file (2-space indent).
 * Parent directory is created automatically.
 * @param {*}      data
 * @param {string} filePath
 */
function writeJson(data, filePath) {
  const dir = path.dirname(filePath);
  if (dir) safeMkdir(dir);
  fs.writeFileSync(filePath, JSON.stringify(data, null, 2) + '\n', 'utf8');
}

/**
 * Try to parse *value* into an ISO 8601 UTC string (YYYY-MM-DDTHH:MM:SSZ).
 * Returns null if the value is empty or cannot be parsed.
 * @param {string|null|undefined} value
 * @returns {string|null}
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

module.exports = {
  sha1Id,
  nowIso,
  todayStr,
  safeMkdir,
  readJsonl,
  writeJsonl,
  writeJson,
  parseDateToIso,
};
