#!/usr/bin/env node
/**
 * fetch_rss.js
 * Fetches RSS feeds from sources defined in data/sources/sources.json
 * and saves the raw XML responses to data/raw/<YYYY-MM-DD>/<source-id>.xml
 */

const fs = require('fs');
const path = require('path');
const https = require('https');
const http = require('http');

const SOURCES_FILE = path.join(__dirname, '..', 'data', 'sources', 'sources.json');
const RAW_DIR = path.join(__dirname, '..', 'data', 'raw');

function today() {
  return new Date().toISOString().slice(0, 10);
}

function fetch(url, redirects = 5) {
  return new Promise((resolve, reject) => {
    if (redirects === 0) { reject(new Error('Too many redirects')); return; }
    const lib = url.startsWith('https') ? https : http;
    lib.get(url, (res) => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        res.resume();
        resolve(fetch(res.headers.location, redirects - 1));
        return;
      }
      let data = '';
      res.on('data', (chunk) => { data += chunk; });
      res.on('end', () => resolve(data));
    }).on('error', reject);
  });
}

async function main() {
  const sources = JSON.parse(fs.readFileSync(SOURCES_FILE, 'utf8'));
  const dateDir = path.join(RAW_DIR, today());
  fs.mkdirSync(dateDir, { recursive: true });

  for (const source of sources) {
    if (!source.enabled) continue;
    console.log(`Fetching ${source.name} (${source.url})...`);
    try {
      const xml = await fetch(source.url);
      const outFile = path.join(dateDir, `${source.id}.xml`);
      fs.writeFileSync(outFile, xml, 'utf8');
      console.log(`  Saved to ${outFile}`);
    } catch (err) {
      console.error(`  Failed to fetch ${source.id}: ${err.message}`);
    }
  }
}

main().catch((err) => { console.error(err); process.exit(1); });
