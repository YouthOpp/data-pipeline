import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { collect } from '../adapters/reviewed-html.js';
import { runPipeline, validateRecord } from '../scripts/collect.js';
const manifests = JSON.parse(await readFile(new URL('../data/sources/sources.json', import.meta.url), 'utf8'));
const manifest = manifests.find(item => item.source === 'pt-fulbright-masters');
const fixture = await readFile(new URL('./fixtures/fulbright-portugal-programme.html', import.meta.url), 'utf8');
const now = '2026-10-05T06:00:00.000Z';

test('Portugal programme uses exact metadata without inventing original publication or application state', async () => {
  const [record] = await collect({ manifest, now, fetchText: async () => fixture });
  validateRecord(record);
  assert.equal(record.title, 'Bolsa Fulbright para Mestrado');
  assert.equal(record.url, manifest.source_url);
  assert.equal(record.published_at, null);
  assert.equal(record.summary, '');
  assert.equal(record.status, 'unknown');
  assert.equal(record.deadline, null);
  assert.deepEqual(record.host_countries, []);
  assert.deepEqual(record.eligible_countries, []);
  assert.equal(record.publisher_country, 'PT');
  assert.equal(record.category, 'scholarships');
  assert.equal(record.kind, 'programme-overview');
});

test('Portugal joins one existing research source and exact categorical indexes', async () => {
  const registry = JSON.parse(await readFile(new URL('../data/sources/source-registry.json', import.meta.url), 'utf8')).sources;
  const catalog = await runPipeline([manifest], undefined, { now, registry, load: async () => fixture });
  const [record] = catalog.opportunities;
  assert.equal(catalog.source_registry.length, registry.length);
  const joins = catalog.source_registry.filter(source => source.adapter_source_id === manifest.source);
  assert.equal(joins.length, 1);
  assert.equal(joins[0].id, 'pt-fulbright-portugal');
  assert.deepEqual(catalog.indexes.categories.scholarships, [record.id]);
  assert.deepEqual(catalog.indexes.record_kinds['programme-overview'], [record.id]);
});

test('Open Graph requires exact unambiguous canonical, URL and title', async () => {
  for (const html of [
    fixture.replace('rel="canonical"', 'rel="alternate"'),
    fixture.replace(/(<link[^>]*href=")[^"]+/, '$1https://www.fulbright.pt/other/'),
    fixture.replace('property="og:url"', 'property="unrelated:url"'),
    fixture.replace(/(<meta property="og:url" content=")[^"]+/, '$1https://www.fulbright.pt/other/'),
    fixture.replace('property="og:title"', 'property="unrelated:title"'),
    fixture.replace('  Bolsa Fulbright para Mestrado', ' '),
    fixture + '<meta property="og:title" content="Conflicting title" />',
    fixture + '<meta property="og:url" content="https://www.fulbright.pt/other/" />',
    fixture + '<meta property="article:published_time" content="invalid" />',
    fixture + '<meta property="article:published_time" content="2020-01-01" /><meta property="article:published_time" content="2021-01-01" />'
  ]) await assert.rejects(collect({ manifest, now, fetchText: async () => html }));
});

test('Open Graph original publication is used only when expressly supplied', async () => {
  const [record] = await collect({ manifest, now, fetchText: async () => fixture + '<meta property="article:published_time" content="2020-01-01T10:00:00+00:00" />' });
  assert.equal(record.published_at, '2020-01-01T10:00:00.000Z');
});

test('Portugal identity failure preserves previous metadata during independent source success', async () => {
  const previous = await runPipeline([manifest], undefined, { now, load: async () => fixture });
  const rss = { ...manifests[0], source: 'independent-rss', source_url: 'https://example.org/feed/' };
  const later = '2026-10-05T12:00:00.000Z';
  const result = await runPipeline([manifest, rss], previous, { now: later, load: async url => url === manifest.source_url ? '<html>Unavailable</html>' : '<rss version="2.0"><channel><title>Example</title><item><title>Programme</title><link>https://example.org/programme</link></item></channel></rss>' });
  assert.deepEqual(result.opportunities.find(record => record.source === manifest.source), previous.opportunities[0]);
  const health = result.sources.find(source => source.source === manifest.source);
  assert.equal(health.status, 'error');
  assert.equal(health.last_attempt_at, later);
  assert.equal(health.last_success_at, now);
  assert.equal(health.record_count, 1);
});
