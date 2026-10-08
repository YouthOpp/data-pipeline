import { classifySource, taxonomy, categoryIds } from './taxonomy.js';
import { readFile } from 'node:fs/promises';
const manifests=JSON.parse(await readFile('data/sources/sources.json','utf8'));const ids=new Set();
for(const item of manifests){
 classifySource(item,true);
 if(!/^[a-z0-9-]+$/.test(item.source)||ids.has(item.source))throw new Error('Invalid or duplicate source');ids.add(item.source);
 for(const field of ['source_url','website_url'])if(new URL(item[field]).protocol!=='https:' || new URL(item[field]).username || new URL(item[field]).password || /^(localhost|127\.|0\.|10\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.|169\.254\.|\[)/.test(new URL(item[field]).hostname))throw new Error('Source requires HTTPS');
 if(typeof item.enabled!=='boolean'||!['rss','reviewed-rss','reviewed-html','link-metadata','opportunity-html','campus-bourses','opportunitydesk'].includes(item.adapter)||typeof item.language!=='string')throw new Error('Invalid adapter manifest');
 if(item.adapter==='opportunitydesk' && (item.source!=='opportunitydesk'||new URL(item.source_url).origin!=='https://opportunitydesk.org'))throw Error('Invalid Opportunity Desk source');
 if(item.adapter==='campus-bourses'&&(item.source!=='fr-campus-france'||new URL(item.source_url).origin!=='https://campusbourses.campusfrance.org'||item.api_url!=='https://bourses-api.campusfrance.org/sgetgrants/en'))throw Error('Invalid reviewed Campus Bourses API');
 if(item.infer_title_destinations!=null&&typeof item.infer_title_destinations!=='boolean')throw Error('Invalid destination inference configuration');
 if(item.adapter==='opportunity-html'){
  if(!item.research_source_id)throw Error('Opportunity HTML requires a research source link');
  if(item.listing_path_pattern){if(item.listing_path_pattern.length>200)throw Error('Listing pattern too long');new RegExp(item.listing_path_pattern);}
  if(item.listing_exclude_title_pattern){if(typeof item.listing_exclude_title_pattern!=='string'||item.listing_exclude_title_pattern.length>200)throw Error('Invalid excluded listing title pattern');new RegExp(item.listing_exclude_title_pattern);}
  if(item.listing_title_rules){if(typeof item.listing_title_rules!=='object'||Array.isArray(item.listing_title_rules)||!Object.keys(item.listing_title_rules).length)throw Error('Invalid reviewed listing title rules');for(const [category,pattern] of Object.entries(item.listing_title_rules)){if(!categoryIds.includes(category)||typeof pattern!=='string'||pattern.length>200)throw Error('Invalid reviewed listing title rule');new RegExp(pattern);}}
  if(item.listing_allowed_hosts&&(!Array.isArray(item.listing_allowed_hosts)||item.listing_allowed_hosts.some(h=>!/^([a-z0-9-]+\.)+[a-z]{2,}$/.test(h)||/^(localhost|127\.|10\.|192\.168\.)/.test(h))))throw Error('Invalid reviewed listing host');
  if(item.listing_allow_pdf!=null&&typeof item.listing_allow_pdf!=='boolean')throw Error('Invalid PDF listing permission');
  if(item.reviewed_links){if(!Array.isArray(item.reviewed_links)||item.reviewed_links.some(r=>!r.title||r.title.length>300||new URL(r.url).protocol!=='https:'||new URL(r.url).username||new URL(r.url).password||!categoryIds.includes(r.category)||!r.reviewed_at))throw Error('Invalid reviewed programme link');}
  if(item.reviewed_links_only!=null&&(typeof item.reviewed_links_only!=='boolean'||!item.reviewed_links?.length))throw Error('Reviewed-only extraction requires programme links');
  if(item.reviewed_sections){if(!Array.isArray(item.reviewed_sections)||!item.reviewed_sections.length||new Set(item.reviewed_sections.map(r=>r.title)).size!==item.reviewed_sections.length||item.reviewed_sections.some(r=>!r.title||r.title.length>300||!categoryIds.includes(r.category)||!r.reviewed_at))throw Error('Invalid reviewed programme sections');}
  if(item.diagnostic_scripts&&(!Array.isArray(item.diagnostic_scripts)||item.diagnostic_scripts.length>2||item.diagnostic_scripts.some(s=>new URL(s).protocol!=='https:'||new URL(s).origin!==new URL(item.source_url).origin)))throw Error('Diagnostic scripts must stay on the reviewed source origin');
  if(item.reviewed_opportunity){const r=item.reviewed_opportunity;if(r.url!==item.source_url||!categoryIds.includes(r.category)||!['programme-overview','institutional-grant','opportunity'].includes(r.kind))throw Error('Invalid reviewed opportunity selection');if(r.host_countries&&(!Array.isArray(r.host_countries)||r.host_countries.some(x=>!/^([A-Z]{2})$/.test(x))||!r.location_evidence))throw Error('Reviewed destination requires evidence');}
 }
 if(item.collection_blocked_reason!=null && (typeof item.collection_blocked_reason!=='string'||!item.collection_blocked_reason.trim()))throw new Error('Invalid collection block reason');
 if(item.excluded_record_urls&&(!Array.isArray(item.excluded_record_urls)||item.excluded_record_urls.some(u=>new URL(u).protocol!=='https:')))throw Error('Invalid excluded record URL');
 if(item.reviewed_page_identity){const r=item.reviewed_page_identity;if(!r.heading||!r.reviewed_at||![r.canonical_url,r.og_url].filter(Boolean).length)throw Error('Invalid reviewed page identity');for(const url of [r.canonical_url,r.og_url].filter(Boolean))if(new URL(url).origin!==new URL(item.source_url).origin)throw Error('Reviewed page identity must stay on source origin');}
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
