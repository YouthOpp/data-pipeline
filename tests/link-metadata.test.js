import test from 'node:test';
import assert from 'node:assert/strict';
import { collect } from '../adapters/link-metadata.js';
import { validateRecord } from '../scripts/collect.js';

const now = '2026-10-06T08:00:00.000Z';
const manifest = {
  source: 'example-link',
  source_url: 'https://example.org/programmes/current?view=full',
  website_url: 'https://publisher.example/',
  enabled: true,
  adapter: 'link-metadata',
  language: 'en',
  default_tags: ['scholarship'],
  publisher_country: 'DE'
};
const robotsUrl = 'https://example.org/robots.txt';

function injected(responses, calls = []) {
  return async url => {
    calls.push(url);
    const response = responses[url];
    if (response instanceof Error) throw response;
    if (typeof response !== 'string') throw new Error(`Unexpected request: ${url}`);
    return response;
  };
}

test('permission review gate prevents robots and page requests', async () => {
  let requests = 0;
  await assert.rejects(collect({
    manifest: { ...manifest, collection_blocked_reason: 'Publisher permission is required' },
    now,
    fetchText: async () => { requests += 1; return ''; }
  }), /permission policy/);
  assert.equal(requests, 0);
});

test('specific robots group blocks before the page even when wildcard allows', async () => {
  const calls = [];
  await assert.rejects(collect({
    manifest,
    now,
    fetchText: injected({
      [robotsUrl]: 'User-agent: *\nAllow: /\n\nUser-agent: YouthOpp\nDisallow: /programmes/'
    }, calls)
  }), /Robots policy disallows/);
  assert.deepEqual(calls, [robotsUrl]);
});

test('unsafe or invalid crawl delays fail closed before requesting the page', async () => {
  for (const crawlDelay of ['30.1', 'later', '-1', '']) {
    const calls = [];
    await assert.rejects(collect({
      manifest,
      now,
      fetchText: injected({
        [robotsUrl]: `User-agent: YouthOpp\nAllow: /\nCrawl-delay: ${crawlDelay}`
      }, calls)
    }), /crawl delay/i);
    assert.deepEqual(calls, [robotsUrl]);
  }
});

test('longest matching robots rule and end wildcard allow the selected page', async () => {
  const calls = [];
  const html = `<!doctype html><html><head>
    <title>Lower priority title</title>
    <meta property="og:title" content="Also lower priority">
    <meta property="og:url" content="/programmes/current?view=full">
    <link rel="alternate canonical" href="./current?view=full">
    <meta name="description" content="This prose must never be copied">
  </head><body><h1>Exact &amp; Safe Programme Title</h1><p>Private source prose.</p></body></html>`;
  const [record] = await collect({
    manifest,
    now,
    fetchText: injected({
      [robotsUrl]: 'User-agent: YouthOpp\nDisallow: /programmes/*\nAllow: /programmes/current?view=full$\nCrawl-delay: 0',
      [manifest.source_url]: html
    }, calls)
  });

  assert.deepEqual(calls, [robotsUrl, manifest.source_url]);
  validateRecord(record);
  assert.equal(record.title, 'Exact & Safe Programme Title');
  assert.equal(record.url, manifest.source_url);
  assert.equal(record.source_url, manifest.source_url);
  assert.equal(record.summary, '');
  assert.equal(record.published_at, null);
  assert.equal(record.deadline, null);
  assert.equal(record.status, 'unknown');
  assert.equal(record.location, null);
  assert.deepEqual(record.host_countries, []);
  assert.deepEqual(record.eligible_countries, []);
  assert.deepEqual(record.tags, []);
  assert.equal(record.category, 'other');
  assert.deepEqual(record.categories, ['other']);
  assert.equal(record.kind, 'unknown');
  assert.equal(record.publisher_country, 'DE');
  assert.deepEqual(record.classification, { method: 'link-metadata-v1', status: 'unknown', evidence: [] });
});

