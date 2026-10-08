import {readFile,writeFile,mkdir,readdir} from 'node:fs/promises';
import {join} from 'node:path';
import {runPipeline,sourceManifest,sourceManifests} from './collect.js';
import {buildCategoricalCatalog,validateCategoricalCatalog} from './taxonomy.js';

// One invocation collects exactly one reviewed source and fails without publishing on errors.
const sourceId=process.argv[2];
const root=process.argv[3]||'data-source';
if(!sourceId||!/^[a-z0-9-]+$/.test(sourceId))throw Error('Supply one safe source ID');
const manifests=sourceManifests;
const manifest=sourceManifest(sourceId);
if(!manifest)throw Error('Source is not enabled');
const priorCatalog=await load(join(root,'catalog.json'),{});
const registry=Array.isArray(priorCatalog.source_registry)?priorCatalog.source_registry:[];
const directory=join(root,'sources',sourceId);
async function load(file,fallback){try{return JSON.parse(await readFile(file,'utf8'));}catch(e){if(e.code==='ENOENT')return fallback;throw e;}}
const old=await load(join(directory,'opportunities.json'),[]);
const previous={opportunities:old,sources:[await load(join(directory,'metadata.json'),{})]};
const collected=await runPipeline([manifest],previous,{registry});
const records=collected.opportunities;
const metadata=collected.sources[0];
if(metadata.status!=='ok'||!records.length)throw Error('No valid records; previous data remains unchanged');
await mkdir(directory,{recursive:true});
await writeFile(join(directory,'opportunities.json'),JSON.stringify(records,null,2)+'\n');
await writeFile(join(directory,'metadata.json'),JSON.stringify(metadata,null,2)+'\n');
const folders=(await readdir(join(root,'sources'),{withFileTypes:true})).filter(e=>e.isDirectory()).map(e=>e.name).sort();
const allSources=[],allRecords=[];
for(const id of folders){
 if(!manifests.some(manifest=>manifest.source===id&&manifest.enabled))continue;
 const source=await load(join(root,'sources',id,'metadata.json'),null);
 const items=await load(join(root,'sources',id,'opportunities.json'),null);
 if(!source||!Array.isArray(items))continue;
 allSources.push(source);allRecords.push(...items);
}
const unique=new Map();
for(const record of allRecords)unique.set(record.id,record);
const opportunities=[...unique.values()].sort((a,b)=>(b.published_at||'').localeCompare(a.published_at||'')||a.id.localeCompare(b.id));
const catalog={schema_version:1,model_version:2,generated_at:new Date().toISOString(),opportunities,sources:allSources,...buildCategoricalCatalog(opportunities,allSources,registry)};
validateCategoricalCatalog(catalog);
await writeFile(join(root,'catalog.json'),JSON.stringify(catalog,null,2)+'\n');
console.log(JSON.stringify({source:sourceId,records:records.length,total:opportunities.length}));
