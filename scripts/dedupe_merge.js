'use strict';

/**
 * dedupe_merge.js — Combine all normalized JSONL files, deduplicate by `id`
 * (last-write wins), sort by published_at descending (nulls last), and write
 * the final dataset to data/latest/.
 *
 * Usage:
 *   node scripts/dedupe_merge.js
 */

const fs   = require('fs');
const path = require('path');

const SCRIPT_DIR = __dirname;
const REPO_ROOT  = path.resolve(SCRIPT_DIR, '..');

const { nowIso, readJsonl, writeJson, writeJsonl } = require('./utils');

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

function main() {
  // Collect all .jsonl files from the normalized directory, sorted by name
  let jsonlFiles = [];
  try {
    jsonlFiles = fs
      .readdirSync(NORM_DIR)
      .filter(f => f.endsWith('.jsonl'))
      .sort()
      .map(f => path.join(NORM_DIR, f));
  } catch {
    // Directory may not exist yet on a fresh repo — that's fine
  }

  console.log(`[dedupe_merge] Found ${jsonlFiles.length} normalized file(s).`);

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
