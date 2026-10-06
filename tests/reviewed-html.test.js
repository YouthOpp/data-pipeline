import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { collect } from '../adapters/reviewed-html.js';
import { runPipeline, validateRecord } from '../scripts/collect.js';

const manifests = JSON.parse(await readFile(new URL('../data/sources/sources.json', import.meta.url), 'utf8'));
const manifest = manifests.find(item => item.source === 'us-nasa-internships');
const fixture = await readFile(new URL('./fixtures/nasa-programme-metadata.html', import.meta.url), 'utf8');
const now = '2026-10-05T05:30:00.000Z';

test('NASA programme joins the existing research registry and categorical indexes exactly once', async () => {
  const registry = JSON.parse(await readFile(new URL('../data/sources/source-registry.json', import.meta.url), 'utf8')).sources;
  const catalog = await runPipeline([manifest], undefined, { now, load: async () => fixture, registry });
  const record = catalog.opportunities[0];
  const joined = catalog.source_registry.filter(source => source.adapter_source_id === manifest.source);
  assert.equal(catalog.model_version, 2);
  assert.equal(catalog.source_registry.length, registry.length);
  assert.equal(joined.length, 1);
  assert.equal(joined[0].id, 'us-nasa-internships');
  assert.equal(joined[0].rights_review_status, 'access_policy_reviewed_metadata_only');
  assert.deepEqual(catalog.indexes.categories.internships, [record.id]);
  assert.deepEqual(catalog.indexes.record_kinds['programme-overview'], [record.id]);
  assert.deepEqual(catalog.indexes.sources[manifest.source], [record.id]);
  assert.ok(catalog.indexes.publisher_countries.US.includes(joined[0].id));
});

test('reviewed NASA HTML publishes only programme metadata with original publication date', async () => {
  const records = await collect({ manifest, now, fetchText: async () => fixture });
  assert.equal(records.length, 1);
  const record = records[0];
  validateRecord(record);
  assert.equal(record.url, manifest.source_url);
  assert.equal(record.title, 'NASA Internship Programs - NASA');
  assert.equal(record.published_at, '2023-01-23T16:57:52.000Z');
  assert.equal(record.summary, '');
  assert.equal(record.category, 'internships');
  assert.deepEqual(record.categories, ['internships']);
  assert.equal(record.kind, 'programme-overview');
  assert.equal(record.publisher_country, 'US');
  assert.deepEqual(record.classification, { method: 'editorial-review', status: 'classified', evidence: [manifest.source_url] });
  assert.deepEqual(record.tags, ['programme-overview']);
  assert.equal(record.status, 'unknown');
  assert.equal(record.deadline, null);
  assert.deepEqual(record.host_countries, []);
  assert.deepEqual(record.eligible_countries, []);
});

test('reviewed HTML rejects wrong canonical, unrelated schemas and missing original dates', async () => {
  for (const html of [
    fixture.replace(/(<link[^>]*href=["'])[^"']+/, '$1https://www.nasa.gov/unrelated/'),
    fixture.replace('"WebPage"', '"NewsArticle"'),
    fixture.replace('2023-01-23T11:57:52-05:00', 'invalid-publication-date'),
    fixture.replace(/"datePublished"\s*:\s*"[^"]+"\s*,?/, ''),
    '<html><h1>NASA Internship Programs</h1><p>No reviewed structured metadata.</p></html>'
  ]) await assert.rejects(collect({ manifest, now, fetchText: async () => html }));
});

test('multiple exact WebPage matches fail rather than selecting ambiguous programme facts', async () => {
  const match = fixture.match(/<script\b[^>]*>[\s\S]*?<\/script\s*>/i);
  await assert.rejects(collect({ manifest, now, fetchText: async () => fixture + match[0] }), /one exact programme WebPage/);
});

test('HTML collection failure preserves prior metadata while another source publishes', async () => {
  const previous = await runPipeline([manifest], undefined, { now, load: async () => fixture });
  const rss = { ...manifests[0], source: 'independent-rss', source_url: 'https://example.org/feed/' };
  const later = '2026-10-05T11:30:00.000Z';
  const result = await runPipeline([manifest, rss], previous, {
    now: later,
    load: async url => url === manifest.source_url ? '<html>Programme metadata unavailable</html>' : '<rss version="2.0"><channel><title>Example</title><item><title>New programme</title><link>https://example.org/programme</link></item></channel></rss>'
  });
  assert.deepEqual(result.opportunities.find(item => item.source === manifest.source), previous.opportunities[0]);
  const source = result.sources.find(item => item.source === manifest.source);
  assert.equal(source.status, 'error');
  assert.equal(source.last_attempt_at, later);
  assert.equal(source.last_success_at, now);
  assert.equal(source.last_checked_at, now);
  assert.equal(source.record_count, 1);
});
