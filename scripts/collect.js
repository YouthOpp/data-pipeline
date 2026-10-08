import { collect as collectAT, manifest as manifestAT } from '../adapters/at-oead-ernst-mach.js';
import { collect as collectBG, manifest as manifestBG } from '../adapters/bg-feba-alumni.js';
import { collect as collectDE, manifest as manifestDE } from '../adapters/de-fulbright-germany.js';
import { titleDestinations } from './geography.js';
import { categoryIds, classifyRecord, validateClassification, buildCategoricalCatalog, validateCategoricalCatalog } from './taxonomy.js';
export const trustedAdapters = Object.freeze({
  [manifestAT.source]: collectAT,
  [manifestBG.source]: collectBG,
  [manifestDE.source]: collectDE
});
export const sourceManifests = Object.freeze([manifestAT, manifestBG, manifestDE]);
export function sourceManifest(sourceId) {
  const manifest = sourceManifests.find(source => source.source === sourceId);
  if (!manifest) throw new Error(`Unknown action source: ${sourceId}`);
  return manifest;
}
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
export async function fetchText(url, request=fetch) {
  for(let attempt=0;attempt<2;attempt++){
    try{return await fetchAttempt(url,request);}
    catch(error){
      if(attempt||!(error instanceof TypeError||error.name==='TimeoutError')||!/fetch failed|timeout|timed out/i.test(error.message))throw error;
      await new Promise(resolve=>setTimeout(resolve,1000));
    }
  }
}
async function fetchAttempt(url, request) {
  if(new URL(url).protocol !== 'https:' || new URL(url).username || new URL(url).password || /^(localhost|127\.|0\.|10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.|169\.254\.|\[)/.test(new URL(url).hostname)) throw new Error('HTTPS source required');
  const origin=new URL(url);const signal=AbortSignal.timeout(25000);let current=origin;let response;
  for(let hop=0;hop<=3;hop++){
    response=await request(current.href,{signal,redirect:'manual',headers:{'User-Agent':'YouthOpp/1.0 (+https://github.com/YouthOpp/data-pipeline)'}});
    if(![301,302,303,307,308].includes(response.status))break;
    await response.body?.cancel();
    if(hop===3)throw Error('Too many source redirects');
    const location=response.headers.get('location');if(!location)throw Error('Source redirect has no location');
    const next=new URL(location,current);
    if(next.protocol!=='https:'||next.username||next.password||next.port!==origin.port||next.hostname.replace(/^www\./,'')!==origin.hostname.replace(/^www\./,''))throw Error('Source redirect leaves its reviewed HTTPS host');
    current=next;
  }
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
      if(!adapter && !Object.hasOwn(trustedAdapters,manifest.source))throw new Error('Unknown action adapter source');
      const items=(await (adapter||trustedAdapters[manifest.source])({manifest,fetchText:load,now})).map(record=>{
        const classified=classifyRecord(record,manifest);
        const geography=classified.host_countries.length||manifest.infer_title_destinations===false?{}:titleDestinations(classified.title,classified.url);
        return geography.host_countries?.length?{...classified,...geography}:classified;
      });if(!items.length)throw new Error('Empty adapter output');
      const batchIds=new Set();for(const record of items){validateRecord(record);if(batchIds.has(record.id))throw new Error('Duplicate adapter record ID');batchIds.add(record.id);}
      // Replace obsolete directory-only rows once their listing adapter succeeds.
      if(['opportunity-html','campus-bourses'].includes(manifest.adapter))for(const [id,record] of records)if(record.source===manifest.source&&record.kind==='unknown'&&record.url===manifest.source_url&&!batchIds.has(id))records.delete(id);
      for(const record of items){const prior=records.get(record.id);const content=r=>JSON.stringify(Object.fromEntries(Object.entries(r).filter(([k])=>!['created_at','updated_at','first_seen_at','last_seen_at','last_checked_at'].includes(k))));records.set(record.id,{...record,created_at:prior?.created_at||now,first_seen_at:prior?.first_seen_at||now,updated_at:prior && content(prior)===content(record)?prior.updated_at:now});}
      sources.push({...manifest,last_attempt_at:now,last_checked_at:now,last_success_at:now,status:'ok',record_count:items.length,error:null});successes++;
    }catch(error){sources.push({...manifest,last_attempt_at:now,last_checked_at:old?.last_checked_at||null,last_success_at:old?.last_success_at||null,status:'error',record_count:old?.record_count||0,error:String(error.message).slice(0,500),...(error.diagnostics?{diagnostics:error.diagnostics}:{})});}
  }
  // A source is processed to completion before the next source starts.
  for(const manifest of enabled)await collectManifest(manifest);
  if(!successes)throw new Error('All enabled sources failed: '+sources.map(s=>s.source+': '+s.error).join('; '));
  const active=new Set(enabled.map(m=>m.source));
  const opportunities=[...records.values()].filter(r=>active.has(r.source)&&!manifests.find(m=>m.source===r.source)?.excluded_record_urls?.includes(r.url)).map(r=>({...r,status:r.deadline ? (Date.parse(r.deadline)<Date.parse(now)?'expired':'open'):'unknown'})).sort((a,b)=>(b.published_at||'').localeCompare(a.published_at||'')||a.id.localeCompare(b.id));
  const result={schema_version:1,model_version:2,generated_at:now,opportunities,sources,...buildCategoricalCatalog(opportunities,manifests,registry)};validateCategoricalCatalog(result);return result;
}
