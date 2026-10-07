import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { collect as collectRSS } from '../adapters/rss.js';
import { collect as collectReviewedRSS } from '../adapters/reviewed-rss.js';
import { collect as collectReviewedHTML } from '../adapters/reviewed-html.js';
import { collect as collectLinkMetadata } from '../adapters/link-metadata.js';
import { categoryIds, classifyRecord, validateClassification, buildCategoricalCatalog, validateCategoricalCatalog } from './taxonomy.js';
export const trustedAdapters = { rss: collectRSS, 'reviewed-rss': collectReviewedRSS, 'reviewed-html': collectReviewedHTML, 'link-metadata': collectLinkMetadata };
export function validateRecord(record) {
  for (const key of ['id','title','url','source','source_url','first_seen_at','last_seen_at','last_checked_at']) if (typeof record[key] !== 'string' || !record[key]) throw new Error(`Missing ${key}`);
  for (const key of ['url','source_url']) if (!['http:','https:'].includes(new URL(record[key]).protocol) || new URL(record[key]).username || new URL(record[key]).password) throw new Error('Unsafe URL');
  for(const key of ['published_at','deadline','created_at','updated_at','first_seen_at','last_seen_at','last_checked_at']) if(record[key]!=null && !Number.isFinite(Date.parse(record[key]))) throw new Error(`Invalid date ${key}`);
  for(const key of ['host_countries','eligible_countries']) if(record[key]?.some(x=>!/^([A-Z]{2})$/.test(x))) throw new Error(`Invalid ISO country ${key}`);
  if (typeof record.summary !== 'string' || record.summary.length > 600 || /<[^>]+>/.test(record.summary)) throw new Error('Invalid plain summary');
  if(!['open','expired','unknown'].includes(record.status))throw new Error('Invalid status');
  for (const key of ['tags','host_countries','eligible_countries']) if (!Array.isArray(record[key]) || record[key].some(x=>typeof x!=='string')) throw new Error(`Invalid ${key}`);
  if (!categoryIds.includes(record.category)) throw new Error('Invalid category');
  if (record.categories) validateClassification(record);
}
export async function fetchText(url) {
  if(new URL(url).protocol !== 'https:' || new URL(url).username || new URL(url).password || /^(localhost|127\.|0\.|10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.|169\.254\.|\[)/.test(new URL(url).hostname)) throw new Error('HTTPS source required');
  const response = await fetch(url,{signal:AbortSignal.timeout(25000),redirect:'error',headers:{'User-Agent':'YouthOpp/1.0 (+https://github.com/YouthOpp/data-pipeline)'}});
  if(!response.ok) throw new Error(`HTTP ${response.status}`);
  const reader=response.body.getReader();let size=0;const chunks=[];
  while(true){const {done,value}=await reader.read();if(done)break;size+=value.length;if(size>5_000_000){await reader.cancel();throw new Error('Feed exceeds 5 MB');}chunks.push(value);}
  return Buffer.concat(chunks).toString('utf8');
}
export async function runPipeline(manifests,previous={opportunities:[],sources:[]},{now=new Date().toISOString(),load=fetchText,adapter,registry=[]}={}) {
  const enabled=manifests.filter(m=>m.enabled);const records=new Map((previous.opportunities||[]).map(r=>{ const migrated=classifyRecord({...r,summary:'',updated_at:r.summary?now:r.updated_at},manifests.find(m=>m.source===r.source)); validateRecord(migrated); return [r.id,migrated]; }));const sources=[];let successes=0;
  async function collectManifest(manifest){
    const old=(previous.sources||[]).find(s=>s.source===manifest.source);
    try {
      if(manifest.collection_blocked_reason)throw new Error(`Collection blocked: ${manifest.collection_blocked_reason}`);
      if(!Object.hasOwn(trustedAdapters,manifest.adapter))throw new Error('Custom adapters require explicit trusted registration in scripts/collect.js');
      const items=(await (adapter||trustedAdapters[manifest.adapter])({manifest,fetchText:load,now})).map(record=>classifyRecord(record,manifest));if(!items.length)throw new Error('Empty adapter output');
      const batchIds=new Set();for(const record of items){validateRecord(record);if(batchIds.has(record.id))throw new Error('Duplicate adapter record ID');batchIds.add(record.id);}
      for(const record of items){const prior=records.get(record.id);const content=r=>JSON.stringify(Object.fromEntries(Object.entries(r).filter(([k])=>!['created_at','updated_at','first_seen_at','last_seen_at','last_checked_at'].includes(k))));records.set(record.id,{...record,created_at:prior?.created_at||now,first_seen_at:prior?.first_seen_at||now,updated_at:prior && content(prior)===content(record)?prior.updated_at:now});}
      sources.push({...manifest,last_attempt_at:now,last_checked_at:now,last_success_at:now,status:'ok',record_count:items.length,error:null});successes++;
    }catch(error){sources.push({...manifest,last_attempt_at:now,last_checked_at:old?.last_checked_at||null,last_success_at:old?.last_success_at||null,status:'error',record_count:old?.record_count||0,error:String(error.message).slice(0,200)});}
  }
  // Independent hosts can run together; each host is serialized with conservative spacing.
  const groups=new Map();for(const manifest of enabled){const host=new URL(manifest.source_url).hostname;if(!groups.has(host))groups.set(host,[]);groups.get(host).push(manifest);}
  const queue=[...groups.values()];let nextGroup=0;
  await Promise.all(Array.from({length:Math.min(4,queue.length)},async()=>{while(nextGroup<queue.length){const group=queue[nextGroup++];for(let i=0;i<group.length;i++){if(i && !adapter && !group[i].collection_blocked_reason)await new Promise(resolve=>setTimeout(resolve,30000));await collectManifest(group[i]);}}}));
  const sourceOrder=new Map(enabled.map((manifest,index)=>[manifest.source,index]));sources.sort((a,b)=>sourceOrder.get(a.source)-sourceOrder.get(b.source));
  if(!successes)throw new Error('All enabled sources failed; catalog must not be published');
  const active=new Set(enabled.map(m=>m.source));
  const opportunities=[...records.values()].filter(r=>active.has(r.source)).map(r=>({...r,status:r.deadline ? (Date.parse(r.deadline)<Date.parse(now)?'expired':'open'):'unknown'})).sort((a,b)=>(b.published_at||'').localeCompare(a.published_at||'')||a.id.localeCompare(b.id));
  const result={schema_version:1,model_version:2,generated_at:now,opportunities,sources,...buildCategoricalCatalog(opportunities,manifests,registry)};validateCategoricalCatalog(result);return result;
}
async function main(){
 const manifests=JSON.parse(await readFile('data/sources/sources.json','utf8'));let previous;
 if(process.env.PREVIOUS_CATALOG)previous=JSON.parse(await readFile(process.env.PREVIOUS_CATALOG,'utf8'));
 const registry=JSON.parse(await readFile('data/sources/source-registry.json','utf8')).sources;
 const result=await runPipeline(manifests,previous,{registry});await mkdir('dist',{recursive:true});await writeFile('dist/catalog.json',JSON.stringify(result));await writeFile('dist/collection-report.json',JSON.stringify({generated_at:result.generated_at,sources:result.sources},null,2));
 console.log(JSON.stringify({records:result.opportunities.length,sources:result.sources.map(s=>({source:s.source,status:s.status,error:s.error}))},null,2));
}
if(process.argv[1]&&import.meta.url===pathToFileURL(resolve(process.argv[1])).href)main().catch(e=>{console.error(e.message);process.exitCode=1;});
