import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { collect } from '../adapters/reviewed-html.js';
import { runPipeline, validateRecord } from '../scripts/collect.js';

const manifests = JSON.parse(await readFile(new URL('../data/sources/sources.json', import.meta.url), 'utf8'));
const manifest = manifests.find(item => item.source === 'lu-fnr-afdoc');
const fixture = await readFile(new URL('./fixtures/fnr-afdoc-programme.html', import.meta.url), 'utf8');
const now = '2026-10-05T06:45:00.000Z';

test('AFdoc preserves exact original metadata and unknown individual application facts', async () => {
  const requested = [];
  const [record] = await collect({ manifest, now, fetchText: async url => { requested.push(url); return fixture; } });
  validateRecord(record);
  assert.deepEqual(requested, [manifest.source_url]);
  assert.equal(record.title, 'AFdoc - FNR');
  assert.equal(record.url, manifest.source_url);
  assert.equal(record.published_at, '2026-07-29T13:25:54.000Z');
  assert.equal(record.summary, '');
  assert.equal(record.status, 'unknown');
  assert.equal(record.deadline, null);
  assert.deepEqual(record.host_countries, []);
  assert.deepEqual(record.eligible_countries, []);
  assert.equal(record.publisher_country, 'LU');
  assert.equal(record.category, 'scholarships');
  assert.equal(record.kind, 'programme-overview');
});

test('AFdoc uses the existing Luxembourg publisher and exact scholarship index', async () => {
  const registry = JSON.parse(await readFile(new URL('../data/sources/source-registry.json', import.meta.url), 'utf8')).sources;
  const result = await runPipeline([manifest], undefined, { now, registry, load: async () => fixture });
  const [record] = result.opportunities;
  const joined = result.source_registry.filter(source => source.adapter_source_id === manifest.source);
  assert.equal(result.source_registry.length, registry.length);
  assert.equal(joined.length, 1);
  assert.equal(joined[0].id, 'lu-fnr-afdoc');
  assert.deepEqual(result.indexes.categories.scholarships, [record.id]);
  assert.deepEqual(result.indexes.record_kinds['programme-overview'], [record.id]);
});

test('AFdoc refuses a changed canonical identity or missing original publication', async () => {
  for (const html of [
    fixture.replace('<link rel="canonical" href="https://www.fnr.lu/funding-instruments/afdoc/">', '<link rel="canonical" href="https://www.fnr.lu/unrelated/">'),
    fixture.replace('"datePublished": "2026-07-29T13:25:54+00:00",', '')
  ]) await assert.rejects(collect({ manifest, now, fetchText: async () => html }));
});

test('AFdoc outage retains its prior record and original successful check', async () => {
  const previous = await runPipeline([manifest], undefined, { now, load: async () => fixture });
  const rss = { ...manifests[0], source: 'independent-rss', source_url: 'https://example.org/feed/' };
  const later = '2026-10-05T12:45:00.000Z';
  const result = await runPipeline([manifest, rss], previous, { now: later, load: async url => {
    if (url === manifest.source_url) throw new Error('FNR unavailable');
    return '<rss version="2.0"><channel><title>Example</title><item><title>Programme</title><link>https://example.org/programme</link></item></channel></rss>';
  } });
  assert.deepEqual(result.opportunities.find(record => record.source === manifest.source), previous.opportunities[0]);
  const health = result.sources.find(source => source.source === manifest.source);
  assert.equal(health.status, 'error');
  assert.equal(health.last_attempt_at, later);
  assert.equal(health.last_success_at, now);
  assert.equal(health.record_count, 1);
});
