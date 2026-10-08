import {readFile,writeFile,appendFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {validateRecord} from './collect.js';
import {loadSourceManifests} from './source-store.js';
import {validateCategoricalCatalog} from './taxonomy.js';

export function inspectCoverage(catalog,manifests){
 validateCategoricalCatalog(catalog);
 for(const record of catalog.opportunities)validateRecord(record);
 const enabled=manifests.filter(s=>s.enabled);
 if(catalog.sources.length!==enabled.length)throw Error('Collection report does not cover every enabled source');
 const seen=new Set();
 const sources=enabled.map(manifest=>{
  const report=catalog.sources.find(s=>s.source===manifest.source);
  if(!report||seen.has(report.source))throw Error('Missing or duplicate collection source');seen.add(report.source);
  const records=catalog.opportunities.filter(r=>r.source===manifest.source);
  const listings=records.filter(r=>r.kind!=='unknown');
  const fresh=listings.filter(r=>r.last_checked_at===catalog.generated_at);
  return {source:manifest.source,country:manifest.publisher_country||null,adapter:manifest.adapter,status:report.status,error:report.error||null,records:records.length,listing_records:listings.length,fresh_listing_records:fresh.length,retained_listing_records:listings.length-fresh.length,metadata_only_records:records.length-listings.length,coverage:report.status!=='ok'?'collection_error':fresh.length?'collected':'no_opportunity_records'};
 });
 return {generated_at:catalog.generated_at,total_sources:sources.length,collected_sources:sources.filter(s=>s.coverage==='collected').length,failed_sources:sources.filter(s=>s.coverage==='collection_error').length,metadata_only_sources:sources.filter(s=>s.coverage==='no_opportunity_records').length,total_records:catalog.opportunities.length,listing_records:sources.reduce((n,s)=>n+s.listing_records,0),all_sources_have_opportunities:sources.every(s=>s.coverage==='collected'),sources};
}
async function main(){
 const input=process.argv[2]||'dist/catalog.json';
 const catalog=JSON.parse(await readFile(input,'utf8'));
 const manifests=await loadSourceManifests(process.argv[3]||'data-source');
 const coverage=inspectCoverage(catalog,manifests);
 await writeFile('dist/collection-report.json',JSON.stringify({generated_at:catalog.generated_at,sources:catalog.sources,coverage},null,2)+'\n');
 console.log(JSON.stringify({...coverage,sources:undefined}));
 if(process.env.GITHUB_STEP_SUMMARY){const rows=coverage.sources.map(s=>`| ${s.source} | ${s.coverage} | ${s.fresh_listing_records} | ${s.retained_listing_records} | ${(s.error||'').replace(/[|\r\n]/g,' ')} |`).join('\n');await appendFile(process.env.GITHUB_STEP_SUMMARY,`## Catalog acceptance\n\n${coverage.collected_sources}/${coverage.total_sources} sources yielded freshly collected opportunity/programme records. ${coverage.failed_sources} collection errors; ${coverage.metadata_only_sources} sources yielded only metadata. Complete coverage: **${coverage.all_sources_have_opportunities}**.\n\n| Source | Coverage | Fresh listings | Retained listings | Error |\n|---|---|---:|---:|---|\n${rows}\n`);}
 if(process.env.REQUIRE_ALL_SOURCES==='1'&&!coverage.all_sources_have_opportunities)throw Error('Complete opportunity coverage has not been achieved');
}
if(process.argv[1]&&import.meta.url===pathToFileURL(resolve(process.argv[1])).href)main().catch(error=>{console.error(error.message);process.exitCode=1;});
