'use strict';

/**
 * fetch_rss.js — Download RSS/Atom feeds listed in data/sources/sources.json
 * and save raw snapshots under data/raw/<source>/.
 *
 * Usage:
 *   node scripts/fetch_rss.js
 */

const fs   = require('fs');
const path = require('path');
const axios = require('axios');

const SCRIPT_DIR = __dirname;
const REPO_ROOT  = path.resolve(SCRIPT_DIR, '..');

const { safeMkdir, todayStr } = require('./utils');

const SOURCES_FILE    = path.join(REPO_ROOT, 'data', 'sources', 'sources.json');
const RAW_DIR         = path.join(REPO_ROOT, 'data', 'raw');
const REQUEST_TIMEOUT = 30_000; // ms

// ---------------------------------------------------------------------------

function loadSources() {
  return JSON.parse(fs.readFileSync(SOURCES_FILE, 'utf8'));
}

/**
 * Download the feed URL and return raw bytes (Buffer).
 * @param {object} cfg  — source config object
 * @returns {Promise<Buffer>}
 */
async function fetchFeed(cfg) {
  console.log(`  → Fetching ${cfg.source_url} …`);
  const resp = await axios.get(cfg.source_url, {
    timeout:      REQUEST_TIMEOUT,
    responseType: 'arraybuffer',
    headers:      { 'User-Agent': 'YouthOpp-DataPipeline/1.0' },
  });
  return Buffer.from(resp.data);
}

/**
 * Save *content* to:
 *   data/raw/<source>/<today>.xml
 *   data/raw/<source>/latest.xml
 *
 * @param {string} source
 * @param {Buffer} content
 * @returns {{ dailyPath: string, latestPath: string }}
 */
function saveRaw(source, content) {
  const destDir    = path.join(RAW_DIR, source);
  safeMkdir(destDir);

  const today      = todayStr();
  const dailyPath  = path.join(destDir, `${today}.xml`);
  const latestPath = path.join(destDir, 'latest.xml');

  fs.writeFileSync(dailyPath,  content);
  fs.writeFileSync(latestPath, content);
  return { dailyPath, latestPath };
}

// ---------------------------------------------------------------------------

async function main() {
  const sources = loadSources();
  const enabled = sources.filter(s => s.enabled);

  console.log(`[fetch_rss] ${enabled.length} enabled source(s) found.`);

  let success = 0;
  let failed  = 0;

  for (const cfg of enabled) {
    const { source } = cfg;
    console.log(`\n[${source}]`);
    try {
      const content                 = await fetchFeed(cfg);
      const { dailyPath, latestPath } = saveRaw(source, content);
      const sizeKb                  = (content.length / 1024).toFixed(1);
      console.log(`  ✓ Saved ${sizeKb} KB → ${dailyPath}`);
      console.log(`  ✓ Updated ${latestPath}`);
      success++;
    } catch (err) {
      console.error(`  ✗ Error: ${err.message}`);
      failed++;
    }
  }

  console.log(`\n[fetch_rss] Done. ${success} succeeded, ${failed} failed.`);

  // Non-zero exit so CI can log failures without blocking downstream steps
  // (workflow uses continue-on-error: true on this step)
  if (failed > 0) process.exit(1);
}

main().catch(err => {
  console.error('[fetch_rss] Fatal:', err.message);
  process.exit(1);
});
