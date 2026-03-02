'use strict';

/**
 * dedupe_merge.js — Combine all classified JSONL files (by-source), deduplicate
 * by `id` (last-write wins), sort by published_at descending (nulls last), and
 * write the final dataset to data/latest/.
 *
 * Falls back to data/normalized/opportunities/ if classified data is not yet
 * available (backward compatibility).
 *
 * Usage:
 *   node scripts/dedupe_merge.js
 */

const fs   = require('fs');
const path = require('path');

const SCRIPT_DIR = __dirname;
const REPO_ROOT  = path.resolve(SCRIPT_DIR, '..');

const { nowIso, readJsonl, writeJson, writeJsonl } = require('./utils');

const CLASSIFIED_BY_SOURCE = path.join(REPO_ROOT, 'data', 'classified', 'by-source');
const NORM_DIR    = path.join(REPO_ROOT, 'data', 'normalized', 'opportunities');
const LATEST_DIR  = path.join(REPO_ROOT, 'data', 'latest');
const LATEST_JSON = path.join(LATEST_DIR, 'opportunities.json');
const LATEST_JSONL= path.join(LATEST_DIR, 'opportunities.jsonl');

// ---------------------------------------------------------------------------

/**
 * Sort key: real dates sort normally; null/missing dates are treated as the
 * earliest possible date so they end up last after a descending sort.
 * @param {object} record
 * @returns {string}
 */
function sortKey(record) {
  return record.published_at || '0000-00-00T00:00:00Z';
}

// ---------------------------------------------------------------------------

/**
 * Recursively collect all .jsonl files under a directory.
 */
function collectJsonlFiles(dir) {
  const files = [];
  if (!fs.existsSync(dir)) return files;

  const entries = fs.readdirSync(dir, { withFileTypes: true });
  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...collectJsonlFiles(fullPath));
    } else if (entry.name.endsWith('.jsonl')) {
      files.push(fullPath);
    }
  }
  return files.sort();
}

function main() {
  // Prefer classified data; fall back to legacy normalized directory
  let jsonlFiles = collectJsonlFiles(CLASSIFIED_BY_SOURCE);
  let dataSource = 'classified/by-source';

  if (jsonlFiles.length === 0) {
    jsonlFiles = collectJsonlFiles(NORM_DIR);
    dataSource = 'normalized/opportunities';
  }

  console.log(`[dedupe_merge] Found ${jsonlFiles.length} file(s) from ${dataSource}.`);

  // --- Read & deduplicate (last-write wins) --------------------------------
  const merged   = new Map();
  let totalRead  = 0;

  for (const filePath of jsonlFiles) {
    try {
      const records = readJsonl(filePath);
      for (const record of records) {
        if (record.id) {
          merged.set(record.id, record);
          totalRead++;
        }
      }
    } catch (err) {
      console.error(`  ⚠ Could not read ${filePath}: ${err.message}`);
    }
  }

  console.log(
    `[dedupe_merge] Read ${totalRead} raw record(s), ` +
    `${merged.size} unique after dedup.`
  );

  // --- Sort: published_at descending, nulls last ---------------------------
  const records = [...merged.values()].sort((a, b) => {
    const ka = sortKey(a);
    const kb = sortKey(b);
    if (ka < kb) return  1;
    if (ka > kb) return -1;
    return 0;
  });

  // --- Write output ---------------------------------------------------------
  writeJson(records,  LATEST_JSON);
  writeJsonl(records, LATEST_JSONL);

  const now = nowIso();
  console.log(`[dedupe_merge] Wrote ${records.length} record(s) → ${LATEST_JSON}`);
  console.log(`[dedupe_merge] Wrote ${records.length} record(s) → ${LATEST_JSONL}`);
  console.log(`[dedupe_merge] Done at ${now}.`);
}

main();
