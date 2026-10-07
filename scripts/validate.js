import { classifySource, taxonomy, categoryIds } from './taxonomy.js';
import { readFile } from 'node:fs/promises';
const manifests=JSON.parse(await readFile('data/sources/sources.json','utf8'));const ids=new Set();
for(const item of manifests){
 classifySource(item,true);
 if(!/^[a-z0-9-]+$/.test(item.source)||ids.has(item.source))throw new Error('Invalid or duplicate source');ids.add(item.source);
 for(const field of ['source_url','website_url'])if(new URL(item[field]).protocol!=='https:' || new URL(item[field]).username || new URL(item[field]).password || /^(localhost|127\.|0\.|10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.|169\.254\.|\[)/.test(new URL(item[field]).hostname))throw new Error('Source requires HTTPS');
 if(typeof item.enabled!=='boolean'||!['rss','reviewed-rss','reviewed-html','link-metadata'].includes(item.adapter)||typeof item.language!=='string')throw new Error('Invalid adapter manifest');
 if(item.collection_blocked_reason!=null && (typeof item.collection_blocked_reason!=='string'||!item.collection_blocked_reason.trim()))throw new Error('Invalid collection block reason');
 if(item.adapter==='link-metadata' && !item.research_source_id)throw new Error('Link metadata requires a research source link');
 if(item.adapter==='reviewed-html') {
  const selected=item.reviewed_page;
  if(!selected || selected.url!==item.source_url || new URL(selected.url).hostname!==new URL(item.website_url).hostname || !['internships','scholarships'].includes(selected.category) || selected.kind!=='programme-overview')throw new Error('Invalid reviewed HTML programme selection');
  if(selected.metadata_format && !['json-ld','open-graph','heading'].includes(selected.metadata_format))throw new Error('Invalid reviewed HTML metadata format');
  if(selected.metadata_format==='heading' && (typeof selected.title!=='string'||!selected.title.trim()||typeof item.attribution!=='string'||!item.attribution.trim()))throw new Error('Reviewed heading requires an exact title and publisher attribution');
 }
 if(item.adapter==='reviewed-rss') {
  if(!Array.isArray(item.reviewed_items)||!item.reviewed_items.length)throw new Error('Reviewed RSS requires explicit item allowlist');
  const reviewedUrls=new Set();
  for(const reviewed of item.reviewed_items) {
   const url=new URL(reviewed.url);
   if(url.protocol!=='https:'||url.username||url.password||url.hostname!==new URL(item.website_url).hostname||reviewedUrls.has(url.href))throw new Error('Invalid reviewed item URL');
   reviewedUrls.add(url.href);
   if(!['scholarships','grants','other'].includes(reviewed.category)||!['programme-overview','institutional-grant'].includes(reviewed.kind))throw new Error('Invalid reviewed item classification');
  }
 }
}
console.log(`Validated ${ids.size} sources`);

const registry=JSON.parse(await readFile('data/sources/source-registry.json','utf8'));
const registryIds=new Set();const joins=new Set();
for(const source of registry.sources){classifySource(source);if(!source.id||registryIds.has(source.id))throw Error('Duplicate research source ID');registryIds.add(source.id);if(source.adapter_source_id){if(!ids.has(source.adapter_source_id)||joins.has(source.adapter_source_id))throw Error('Invalid research adapter join');joins.add(source.adapter_source_id);}}
const schema=JSON.parse(await readFile('schemas/opportunity.schema.json','utf8'));
if(JSON.stringify(schema.properties.category.enum)!==JSON.stringify(categoryIds))throw Error('Schema taxonomy drift');
console.log(`Validated ${registryIds.size} research classifications`);

for(const manifest of manifests)if(manifest.research_source_id&&!registryIds.has(manifest.research_source_id))throw Error('Invalid research source link');
