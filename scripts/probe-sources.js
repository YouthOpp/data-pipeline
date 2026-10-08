import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { fetchText, trustedAdapters, validateRecord } from './collect.js';

const DEFAULT_CONCURRENCY = 4;
const DEFAULT_HOST_DELAY_MS = 1_000;

function message(error) {
  return String(error?.message || error || 'Unknown probe error').slice(0, 200);
}

function sampleRecords(records) {
  return records.slice(0, 3).map(record => ({
    title: typeof record.title === 'string' ? record.title : '',
    url: typeof record.url === 'string' ? record.url : ''
  }));
}

function resultIdentity(manifest) {
  return {
    source: manifest.source,
    requested_url: manifest.source_url,
    adapter: manifest.adapter,
    enabled: manifest.enabled,
    metadata_scope: manifest.adapter === 'link-metadata' ? 'factual-title-link-only' : 'validated-records-no-prose',
    application_state: 'not_assessed'
  };
}

function blockedResult(manifest, reason = manifest.collection_blocked_reason) {
  return {
    ...resultIdentity(manifest),
    status: 'blocked',
    record_count: 0,
    samples: [],
    error: message(reason)
  };
}

function isRobotsBlock(error) {
  const value = message(error);
  return value.startsWith('Robots ') && /(?:collection denied|disallows collection|safe collection window)/i.test(value);
}

async function probeManifest(manifest, { adapters, load, now, validate }) {
  if (typeof manifest.collection_blocked_reason === 'string' && manifest.collection_blocked_reason.trim()) return blockedResult(manifest);
  try {
    if (!Object.hasOwn(adapters, manifest.adapter)) throw new Error(`Untrusted or unknown adapter: ${manifest.adapter}`);
    const adapter = adapters[manifest.adapter];
    if (typeof adapter !== 'function') throw new Error(`Untrusted or unknown adapter: ${manifest.adapter}`);
    const records = await adapter({ manifest, fetchText: load, now });
    if (!Array.isArray(records) || records.length === 0) throw new Error('Empty adapter output');
    for (const record of records) validate(record);
    return {
      ...resultIdentity(manifest),
      status: 'ok',
      record_count: records.length,
      samples: sampleRecords(records),
      error: null
    };
  } catch (error) {
    if (isRobotsBlock(error)) return blockedResult(manifest, error);
    return {
      ...resultIdentity(manifest),
      status: 'error',
      record_count: 0,
      samples: [],
      error: message(error)
    };
  }
}

function groupByHost(manifests) {
  const groups = new Map();
  for (const manifest of manifests) {
    let host;
    try {
      host = new URL(manifest.source_url).hostname;
    } catch {
      host = `invalid:${manifest.source}`;
    }
    if (!groups.has(host)) groups.set(host, []);
    groups.get(host).push(manifest);
  }
  return [...groups.values()];
}

/**
 * Probe selected manifests without changing their enabled flags or publishing data.
 * Requests to the same host are serialized; at most `concurrency` hosts run at once.
 */
export async function probeSources(manifests, {
  adapters = trustedAdapters,
  load = fetchText,
  now = new Date().toISOString(),
  concurrency = DEFAULT_CONCURRENCY,
  hostDelayMs = DEFAULT_HOST_DELAY_MS,
  validate = validateRecord,
  sleep = milliseconds => new Promise(resolvePromise => setTimeout(resolvePromise, milliseconds))
} = {}) {
  if (!Array.isArray(manifests) || manifests.length === 0) throw new Error('No sources selected for probing');
  if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > DEFAULT_CONCURRENCY) throw new Error('Probe concurrency must be between 1 and 4');

  const positions = new Map(manifests.map((manifest, index) => [manifest.source, index]));
  const groups = groupByHost(manifests);
  const results = [];
  let nextGroup = 0;

  async function worker() {
    while (nextGroup < groups.length) {
      const group = groups[nextGroup++];
      for (let index = 0; index < group.length; index += 1) {
        const manifest = group[index];
        results.push(await probeManifest(manifest, { adapters, load, now, validate }));
        if (index < group.length - 1 && hostDelayMs > 0 && !manifest.collection_blocked_reason) await sleep(hostDelayMs);
      }
    }
  }

  await Promise.all(Array.from({ length: Math.min(concurrency, groups.length) }, () => worker()));
  results.sort((left, right) => positions.get(left.source) - positions.get(right.source));
  const summary = {
    selected: results.length,
    successful: results.filter(result => result.status === 'ok').length,
    failed: results.filter(result => result.status === 'error').length,
    blocked: results.filter(result => result.status === 'blocked').length
  };
  return { schema_version: 1, generated_at: now, sources: results, summary };
}

function selectManifests(manifests, argumentsList) {
  if (argumentsList.length === 1 && argumentsList[0] === '--all') return manifests;
  if (argumentsList.length === 2 && argumentsList[0] === '--source' && /^[a-z0-9-]+$/.test(argumentsList[1])) {
    const selected = manifests.find(manifest => manifest.source === argumentsList[1]);
    if (!selected) throw new Error(`Unknown source: ${argumentsList[1]}`);
    return [selected];
  }
  throw new Error('Usage: node scripts/probe-sources.js --all | --source <source-id>');
}

async function writeReport(report) {
  await mkdir('dist', { recursive: true });
  await writeFile('dist/source-probe-report.json', `${JSON.stringify(report, null, 2)}\n`);
}

async function main() {
  const now = new Date().toISOString();
  let report;
  try {
    const manifests = JSON.parse(await readFile('data/sources/sources.json', 'utf8'));
    report = await probeSources(selectManifests(manifests, process.argv.slice(2)), { now });
  } catch (error) {
    report = {
      schema_version: 1,
      generated_at: now,
      sources: [],
      summary: { selected: 0, successful: 0, failed: 1, blocked: 0 },
      error: message(error)
    };
  }
  await writeReport(report);
  console.log(JSON.stringify(report.summary));
  if (report.summary.successful === 0) process.exitCode = 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(error => {
    console.error(message(error));
    process.exitCode = 1;
  });
}
