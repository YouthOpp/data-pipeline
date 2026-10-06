import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { assetMetadata, obsoleteReleases, restoreCatalog, validateManifest, verifyAsset } from '../scripts/release-state.js';

const bytes = Buffer.from('{"opportunities":[]}');
const manifest = { schema_version: 1, generated_at: '2026-10-04T20:00:00Z', release_tag: 'catalog-123-1', assets: { 'catalog.json': assetMetadata(bytes), 'collection-report.json': assetMetadata(Buffer.from('{}')) } };

test('manifest and SHA256 verification reject corruption, missing data and unsafe tags', () => {
  validateManifest(manifest);
  verifyAsset(bytes, manifest.assets['catalog.json']);
  assert.throws(() => verifyAsset(Buffer.from('changed'), manifest.assets['catalog.json']), /integrity/);
  assert.throws(() => verifyAsset(bytes, { ...manifest.assets['catalog.json'], size: 1 }), /integrity/);
  assert.throws(() => validateManifest({ ...manifest, release_tag: 'main' }), /manifest/);
  assert.throws(() => validateManifest({ ...manifest, assets: {} }), /asset/);
});

test('new publication requires contributor integrity while prior catalog restoration stays valid', () => {
  assert.throws(() => validateManifest(manifest, { requireContributors: true }), /contributors.json/);
  const complete = { ...manifest, assets: { ...manifest.assets, 'contributors.json': assetMetadata(Buffer.from('{"contributors":[]}')) } };
  validateManifest(complete, { requireContributors: true });
  assert.throws(() => validateManifest({ ...complete, assets: { ...complete.assets, 'contributors.json': { size: 1, sha256: 'invalid' } } }), /contributors.json/);
});

test('retention crosses page boundaries and preserves latest, unrelated and protected releases', () => {
  const versions = Array.from({ length: 105 }, (_, index) => ({ id: index, tag_name: `catalog-${1000 + index}-1`, created_at: new Date(Date.UTC(2026, 0, 1, 0, index)).toISOString(), draft: false, prerelease: false }));
  const releases = [...versions, { ...versions[0], tag_name: 'catalog-latest' }, { ...versions[0], tag_name: 'v1.0' }, { ...versions[0], tag_name: 'catalog-1-1', draft: true }, { ...versions[0], tag_name: 'catalog-2-1', prerelease: true }];
  const obsolete = obsoleteReleases(releases, 'catalog-1104-1');
  assert.equal(obsolete.length, 75);
  assert.ok(obsolete.includes('catalog-1000-1'));
  assert.ok(!obsolete.includes('catalog-1104-1'));
  assert.ok(!obsolete.includes('catalog-latest'));
  assert.ok(!obsolete.includes('catalog-1-1'));
  assert.equal(obsoleteReleases(releases, 'catalog-1000-1').length, 74);
});

test('restoration captures manifest and downloads verified immutable snapshot', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'youthopp-release-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const commands = [];
  const run = async args => {
    commands.push(args);
    if (args[0] === 'api') return JSON.stringify({ assets: [{ name: 'manifest.json' }] });
    if (args.includes('manifest.json')) await writeFile(join(directory, 'manifest.json'), JSON.stringify(manifest));
    else await writeFile(join(directory, 'catalog.json'), bytes);
    return '';
  };
  assert.equal(await restoreCatalog({ run, directory, repository: 'YouthOpp/data-pipeline' }), `${directory}/catalog.json`);
  assert.equal(commands[2][2], manifest.release_tag);
});

test('migration verifies native GitHub asset digest before preserving older state', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'youthopp-release-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const run = async args => {
    if (args[0] === 'api') return JSON.stringify({ assets: [{ name: 'catalog.json', digest: `sha256:${assetMetadata(bytes).sha256}`, size: bytes.length }] });
    await writeFile(join(directory, 'catalog.json'), bytes);
    return '';
  };
  assert.equal(await restoreCatalog({ run, directory, repository: 'YouthOpp/data-pipeline' }), `${directory}/catalog.json`);
});

test('restoration distinguishes actual missing release from permission/network errors', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'youthopp-release-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const options = { directory, repository: 'YouthOpp/data-pipeline' };
  assert.equal(await restoreCatalog({ ...options, run: async () => { throw Error('HTTP 404'); } }), null);
  await assert.rejects(restoreCatalog({ ...options, run: async () => { throw Error('HTTP 403'); } }), /403/);
  await assert.rejects(restoreCatalog({ ...options, run: async () => { throw Error('connection failed'); } }), /connection/);
  await assert.rejects(restoreCatalog({ ...options, run: async () => JSON.stringify({ assets: [{ name: 'catalog.json' }] }) }), /no verifiable/);
});

test('manifest restoration refuses corrupt bytes and never treats download404 as initial absence', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'youthopp-release-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const options = { directory, repository: 'YouthOpp/data-pipeline' };
  const run = async args => {
    if (args[0] === 'api') return JSON.stringify({ assets: [{ name: 'manifest.json' }] });
    if (args.includes('manifest.json')) await writeFile(join(directory, 'manifest.json'), JSON.stringify(manifest));
    else await writeFile(join(directory, 'catalog.json'), 'corruption');
    return '';
  };
  await assert.rejects(restoreCatalog({ ...options, run }), /integrity/);
  await assert.rejects(restoreCatalog({ ...options, run: async args => args[0] === 'api' ? JSON.stringify({ assets: [{ name: 'manifest.json' }] }) : Promise.reject(Error('HTTP 404 download')) }), /404 download/);
});
