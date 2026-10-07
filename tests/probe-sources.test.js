import test from 'node:test';
import assert from 'node:assert/strict';
import { probeSources } from '../scripts/probe-sources.js';

const manifest = (source, overrides = {}) => ({
  source,
  source_url: `https://${source}.example.org/programme`,
  website_url: `https://${source}.example.org/`,
  enabled: false,
  adapter: 'link-metadata',
  ...overrides
});

const probeRecord = (selected, index = 0, overrides = {}) => {
  const now = '2026-10-06T00:00:00.000Z';
  const url = `${selected.source_url}/${index}`;
  return {
    id: `${selected.source}-${index}`,
    title: `Title ${index}`,
    url,
    source: selected.source,
    source_url: selected.source_url,
    published_at: null,
    summary: '',
    tags: [],
    location: null,
    deadline: null,
    language: 'en',
    category: 'other',
    categories: ['other'],
    kind: 'unknown',
    publisher_country: null,
    classification: { method: 'link-metadata-v1', status: 'unknown', evidence: [] },
    host_countries: [],
    eligible_countries: [],
    created_at: now,
    updated_at: now,
    first_seen_at: now,
    last_seen_at: now,
    last_checked_at: now,
    status: 'unknown',
    ...overrides
  };
};

test('probe fetches a disabled source without changing its production flag and bounds samples', async () => {
  let fetches = 0;
  const result = await probeSources([manifest('disabled')], {
    now: '2026-10-06T00:00:00.000Z',
    hostDelayMs: 0,
    load: async () => { fetches += 1; return '<html></html>'; },
    adapters: {
      'link-metadata': async ({ manifest: selected, fetchText }) => {
        await fetchText(selected.source_url);
        return Array.from({ length: 4 }, (_, index) => probeRecord(selected, index));
      }
    }
  });

  assert.equal(fetches, 1);
  assert.deepEqual(result.summary, { selected: 1, successful: 1, failed: 0, blocked: 0 });
  assert.equal(result.sources[0].enabled, false);
  assert.equal(result.sources[0].requested_url, 'https://disabled.example.org/programme');
  assert.equal(result.sources[0].adapter, 'link-metadata');
  assert.equal(result.sources[0].metadata_scope, 'factual-title-link-only');
  assert.equal(result.sources[0].application_state, 'not_assessed');
  assert.equal(result.sources[0].record_count, 4);
  assert.equal(result.sources[0].samples.length, 3);
  assert.deepEqual(Object.keys(result.sources[0].samples[0]), ['title', 'url']);
});

test('collection block is enforced before dispatch for every adapter', async () => {
  let dispatches = 0;
  let fetches = 0;
  const blocked = manifest('restricted', {
    adapter: 'rss',
    collection_blocked_reason: 'Publisher permission is required before automated retrieval.'
  });
  const result = await probeSources([blocked], {
    hostDelayMs: 0,
    load: async () => { fetches += 1; return ''; },
    adapters: { rss: async () => { dispatches += 1; return []; } }
  });

  assert.equal(dispatches, 0);
  assert.equal(fetches, 0);
  assert.deepEqual(result.summary, { selected: 1, successful: 0, failed: 0, blocked: 1 });
  assert.equal(result.sources[0].status, 'blocked');
  assert.match(result.sources[0].error, /permission/i);
});

test('same-host sources are sequential while independent hosts are concurrency bounded', async () => {
  let active = 0;
  let maximumActive = 0;
  const activeHosts = new Set();
  const selected = [
    manifest('one', { source_url: 'https://shared.example.org/one' }),
    manifest('two', { source_url: 'https://shared.example.org/two' }),
    manifest('three'),
    manifest('four'),
    manifest('five'),
    manifest('six')
  ];
  const result = await probeSources(selected, {
    concurrency: 4,
    hostDelayMs: 0,
    adapters: {
      'link-metadata': async ({ manifest: current }) => {
        const host = new URL(current.source_url).hostname;
        assert.equal(activeHosts.has(host), false, `parallel request to ${host}`);
        activeHosts.add(host);
        active += 1;
        maximumActive = Math.max(maximumActive, active);
        await new Promise(resolve => setTimeout(resolve, 5));
        active -= 1;
        activeHosts.delete(host);
        return [probeRecord(current, 0, { title: current.source, url: current.source_url })];
      }
    }
  });

  assert.equal(result.summary.successful, selected.length);
  assert.ok(maximumActive <= 4);
});

test('adapter failures are reported without hiding successful probes or copying content', async () => {
  const result = await probeSources([manifest('good'), manifest('bad')], {
    hostDelayMs: 0,
    adapters: {
      'link-metadata': async ({ manifest: current }) => {
        if (current.source === 'bad') throw new Error('HTTP 503 with sensitive response body omitted');
        return [probeRecord(current, 0, { title: 'Verified title', url: current.source_url, summary: 'must not be copied' })];
      }
    }
  });

  assert.deepEqual(result.summary, { selected: 2, successful: 1, failed: 1, blocked: 0 });
  assert.deepEqual(result.sources[0].samples, [{ title: 'Verified title', url: 'https://good.example.org/programme' }]);
  assert.equal(JSON.stringify(result).includes('must not be copied'), false);
  assert.match(result.sources[1].error, /HTTP 503/);
});

test('invalid adapter records cannot be reported as a successful probe', async () => {
  const selected = manifest('invalid');
  const result = await probeSources([selected], {
    hostDelayMs: 0,
    adapters: { 'link-metadata': async () => [{ title: 'Unvalidated', url: selected.source_url }] }
  });

  assert.deepEqual(result.summary, { selected: 1, successful: 0, failed: 1, blocked: 0 });
  assert.match(result.sources[0].error, /Missing id/);
});

test('robots denials are reported as blocked rather than transient adapter failures', async () => {
  const selected = manifest('robots-denied');
  const result = await probeSources([selected], {
    hostDelayMs: 0,
    adapters: { 'link-metadata': async () => { throw new Error('Robots policy disallows collection'); } }
  });

  assert.deepEqual(result.summary, { selected: 1, successful: 0, failed: 0, blocked: 1 });
  assert.equal(result.sources[0].status, 'blocked');
  assert.match(result.sources[0].error, /Robots policy/);
});

test('adapter lookup accepts only own trusted-dispatch properties', async () => {
  const selected = manifest('prototype', { adapter: 'toString' });
  const result = await probeSources([selected], { hostDelayMs: 0, adapters: {} });
  assert.deepEqual(result.summary, { selected: 1, successful: 0, failed: 1, blocked: 0 });
  assert.match(result.sources[0].error, /Untrusted or unknown adapter/);
});
