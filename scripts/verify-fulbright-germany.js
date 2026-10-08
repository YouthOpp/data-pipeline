import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { setTimeout as delay } from 'node:timers/promises';
import { runPipeline, fetchText, validateRecord } from '../scripts/collect.js';

const sourceId = 'de-fulbright-germany';
const expectedTitle = 'Studienstipendium (Uni und HAW)';
const mode = process.argv[2] || 'fixture';
const now = new Date().toISOString();
const manifests = JSON.parse(await readFile('data/sources/sources.json', 'utf8'));
const registry = JSON.parse(await readFile('data/sources/source-registry.json', 'utf8')).sources;
const manifest = manifests.find(item => item.source === sourceId);
assert.ok(manifest?.enabled, 'Fulbright source must be enabled');
assert.equal(manifest.adapter, 'opportunity-html');
assert.equal(manifest.reviewed_opportunity?.url, manifest.source_url);

function verifyResult(result) {
  assert.equal(result.opportunities.length, 1, 'Expected exactly one reviewed Fulbright programme');
  const [record] = result.opportunities;
  validateRecord(record);
  assert.equal(record.id, createHash('sha256').update(sourceId + '|' + manifest.source_url).digest('hex').slice(0, 24));
  assert.equal(record.title, expectedTitle, 'Live publisher programme title changed');
  assert.equal(record.url, manifest.source_url);
  assert.equal(record.source, sourceId);
  assert.equal(record.source_url, manifest.source_url);
  assert.equal(record.publisher_country, 'DE');
  assert.equal(record.category, 'scholarships');
  assert.deepEqual(record.categories, ['scholarships']);
  assert.equal(record.kind, 'programme-overview');
  assert.equal(record.status, 'unknown', 'Conflicting application messages must not imply open status');
  assert.equal(record.deadline, null, 'Do not invent an exact deadline');
  assert.equal(record.published_at, null, 'Do not invent publication time');
  assert.equal(record.summary, '', 'Do not reproduce publisher prose');
  assert.deepEqual(record.eligible_countries, [], 'No inferred applicant eligibility');
  assert.deepEqual(record.host_countries, [], 'Publisher geography is not host geography');
  assert.equal(result.sources[0].status, 'ok');
  assert.equal(result.sources[0].record_count, 1);
  assert.deepEqual(result.indexes.sources[sourceId], [record.id]);
  assert.ok(result.indexes.categories.scholarships.includes(record.id));
  assert.equal(result.source_registry.filter(s => s.adapter_source_id === sourceId).length, 1);
  console.log(JSON.stringify({source: sourceId, mode, qa: 'pass', count: 1, id: record.id, title: record.title, status: record.status}));
}

const fixtureHtml = '<!doctype html><html><head><title>' + expectedTitle + '</title><link rel="canonical" href="' + manifest.source_url + '"><meta property="og:url" content="' + manifest.source_url + '"></head><body><main><h1>' + expectedTitle + '</h1><h2>Das Programm</h2><p>Bewerbungsfrist: Die Bewerbungsfrist fuer 2027 ist verstrichen.</p></main></body></html>';
const fixtureFetch = async url => url.endsWith('/robots.txt') ? 'User-agent: *\nAllow: /' : fixtureHtml;

if (mode === 'fixture') {
  const result = await runPipeline([manifest], undefined, {now, registry, load: fixtureFetch});
  verifyResult(result);
  await assert.rejects(runPipeline([manifest], undefined, {
    now, registry,
    load: async url => url.endsWith('/robots.txt') ? 'User-agent: *\nDisallow: /stipendien/' : fixtureHtml
  }), /All enabled sources failed.*Robots policy disallows collection/);
  await assert.rejects(runPipeline([manifest], undefined, {
    now, registry,
    load: async url => url.endsWith('/robots.txt') ? 'User-agent: *\nAllow: /' : fixtureHtml.replace('<h1>' + expectedTitle + '</h1>', '<h1>Unrelated programme</h1>')
  }).then(verifyResult), /Live publisher programme title changed/);
} else if (mode === 'live') {
  let lastRequest = 0;
  const pacedFetch = async url => {
    const target = new URL(url);
    assert.ok(['fulbright.de', 'www.fulbright.de'].includes(target.hostname), 'Unexpected external host');
    const wait = Math.max(0, 6500 - (Date.now() - lastRequest));
    if (lastRequest && wait) await delay(wait);
    lastRequest = Date.now();
    const response = await fetchText(url);
    console.log(JSON.stringify({kind: 'source-fetch', path: target.pathname, bytes: Buffer.byteLength(response)}));
    if (target.pathname !== '/robots.txt') {
      assert.match(response, /Studienstipendium/i, 'Publisher programme text missing');
      assert.match(response, /Bewerbungsfrist/i, 'Publisher application window evidence missing');
    }
    return response;
  };
  verifyResult(await runPipeline([manifest], undefined, {now, registry, load: pacedFetch}));
} else {
  throw new Error('Expected fixture or live mode');
}
