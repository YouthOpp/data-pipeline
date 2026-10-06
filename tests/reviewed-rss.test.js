import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { collect } from '../adapters/reviewed-rss.js';
import { runPipeline, validateRecord } from '../scripts/collect.js';

const manifests = JSON.parse(await readFile(new URL('../data/sources/sources.json', import.meta.url), 'utf8'));
const manifest = manifests.find(item => item.source === 'fulbright-czech-programmes');
const fixture = await readFile(new URL('./fixtures/fulbright-czech-metadata.xml', import.meta.url), 'utf8');
const now = '2026-10-04T22:00:00.000Z';

test('reviewed Czech feed emits exactly two programme metadata records, excluding eight news stories', async () => {
  const xml = fixture.replaceAll('</item>', '<description>Publisher article prose must never be indexed.</description></item>');
  const records = await collect({ manifest, now, fetchText: async () => xml });
  assert.equal(records.length, 2);
  assert.deepEqual(records.map(record => record.url), manifest.reviewed_items.map(item => item.url));
  assert.equal(records[0].title, 'Intercountry travel grant: pozvěte si amerického vědce na pár dní');
  assert.equal(records[0].published_at, '2026-09-03T06:59:43.000Z');
  assert.equal(records[1].title, 'Nemusíte letět na Měsíc. Můžete letět na Fulbrighta.');
  for (const record of records) {
    validateRecord(record);
    assert.equal(record.summary, '');
    assert.equal(record.language, 'cs');
    assert.equal(record.status, 'unknown');
    assert.equal(record.deadline, null);
    assert.deepEqual(record.host_countries, []);
    assert.deepEqual(record.eligible_countries, []);
  }
  assert.deepEqual(records[0].tags, ['institutional-grant']);
  assert.deepEqual(records[1].tags, ['programme-overview']);
});

test('feed containing only unreviewed alumni/news cannot become an opportunity batch', async () => {
  const newsOnly = fixture.replace(/<item>[\s\S]*?<\/item>/g, item => manifest.reviewed_items.some(reviewed => item.includes(reviewed.url)) ? '' : item);
  await assert.rejects(collect({ manifest, now, fetchText: async () => newsOnly }), /No reviewed programme/);
});

test('trusted pipeline dispatch integrates reviewed metadata and rejects arbitrary adapter names', async () => {
  const catalog = await runPipeline([manifest], undefined, { now, load: async () => fixture });
  assert.equal(catalog.opportunities.length, 2);
  assert.equal(catalog.sources[0].status, 'ok');
  assert.equal(catalog.sources[0].record_count, 2);
  await assert.rejects(runPipeline([{ ...manifest, adapter: '../untrusted.js' }], undefined, { now, load: async () => fixture }), /All enabled sources failed/);
});

test('review selection exhaustion preserves metadata and last success while another source publishes', async () => {
  const previous = await runPipeline([manifest], undefined, { now, load: async () => fixture });
  const newsOnly = fixture.replace(/<item>[\s\S]*?<\/item>/g, item => manifest.reviewed_items.some(reviewed => item.includes(reviewed.url)) ? '' : item);
  const second = { ...manifest, source: 'independent-rss', adapter: 'rss', source_url: 'https://example.org/feed/' };
  const later = '2026-10-05T22:00:00.000Z';
  const result = await runPipeline([manifest, second], previous, {
    now: later,
    load: async url => url === manifest.source_url ? newsOnly : '<rss version="2.0"><channel><title>Example</title><item><title>New programme</title><link>https://example.org/programme</link></item></channel></rss>'
  });
  const source = result.sources.find(item => item.source === manifest.source);
  assert.equal(source.status, 'error');
  assert.match(source.error, /No reviewed programme/);
  assert.equal(source.last_attempt_at, later);
  assert.equal(source.last_success_at, now);
  assert.equal(source.last_checked_at, now);
  assert.equal(source.record_count, 2);
  assert.deepEqual(result.opportunities.filter(item => item.source === manifest.source), previous.opportunities);
  assert.equal(result.sources.find(item => item.source === second.source).status, 'ok');
});
