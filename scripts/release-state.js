import { createHash } from 'node:crypto';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const execute = promisify(execFile);
const versionTag = /^catalog-\d+-\d+$/;
const assetNames = ['catalog.json', 'collection-report.json', 'contributors.json'];
export function assetMetadata(bytes) {
  return { sha256: createHash('sha256').update(bytes).digest('hex'), size: bytes.length };
}
export function validateManifest(manifest, { requireContributors = false } = {}) {
  if (manifest?.schema_version !== 1 || !versionTag.test(manifest.release_tag) || !Number.isFinite(Date.parse(manifest.generated_at))) throw new Error('Invalid release manifest');
  for (const name of assetNames) {
    if (name === 'contributors.json' && !requireContributors && !manifest.assets?.[name]) continue;
    const asset = manifest.assets?.[name];
    if (!asset || !/^[a-f0-9]{64}$/.test(asset.sha256) || !Number.isSafeInteger(asset.size) || asset.size < 1) throw new Error(`Invalid manifest asset: ${name}`);
  }
  return manifest;
}
export function verifyAsset(bytes, expected) {
  const actual = assetMetadata(bytes);
  if (actual.sha256 !== expected.sha256 || actual.size !== expected.size) throw new Error('Catalog integrity verification failed');
}
export function obsoleteReleases(releases, currentTag, keep = 30) {
  if (!versionTag.test(currentTag) || !Number.isSafeInteger(keep) || keep < 1) throw new Error('Invalid retention parameters');
  return releases.filter(release => versionTag.test(release.tag_name) && !release.draft && !release.prerelease)
    .sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id - a.id)
    .slice(keep).filter(release => release.tag_name !== currentTag).map(release => release.tag_name);
}
async function github(args) {
  return (await execute('gh', args, { maxBuffer: 20_000_000 })).stdout;
}
export async function restoreCatalog({ run = github, directory = 'previous', repository = process.env.GH_REPO } = {}) {
  if (!repository) throw new Error('GH_REPO is required');
  await mkdir(directory, { recursive: true });
  let release;
  try {
    release = JSON.parse(await run(['api', `repos/${repository}/releases/tags/catalog-latest`]));
  } catch (error) {
    if (/HTTP 404/.test(String(error.stderr || error.message))) return null;
    throw error;
  }
  const catalogPath = `${directory}/catalog.json`;
  if (release.assets.some(asset => asset.name === 'manifest.json')) {
    await run(['release', 'download', 'catalog-latest', '--pattern', 'manifest.json', '--dir', directory, '--clobber']);
    const manifest = validateManifest(JSON.parse(await readFile(`${directory}/manifest.json`, 'utf8')));
    await run(['release', 'download', manifest.release_tag, '--pattern', 'catalog.json', '--dir', directory, '--clobber']);
    verifyAsset(await readFile(catalogPath), manifest.assets['catalog.json']);
  } else {
    // One-time migration verifies the existing release using GitHub's asset digest.
    const asset = release.assets.find(asset => asset.name === 'catalog.json');
    if (!asset || !/^sha256:[a-f0-9]{64}$/.test(asset.digest || '')) throw new Error('Legacy catalog has no verifiable SHA256 digest');
    await run(['release', 'download', 'catalog-latest', '--pattern', 'catalog.json', '--dir', directory, '--clobber']);
    verifyAsset(await readFile(catalogPath), { sha256: asset.digest.slice(7), size: asset.size });
  }
  return catalogPath;
}
async function main() {
  const [command, tag] = process.argv.slice(2);
  if (command === 'manifest') {
    const catalog = JSON.parse(await readFile('dist/catalog.json', 'utf8'));
    const assets = {};
    for (const name of assetNames) assets[name] = assetMetadata(await readFile(`dist/${name}`));
    const manifest = validateManifest({ schema_version: 1, release_tag: tag, generated_at: catalog.generated_at, assets }, { requireContributors: true });
    await writeFile('dist/manifest.json', JSON.stringify(manifest, null, 2));
  } else if (command === 'restore') {
    const path = await restoreCatalog();
    if (path) await writeFile(process.env.GITHUB_ENV, `PREVIOUS_CATALOG=${path}\n`, { flag: 'a' });
    else console.log('No prior catalog release. First successful collection seeds state.');
  } else if (command === 'prune') {
    const pages = JSON.parse(await github(['api', '--paginate', '--slurp', `repos/${process.env.GH_REPO}/releases?per_page=100`]));
    for (const oldTag of obsoleteReleases(pages.flat(), tag)) {
      await github(['release', 'delete', oldTag, '--yes', '--cleanup-tag']);
      console.log(`Removed old catalog snapshot ${oldTag}`);
    }
  } else throw new Error('Expected manifest, restore or prune command');
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) main().catch(error => { console.error(error.message); process.exitCode = 1; });