test('an exact robots 404 permits collection and title fallback is h1 then Open Graph then title', async () => {
  for (const [robots, html, expected] of [
    [new Error('HTTP 404'), '<meta property="og:title" content="Open Graph Programme"><title>Document title</title>', 'Open Graph Programme'],
    ['# Publisher has no crawler restrictions\n', '<title>Document title</title>', 'Document title']
  ]) {
    const calls = [];
    const [record] = await collect({
      manifest,
      now,
      fetchText: injected({ [robotsUrl]: robots, [manifest.source_url]: html }, calls)
    });
    assert.equal(record.title, expected);
    assert.deepEqual(calls, [robotsUrl, manifest.source_url]);
  }
});

test('canonical and Open Graph identities cannot redirect metadata to another page', async () => {
  for (const html of [
    '<link rel="canonical" href="/elsewhere"><h1>Good programme</h1>',
    '<link rel="canonical" href="/programmes/current?view=full"><link rel="canonical" href="/programmes/current?view=full"><h1>Good programme</h1>',
    '<meta property="og:url" content="https://other.example/programme"><h1>Good programme</h1>',
    '<meta property="og:url" content="/programmes/current?view=full"><meta property="og:url" content="/elsewhere"><h1>Good programme</h1>',
    '<link rel="canonical"><h1>Good programme</h1>'
  ]) {
    await assert.rejects(collect({
      manifest,
      now,
      fetchText: injected({ [robotsUrl]: '', [manifest.source_url]: html })
    }), /canonical|Open Graph URL/i);
  }
});

test('unavailable robots, ambiguous metadata, empty pages and access challenges fail closed', async () => {
  for (const robotsError of ['HTTP 403', 'HTTP 500', 'HTTP 301', 'fetch failed']) {
    const calls = [];
    await assert.rejects(collect({
      manifest,
      now,
      fetchText: injected({ [robotsUrl]: new Error(robotsError) }, calls)
    }), /Robots policy is unavailable/);
    assert.deepEqual(calls, [robotsUrl]);
  }

  for (const html of [
    '',
    '<h1>One</h1><h1>Two</h1>',
    '<h1></h1><meta property="og:title" content="Fallback must not hide a broken h1">',
    '<title>Just a moment...</title>',
    '<h1>403 Forbidden</h1>',
    '<h1>Sorry, you have been blocked</h1>',
    '<meta property="og:title" content="Access Denied | Publisher"><title>Good title</title>',
    `<h1>${'x'.repeat(301)}</h1>`
  ]) {
    await assert.rejects(collect({
      manifest,
      now,
      fetchText: injected({ [robotsUrl]: '', [manifest.source_url]: html })
    }), /title|h1|og:title/i);
  }
});

test('robots wildcard and dollar matching do not overblock a different path suffix', async () => {
  const [record] = await collect({
    manifest,
    now,
    fetchText: injected({
      [robotsUrl]: 'User-agent: *\nDisallow: /programmes/current$\nAllow: /programmes/*',
      [manifest.source_url]: '<h1>Query-specific programme</h1>'
    })
  });
  assert.equal(record.title, 'Query-specific programme');
});

test('HTML5 title entities decode without copying publisher descriptions',async()=>{
 const [record]=await collect({manifest,now,fetchText:injected({[robotsUrl]:'',[manifest.source_url]:'<h1>Bachelor&#039;s &amp; H&auml;ufige &#x1F393; Questions</h1><p>Publisher description</p>'})});
 assert.equal(record.title,"Bachelor's & Häufige 🎓 Questions");assert.equal(record.summary,'');
});
test('navigation landmark heading falls back to actual page title',async()=>{
 const [record]=await collect({manifest,now,fetchText:injected({[robotsUrl]:'',[manifest.source_url]:'<h1>Navega&ccedil;&atilde;o</h1><title>Candidaturas | IPDJ</title>'})});
 assert.equal(record.title,'Candidaturas | IPDJ');
});
