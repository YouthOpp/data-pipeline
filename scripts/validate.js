import { classifySource, categoryIds } from './taxonomy.js';
import { sourceManifests,trustedAdapters } from './collect.js';
import { readFile } from 'node:fs/promises';
const manifests=sourceManifests;
const ids=new Set();
if (manifests.length !== 3 || Object.keys(trustedAdapters).length !== manifests.length) throw Error('Expected one adapter per action');
for (const item of manifests) {
  classifySource(item,true);
  if (ids.has(item.source) || !/^[a-z0-9-]+$/.test(item.source)) throw Error('Duplicate or invalid source');
  ids.add(item.source);
  if (!Object.hasOwn(trustedAdapters,item.source) || item.enabled !== true) throw Error('Missing action adapter');
  if (new URL(item.source_url).protocol !== 'https:') throw Error('Unsafe source URL');
}
console.log(`Validated ${ids.size} sources`);

const schema=JSON.parse(await readFile('schemas/opportunity.schema.json','utf8'));
if(JSON.stringify(schema.properties.category.enum)!==JSON.stringify(categoryIds))throw Error('Schema taxonomy drift');
console.log('Validated action adapter manifests');


