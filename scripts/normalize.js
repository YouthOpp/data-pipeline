#!/usr/bin/env node
/**
 * normalize.js
 * Parses raw XML files from data/raw/<date>/ and writes normalized JSONL
 * to data/normalized/opportunities/<date>.jsonl
 */

const fs = require('fs');
const path = require('path');

const RAW_DIR = path.join(__dirname, '..', 'data', 'raw');
const NORMALIZED_DIR = path.join(__dirname, '..', 'data', 'normalized', 'opportunities');

function today() {
  return new Date().toISOString().slice(0, 10);
}

/**
 * Minimal XML tag extractor — pulls all <item> blocks from an RSS feed
 * and maps common RSS fields to a normalized opportunity object.
 */
function parseItems(xml) {
  const items = [];
  const itemRegex = /<item>([\s\S]*?)<\/item>/g;
  let match;
  while ((match = itemRegex.exec(xml)) !== null) {
    const block = match[1];
    const get = (tag) => {
      const cdata = block.match(new RegExp(`<${tag}[^>]*><!\\[CDATA\\[([\\s\\S]*?)\\]\\]><\\/${tag}>`));
      if (cdata) return cdata[1].trim();
      const plain = block.match(new RegExp(`<${tag}[^>]*>([\\s\\S]*?)<\\/${tag}>`));
      return plain ? plain[1].trim() : '';
    };
    items.push({
      title: get('title'),
      link: get('link'),
      description: get('description'),
      pubDate: get('pubDate'),
    });
  }
  return items;
}

function main() {
  const date = today();
  const dateDir = path.join(RAW_DIR, date);

  if (!fs.existsSync(dateDir)) {
    console.log(`No raw data directory found for ${date}: ${dateDir}`);
    return;
  }

  const xmlFiles = fs.readdirSync(dateDir).filter((f) => f.endsWith('.xml'));
  if (xmlFiles.length === 0) {
    console.log('No XML files to normalize.');
    return;
  }

  fs.mkdirSync(NORMALIZED_DIR, { recursive: true });
  const outFile = path.join(NORMALIZED_DIR, `${date}.jsonl`);
  const lines = [];

  for (const file of xmlFiles) {
    const sourceId = path.basename(file, '.xml');
    const xml = fs.readFileSync(path.join(dateDir, file), 'utf8');
    const items = parseItems(xml);
    for (const item of items) {
      lines.push(JSON.stringify({ sourceId, ...item }));
    }
    console.log(`  ${sourceId}: ${items.length} items`);
  }

  fs.writeFileSync(outFile, lines.join('\n') + (lines.length ? '\n' : ''), 'utf8');
  console.log(`Wrote ${lines.length} records to ${outFile}`);
}

main();
