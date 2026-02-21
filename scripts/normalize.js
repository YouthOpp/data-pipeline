'use strict';

/**
 * normalize.js — Parse today's raw XML feed for each enabled source and write
 * normalized Opportunity records to data/normalized/opportunities/<today>.jsonl.
 *
 * Usage:
 *   node scripts/normalize.js
 */

const fs        = require('fs');
const path      = require('path');
const RssParser = require('rss-parser');

const SCRIPT_DIR = __dirname;
const REPO_ROOT  = path.resolve(SCRIPT_DIR, '..');

const { sha1Id, nowIso, todayStr, safeMkdir, writeJsonl } = require('./utils');

const SOURCES_FILE = path.join(REPO_ROOT, 'data', 'sources', 'sources.json');
const RAW_DIR      = path.join(REPO_ROOT, 'data', 'raw');
const NORM_DIR     = path.join(REPO_ROOT, 'data', 'normalized', 'opportunities');

// ---------------------------------------------------------------------------

function loadSources() {
  return JSON.parse(fs.readFileSync(SOURCES_FILE, 'utf8'));
}

/**
 * Strip HTML tags and decode common HTML entities from *text*.
 * @param {string|null|undefined} text
 * @returns {string|null}
 */
function cleanHtml(text) {
  if (!text) return null;
  const stripped = text
    .replace(/<[^>]+>/g, ' ')          // remove tags
    .replace(/&[a-zA-Z#\d]+;/g, ' ')  // remove entities (approx.)
    .replace(/\s+/g, ' ')
    .trim();
  return stripped || null;
}

/**
 * Return path to today's raw XML, falling back to latest.xml.
 * @param {string} source
 * @param {string} today   YYYY-MM-DD
 * @returns {string|null}
 */
function findRawFile(source, today) {
  const sourceDir = path.join(RAW_DIR, source);
  const daily     = path.join(sourceDir, `${today}.xml`);
  if (fs.existsSync(daily)) return daily;

  const latest = path.join(sourceDir, 'latest.xml');
  if (fs.existsSync(latest)) {
    console.log(`  ⚠ No daily file for ${today}, falling back to latest.xml`);
    return latest;
  }
  return null;
}

/**
 * Convert a single rss-parser item into a normalized Opportunity record.
 * @param {object} item   rss-parser feed item
 * @param {object} cfg    source config
 * @param {string} now    ISO timestamp string
 * @returns {object}
 */
function parseItem(item, cfg, now) {
  const source = cfg.source;
  const url    = item.link || '';
  const title  = item.title || '';

  // Summary — prefer full content, fall back to snippet
  const summaryRaw = item['content:encoded'] || item.content || item.contentSnippet || null;
  const summary    = cleanHtml(summaryRaw);

  // Date — rss-parser exposes isoDate when the feed has a valid date
  const publishedAt = item.isoDate
    ? new Date(item.isoDate).toISOString().replace(/\.\d{3}Z$/, 'Z')
    : null;

  // Tags: merge source defaults with feed categories
  const tags = [...(cfg.default_tags || [])];
  if (Array.isArray(item.categories)) {
    for (const cat of item.categories) {
      const term = typeof cat === 'string' ? cat : (cat && cat._ );
      if (term && !tags.includes(term)) tags.push(term);
    }
  }

  return {
    id:         sha1Id(source, url),
    title,
    url,
    source,
    source_url:   cfg.source_url,
    published_at: publishedAt,
    summary,
    tags,
    location:   null,
    deadline:   null,
    language:   cfg.language || null,
    created_at: now,
    updated_at: now,
  };
}

/**
 * Parse the raw feed for one source and return an array of Opportunity records.
 * @param {object} cfg
 * @param {string} today
 * @param {string} now
 * @returns {Promise<object[]>}
 */
async function normalizeSource(cfg, today, now) {
  const { source } = cfg;
  const rawPath = findRawFile(source, today);
  if (!rawPath) {
    console.log(`  ✗ No raw file found for source '${source}' — skipping.`);
    return [];
  }

  console.log(`  → Parsing ${rawPath} …`);
  const xmlContent = fs.readFileSync(rawPath, 'utf8');

  const parser = new RssParser({
    customFields: {
      item: [['content:encoded', 'content:encoded']],
    },
  });

  let feed;
  try {
    feed = await parser.parseString(xmlContent);
  } catch (err) {
    console.error(`  ✗ Feed parse error: ${err.message}`);
    return [];
  }

  const records = [];
  for (const item of feed.items || []) {
    try {
      const record = parseItem(item, cfg, now);
      if (record.url) records.push(record); // skip entries without a URL
    } catch (err) {
      console.error(`  ⚠ Skipping entry '${item.title || ''}' due to error: ${err.message}`);
    }
  }

  console.log(`  ✓ Parsed ${records.length} record(s) from ${source}`);
  return records;
}

// ---------------------------------------------------------------------------

async function main() {
  const sources = loadSources();
  const enabled = sources.filter(s => s.enabled);
  const today   = todayStr();
  const now     = nowIso();

  console.log(`[normalize] Date: ${today}  |  ${enabled.length} enabled source(s)`);

  const allRecords = [];

  for (const cfg of enabled) {
    console.log(`\n[${cfg.source}]`);
    try {
      const records = await normalizeSource(cfg, today, now);
      allRecords.push(...records);
    } catch (err) {
      console.error(`  ✗ Unexpected error for '${cfg.source}': ${err.message}`);
    }
  }

  if (allRecords.length > 0) {
    safeMkdir(NORM_DIR);
    const outPath = path.join(NORM_DIR, `${today}.jsonl`);
    writeJsonl(allRecords, outPath);
    console.log(`\n[normalize] Wrote ${allRecords.length} record(s) → ${outPath}`);
  } else {
    console.log('\n[normalize] No records to write.');
  }
}

main().catch(err => {
  console.error('[normalize] Fatal:', err.message);
  process.exit(1);
});
