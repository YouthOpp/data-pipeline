#!/usr/bin/env node
/**
 * dedupe_merge.js
 * Merges all normalized JSONL files from data/normalized/opportunities/ into
 * data/latest/opportunities.jsonl (all records) and
 * data/latest/opportunities.json (deduplicated array, keyed by link).
 */

const fs = require('fs');
const path = require('path');

const NORMALIZED_DIR = path.join(__dirname, '..', 'data', 'normalized', 'opportunities');
const LATEST_DIR = path.join(__dirname, '..', 'data', 'latest');

function main() {
  if (!fs.existsSync(NORMALIZED_DIR)) {
    console.log('No normalized data directory found.');
    return;
  }

  const jsonlFiles = fs.readdirSync(NORMALIZED_DIR)
    .filter((f) => f.endsWith('.jsonl'))
    .sort();

  if (jsonlFiles.length === 0) {
    console.log('No JSONL files to merge.');
    return;
  }

  const seen = new Map();

  for (const file of jsonlFiles) {
    const lines = fs.readFileSync(path.join(NORMALIZED_DIR, file), 'utf8')
      .split('\n')
      .filter(Boolean);
    for (const line of lines) {
      try {
        const record = JSON.parse(line);
        const key = record.link || record.title;
        if (key && !seen.has(key)) {
          seen.set(key, record);
        } else if (!key) {
          console.warn(`  Skipping record without link or title in ${file}`);
        }
      } catch {
        console.warn(`  Skipping malformed line in ${file}: ${line.slice(0, 80)}`);
      }
    }
  }

  fs.mkdirSync(LATEST_DIR, { recursive: true });

  const records = Array.from(seen.values());

  // Write JSONL
  const jsonlOut = path.join(LATEST_DIR, 'opportunities.jsonl');
  fs.writeFileSync(jsonlOut, records.map((r) => JSON.stringify(r)).join('\n') + (records.length ? '\n' : ''), 'utf8');

  // Write JSON array
  const jsonOut = path.join(LATEST_DIR, 'opportunities.json');
  fs.writeFileSync(jsonOut, JSON.stringify(records, null, 2) + '\n', 'utf8');

  console.log(`Merged ${records.length} unique records into ${LATEST_DIR}`);
}

main();
