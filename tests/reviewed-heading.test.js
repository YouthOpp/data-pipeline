import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { collect } from '../adapters/reviewed-html.js';
import { runPipeline, validateRecord } from '../scripts/collect.js';

const url = 'https://studyinaustria.at/en/news/article/2026/08/ernst-mach-stipendium-weltweit-bewerbung-bis-1-dezember-2026';
const title = 'Ernst Mach Scholarship – Worldwide: Application Deadline 1 December 2026';
const manifests = JSON.parse(await readFile(new URL('../data/sources/sources.json', import.meta.url), 'utf8'));
const manifest = manifests.find(source => source.source === 'at-oead-ernst-mach');
const registry = JSON.parse(await readFile(new URL('../data/sources/source-registry.json', import.meta.url), 'utf8')).sources;
const fixture = await readFile(new URL('./fixtures/oead-notice-metadata.html', import.meta.url), 'utf8');
const now = '2026-10-05T08:00:00.000Z';

test('reviewed heading indexes original metadata without generic OG title or invented timing', async () => {
  const [record] = await collect({ manifest, now, fetchText: async () => fixture });
  validateRecord(record);
  assert.equal(record.title, title);
  assert.equal(record.url, url);
  assert.equal(record.published_at, null);
  assert.equal(record.deadline, null);
  assert.equal(record.status, 'unknown');
  assert.equal(record.summary, '');
  assert.deepEqual(record.eligible_countries, []);
  assert.deepEqual(record.host_countries, []);
});

test('reviewed heading rejects duplicate, missing, changed and noncanonical identities', async () => {
  for (const html of [fixture.replace('<h1', '<h2').replace('</h1>', '</h2>'), fixture.replace(title, 'An unrelated call'), fixture.replace('</body>', `<h1>${title}</h1></body>`), fixture.replace('rel="canonical"', 'rel="unrelated"'), fixture.replace(url, 'https://oead.at/unreviewed-page')]) {
    await assert.rejects(collect({ manifest, now, fetchText: async () => html }));
  }
});

test('script headings and modification metadata cannot replace reviewed page identity or publication', async () => {
  const html = fixture.replace('</head>', '<script>const markup = "<h1>Unrelated title</h1>";</script><meta property="article:modified_time" content="2026-10-05T07:00:00Z"></head>');
  const [record] = await collect({ manifest, now, fetchText: async () => html });
  assert.equal(record.title, title);
  assert.equal(record.published_at, null);
});

test('published source metadata preserves required copyright credit and unknown application state', async () => {
  const catalog = await runPipeline([manifest], undefined, { now, load: async () => fixture, registry });
  assert.equal(catalog.opportunities.length, 1);
  assert.equal(catalog.opportunities[0].status, 'unknown');
  assert.equal(catalog.sources[0].attribution, '© OeAD');
  const joined = catalog.source_registry.filter(source => source.adapter_source_id === manifest.source);
  assert.equal(joined.length, 1);
  assert.equal(joined[0].id, 'at-oead');
  assert.equal(joined[0].attribution, '© OeAD');
  assert.equal(catalog.source_registry.length, registry.length);
  assert.ok(catalog.indexes.publisher_countries.AT.includes(joined[0].id));
  assert.deepEqual(catalog.indexes.categories.scholarships, [catalog.opportunities[0].id]);
});
