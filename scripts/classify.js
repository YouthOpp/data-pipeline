'use strict';

/**
 * classify.js — Re-classify all normalized JSONL data into the classified
 * directory structure (by-source, by-category, by-date).
 *
 * This script reads from data/normalized/opportunities/*.jsonl and writes
 * classified output. Useful for re-classifying historical data.
 *
 * Usage:
 *   node scripts/classify.js
 */

const fs   = require('fs');
const path = require('path');

const SCRIPT_DIR = __dirname;
const REPO_ROOT  = path.resolve(SCRIPT_DIR, '..');

const { readJsonl, classifyRecords } = require('./pipeline_utils');

const NORM_DIR       = path.join(REPO_ROOT, 'data', 'normalized', 'opportunities');
const CLASSIFIED_DIR = path.join(REPO_ROOT, 'data', 'classified');

// ---------------------------------------------------------------------------

function main() {
  // Collect all .jsonl files from the normalized directory
  let jsonlFiles = [];
  try {
    jsonlFiles = fs
      .readdirSync(NORM_DIR)
      .filter(f => f.endsWith('.jsonl'))
      .sort()
      .map(f => path.join(NORM_DIR, f));
  } catch {
    console.log('[classify] No normalized directory found — nothing to classify.');
    return;
  }

  console.log(`[classify] Found ${jsonlFiles.length} normalized file(s) to classify.`);

  // Clean the classified directory to avoid stale data
  if (fs.existsSync(CLASSIFIED_DIR)) {
    fs.rmSync(CLASSIFIED_DIR, { recursive: true, force: true });
  }

  let totalRecords = 0;

  for (const filePath of jsonlFiles) {
    const basename = path.basename(filePath, '.jsonl'); // YYYY-MM-DD
    console.log(`\n[classify] Processing ${basename} …`);

    try {
      const records = readJsonl(filePath);
      if (records.length === 0) continue;

      // Group records by source
      const bySource = new Map();
      for (const record of records) {
        const src = record.source || 'unknown';
        if (!bySource.has(src)) bySource.set(src, []);
        bySource.get(src).push(record);
      }

      // Classify each source group
      for (const [source, sourceRecords] of bySource) {
        classifyRecords(sourceRecords, source, basename);
        totalRecords += sourceRecords.length;
      }
    } catch (err) {
      console.error(`  ⚠ Error processing ${filePath}: ${err.message}`);
    }
  }

  console.log(`\n[classify] Done. Classified ${totalRecords} total record(s).`);
}

main();
