import { readFile, readdir } from 'node:fs/promises';
import { join } from 'node:path';

// Stored source metadata is the only configuration used by the pipeline.
export async function loadSourceManifests(root = 'data-source') {
  const entries = await readdir(join(root, 'sources'), { withFileTypes: true });
  const names = entries.filter(entry => entry.isDirectory()).map(entry => entry.name).sort();
  if (!names.length) throw new Error('No source metadata found in data-source checkout');
  return Promise.all(names.map(async name => {
    if (!/^[a-z0-9-]+$/.test(name)) throw new Error('Unsafe source directory name');
    const manifest = JSON.parse(await readFile(join(root, 'sources', name, 'metadata.json'), 'utf8'));
    if (manifest.source !== name) throw new Error(`Source metadata identity mismatch: ${name}`);
    return manifest;
  }));
}

export async function loadSourceRegistry(root = 'data-source') {
  const catalog = JSON.parse(await readFile(join(root, 'catalog.json'), 'utf8'));
  if (!Array.isArray(catalog.source_registry)) throw new Error('Missing source registry in data-source catalog');
  return catalog.source_registry;
}
