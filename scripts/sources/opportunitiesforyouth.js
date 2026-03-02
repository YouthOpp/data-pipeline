'use strict';

/**
 * opportunitiesforyouth.js — Source-specific pipeline for opportunitiesforyouth.org
 *
 * Fetches the RSS feed, normalizes entries, classifies by date/source/category,
 * and writes the output. Raw data is treated as temporary and is not persisted.
 *
 * Usage:
 *   node scripts/sources/opportunitiesforyouth.js
 */

const fs        = require('fs');
const path      = require('path');
const axios     = require('axios');
const RssParser = require('rss-parser');

const SCRIPT_DIR = __dirname;
const REPO_ROOT  = path.resolve(SCRIPT_DIR, '..', '..');

const {
  sha1Id, nowIso, todayStr, safeMkdir,
  writeJsonl, cleanHtml, parseDateToIso,
  classifyRecords,
} = require('../pipeline_utils');

// ---------------------------------------------------------------------------
// Source configuration
// ---------------------------------------------------------------------------

const SOURCE_NAME = 'opportunitiesforyouth';
const SOURCE_URL  = 'https://opportunitiesforyouth.org/feed/';
const LANGUAGE    = 'en';
const DEFAULT_TAGS = ['education'];
const REQUEST_TIMEOUT = 30_000; // ms

// ---------------------------------------------------------------------------
// Fetch
// ---------------------------------------------------------------------------

async function fetchFeed() {
  console.log(`  → Fetching ${SOURCE_URL} …`);
  const resp = await axios.get(SOURCE_URL, {
    timeout:      REQUEST_TIMEOUT,
    responseType: 'arraybuffer',
    headers:      { 'User-Agent': 'YouthOpp-DataPipeline/1.0' },
  });
  return Buffer.from(resp.data).toString('utf8');
}

// ---------------------------------------------------------------------------
// Normalize
// ---------------------------------------------------------------------------

function parseItem(item, now) {
  const url   = item.link || '';
  const title = item.title || '';

  const summaryRaw = item['content:encoded'] || item.content || item.contentSnippet || null;
  const summary    = cleanHtml(summaryRaw);

  const publishedAt = item.isoDate
    ? new Date(item.isoDate).toISOString().replace(/\.\d{3}Z$/, 'Z')
    : null;

  // Tags: merge defaults with feed categories
  const tags = [...DEFAULT_TAGS];
  if (Array.isArray(item.categories)) {
    for (const cat of item.categories) {
      const term = typeof cat === 'string' ? cat : (cat && cat._);
      if (term && !tags.includes(term)) tags.push(term);
    }
  }

  return {
    id:           sha1Id(SOURCE_NAME, url),
    title,
    url,
    source:       SOURCE_NAME,
    source_url:   SOURCE_URL,
    published_at: publishedAt,
    summary,
    tags,
    location:     null,
    deadline:     null,
    language:     LANGUAGE,
    created_at:   now,
    updated_at:   now,
  };
}

async function normalize(xmlContent, now) {
  const parser = new RssParser({
    customFields: {
      item: [['content:encoded', 'content:encoded']],
    },
  });

  const feed = await parser.parseString(xmlContent);
  const records = [];

  for (const item of feed.items || []) {
    try {
      const record = parseItem(item, now);
      if (record.url) records.push(record);
    } catch (err) {
      console.error(`  ⚠ Skipping entry '${item.title || ''}': ${err.message}`);
    }
  }

  return records;
}

// ---------------------------------------------------------------------------
// Main pipeline
// ---------------------------------------------------------------------------

async function main() {
  const today = todayStr();
  const now   = nowIso();

  console.log(`[${SOURCE_NAME}] Starting pipeline — ${today}`);

  // 1. Fetch (temporary — not persisted)
  let xmlContent;
  try {
    xmlContent = await fetchFeed();
    console.log(`  ✓ Fetched ${(xmlContent.length / 1024).toFixed(1)} KB`);
  } catch (err) {
    console.error(`  ✗ Fetch failed: ${err.message}`);
    process.exit(1);
  }

  // 2. Normalize
  const records = await normalize(xmlContent, now);
  console.log(`  ✓ Normalized ${records.length} record(s)`);

  if (records.length === 0) {
    console.log(`[${SOURCE_NAME}] No records — exiting.`);
    return;
  }

  // 3. Classify and write output
  classifyRecords(records, SOURCE_NAME, today);

  console.log(`[${SOURCE_NAME}] Pipeline complete.`);
}

main().catch(err => {
  console.error(`[${SOURCE_NAME}] Fatal: ${err.message}`);
  process.exit(1);
});
