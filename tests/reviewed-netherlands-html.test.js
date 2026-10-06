import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { collect } from '../adapters/reviewed-html.js';
import { runPipeline, validateRecord } from '../scripts/collect.js';

const manifests = JSON.parse(await readFile(new URL('../data/sources/sources.json', import.meta.url), 'utf8'));
const manifest = manifests.find(item => item.source === 'nl-fulbright-doctoral');
const fixture = await readFile(new URL('./fixtures/fulbright-netherlands-programme.html', import.meta.url), 'utf8');
const now = '2026-10-05T06:30:00.000Z';

test('Netherlands programme preserves original metadata and unknown application facts', async () => {
  const requested = [];
  const [record] = await collect({ manifest, now, fetchText: async url => { requested.push(url); return fixture; } });
  validateRecord(record);
  assert.deepEqual(requested, [manifest.source_url]);
  assert.equal(record.title, 'fulbright beurzen voor promovendi');
  assert.equal(record.url, manifest.source_url);
  assert.equal(record.published_at, null);
  assert.equal(record.summary, '');
  assert.equal(record.status, 'unknown');
  assert.equal(record.deadline, null);
  assert.deepEqual(record.host_countries, []);
  assert.deepEqual(record.eligible_countries, []);
  assert.equal(record.publisher_country, 'NL');
  assert.equal(record.category, 'scholarships');
  assert.equal(record.kind, 'programme-overview');
});

test('Netherlands programme joins existing source without creating a publisher duplicate', async () => {
  const registry = JSON.parse(await readFile(new URL('../data/sources/source-registry.json', import.meta.url), 'utf8')).sources;
  const result = await runPipeline([manifest], undefined, { now, registry, load: async () => fixture });
  const [record] = result.opportunities;
  const joined = result.source_registry.filter(source => source.adapter_source_id === manifest.source);
  assert.equal(result.source_registry.length, registry.length);
  assert.equal(joined.length, 1);
  assert.equal(joined[0].id, 'nl-fulbright-netherlands');
  assert.deepEqual(result.indexes.categories.scholarships, [record.id]);
  assert.deepEqual(result.indexes.record_kinds['programme-overview'], [record.id]);
});

test('Wrong Dutch programme identity preserves prior metadata and source success timestamp', async () => {
  const previous = await runPipeline([manifest], undefined, { now, load: async () => fixture });
  const independent = { ...manifests[0], source: 'independent-rss', source_url: 'https://example.org/feed/' };
  const later = '2026-10-05T12:30:00.000Z';
  const wrong = fixture.replace('property="og:url"', 'property="unrelated:url"');
  const result = await runPipeline([manifest, independent], previous, { now: later, load: async url => url === manifest.source_url ? wrong : '<rss version="2.0"><channel><title>Example</title><item><title>Programme</title><link>https://example.org/programme</link></item></channel></rss>' });
  assert.deepEqual(result.opportunities.find(record => record.source === manifest.source), previous.opportunities[0]);
  const health = result.sources.find(source => source.source === manifest.source);
  assert.equal(health.status, 'error');
  assert.equal(health.last_attempt_at, later);
  assert.equal(health.last_success_at, now);
  assert.equal(health.record_count, 1);
});
