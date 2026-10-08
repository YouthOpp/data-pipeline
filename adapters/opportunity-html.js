import {decode} from 'html-entities';
import {loadPage,collect as collectMetadata} from './link-metadata.js';
import {normalizeItem,plainText} from './rss.js';
import {createHash} from 'node:crypto';

const rules={
 internships:/\b(?:internships?|stage|tirocini?o|praktikum)\b/i,
 scholarships:/\b(?:scholarships?|borse?\s+(?:di\s+)?studio|bourses?|stipendi\w*|burslar|beurs\w*)\b/i,
 volunteering:/\b(?:volunteer\w*|volontari\w*|solidariet[aà]|solidarity|\bESC\b|service civique)\b/i,
 fellowships:/\bfellowships?\b/i,
 training:/\b(?:training course|corso|scambio culturale|summer school|exchange|mobility|ceepus)\b/i,
 jobs:/\b(?:jobs?|lavoro|recruitment|vacanc\w*)\b/i,
 grants:/\b(?:grants?|funding|funded call)\b/i,
 competitions:/\b(?:competition\w*|concorso|concorsi|contest\w*|prize|hack\w*)\b/i
};
export function extractListing(html,manifest,now){
 const main=html.match(/<main\b[^>]*>([\s\S]*?)<\/main\s*>/i)?.[1]||html;
 const content=main.replace(/<!--[^]*?-->/g,'').replace(/<(script|style|nav|footer)\b[^>]*>[^]*?<\/\1\s*>/gi,'');
 const records=new Map();
 for(const selected of manifest.reviewed_links||[]){
  const found=[...html.matchAll(/<a\b([^>]*?)>([\s\S]*?)<\/a\s*>/gi)].some(m=>{
   const href=m[1].match(/\bhref\s*=\s*(["'])(.*?)\1/i)?.[2];if(!href)return false;
   try{return new URL(decode(href),manifest.source_url).href===selected.url&&plainText(decode(m[2]))===selected.title;}catch{return false;}
  });
  if(!found)continue;
  const item=normalizeItem({title:selected.title,link:selected.url},manifest,now);
  records.set(item.id,{...item,category:selected.category,categories:[selected.category],kind:'programme-overview',classification:{method:'reviewed-live-programme-link',status:'classified',evidence:[manifest.source_url,selected.url]}});
 }
 const candidates=[...content.matchAll(/<a\b([^>]*?)>([\s\S]*?)<\/a\s*>/gi)].map(m=>({attributes:m[1],label:m[2].match(/<h[2-4]\b[^>]*>([\s\S]*?)<\/h[2-4]\s*>/i)?.[1]||m[2],heading:false}));
 // Publisher cards often put the programme title in a heading and use a generic link label.
 // Stop at the next heading so that a call never inherits a neighbouring card's link.
 for(const m of content.matchAll(/<h([2-4])\b[^>]*>([\s\S]*?)<\/h\1\s*>([\s\S]*?)(?=<h[1-6]\b|$)/gi)){
  const link=m[3].slice(0,4000).match(/<a\b([^>]*?)>([\s\S]*?)<\/a\s*>/i);
  if(link && /^(?:learn more|read more|apply(?: now)?|more|details|siit|lisainfo)$/i.test(plainText(decode(link[2]))))candidates.push({attributes:link[1],label:m[2],heading:true});
 }

 for(const match of candidates){
  const href=match.attributes.match(/\bhref\s*=\s*(["'])(.*?)\1/i)?.[2];if(!href)continue;
  let url;try{url=new URL(decode(href),manifest.source_url);}catch{continue;}
  if(url.protocol!=='https:'||url.username||url.password||url.port||!(url.origin===new URL(manifest.source_url).origin||manifest.listing_allowed_hosts?.includes(url.hostname))||url.hash||url.href===manifest.source_url)continue;
  if(/\.(?:jpg|jpeg|png|zip)$/i.test(url.pathname)||(/\.pdf$/i.test(url.pathname)&&!manifest.listing_allow_pdf)||/\/(?:category|tag|page|author|contact|privacy|terms|about|feed)(?:\/|$)/i.test(url.pathname))continue;
  const title=plainText(decode(match.label,{level:'html5'}));
  if(title.length<(match.heading?12:25)||title.length>300||/^(?:all |tutte |read more|learn more|how to |guide |news |newsletter)/i.test(title))continue;
  if(manifest.listing_exclude_title_pattern&&new RegExp(manifest.listing_exclude_title_pattern,'i').test(title))continue;
  const selectedRules=manifest.listing_title_rules?Object.fromEntries(Object.entries(manifest.listing_title_rules).map(([category,pattern])=>[category,new RegExp(pattern,'i')])):rules;
  const categories=Object.entries(selectedRules).filter(([,rule])=>rule.test(title)).map(([category])=>category);
  if(!categories.length)continue;
  // Listing scope is reviewed per publisher; an anchor must identify a distinct call.
  if(manifest.listing_path_pattern&&!new RegExp(manifest.listing_path_pattern).test(url.pathname))continue;
  const item=normalizeItem({title,link:url.href},manifest,now);
  const record={...item,category:categories[0],categories,kind:'opportunity',classification:{method:'reviewed-listing-title',status:'classified',evidence:[manifest.source_url,url.href]}};
  const existing=records.get(record.id);if(!existing||(!manifest.reviewed_links?.some(s=>s.url===record.url)&&record.title.length<existing.title.length))records.set(record.id,record);
  if(records.size>500)throw Error('Listing exceeds 500 records; source needs a paginated adapter');
 }
 return [...records.values()];
}
export async function collect({manifest,fetchText,now}){
 if(manifest.reviewed_sections){
  const html=(await loadPage({manifest,fetchText})).replace(/<(script|style|nav|footer)\b[^>]*>[^]*?<\/\1\s*>/gi,'');
  const headings=[...html.matchAll(/<h[1-4]\b[^>]*>([\s\S]*?)<\/h[1-4]\s*>/gi)].map(m=>plainText(decode(m[1])));
  return manifest.reviewed_sections.map(selected=>{
   if(!headings.includes(selected.title))throw Error(`Reviewed programme section is missing: ${selected.title}`);
   const record=normalizeItem({title:selected.title,link:manifest.source_url},manifest,now);
   return {...record,id:createHash('sha256').update(`${manifest.source}|${manifest.source_url}|section:${selected.title}`).digest('hex').slice(0,24),kind:'programme-overview',category:selected.category,categories:[selected.category],classification:{method:'reviewed-live-programme-section',status:'classified',evidence:[manifest.source_url,selected.title]}};
  });
 }
 if(manifest.reviewed_opportunity){
  const selected=manifest.reviewed_opportunity;
  let items;
  if(selected.title){
   const html=(await loadPage({manifest,fetchText})).replace(/<(script|style|nav|footer)\b[^>]*>[^]*?<\/\1\s*>/gi,'');
   const headings=[...html.matchAll(/<h[1-3]\b[^>]*>([\s\S]*?)<\/h[1-3]\s*>/gi)].map(m=>plainText(decode(m[1])));
   if(!headings.includes(selected.title)){const error=Error('Reviewed programme title is missing from its live page');error.diagnostics={headings:headings.slice(0,30)};throw error;}
   items=[normalizeItem({title:selected.title,link:selected.url},manifest,now)];
  }else items=await collectMetadata({manifest:{...manifest,title_fallback:true},fetchText,now});
  return items.map(r=>({...r,category:selected.category,categories:[selected.category],kind:selected.kind,classification:{method:'reviewed-exact-programme',status:'classified',evidence:[selected.url]},...(selected.host_countries?{host_countries:selected.host_countries,country_evidence:selected.host_countries.map(country=>({country,method:'reviewed-programme-location',url:selected.url,text:selected.location_evidence}))}:{})}));
 }
 const html=await loadPage({manifest,fetchText});
 const items=extractListing(html,manifest,now);
 if(!items.length){
  const error=Error('No opportunity listings extracted; publisher directory is not an opportunity');
  // Short original titles/links only, never publisher article prose, for actionable failure review.
  error.diagnostics={headings:[...html.matchAll(/<h[1-4]\b[^>]*>([\s\S]*?)<\/h[1-4]\s*>/gi)].map(m=>plainText(decode(m[1])).slice(0,180)).filter(Boolean).slice(0,60),links:[...html.matchAll(/<a\b[^>]*href=["']([^"']+)["'][^>]*>([\s\S]*?)<\/a\s*>/gi)].map(m=>({url:decode(m[1]),title:plainText(decode(m[2])).slice(0,180)})).filter(m=>Object.values(rules).some(r=>r.test(m.title))).slice(0,60)};
  const resourceUrl=value=>{try{const url=new URL(decode(value),manifest.source_url);if(url.protocol!=='https:')return null;url.search='';url.hash='';return url.href;}catch{return null;}};
  error.diagnostics.page_bytes=Buffer.byteLength(html);
  error.diagnostics.scripts=[...html.matchAll(/<script\b[^>]*src=["']([^"']+)["']/gi)].map(m=>resourceUrl(m[1])).filter(Boolean).slice(0,20);
  error.diagnostics.frames=[...html.matchAll(/<iframe\b[^>]*src=["']([^"']+)["']/gi)].map(m=>resourceUrl(m[1])).filter(Boolean).slice(0,10);
  if(manifest.diagnostic_scripts){
   error.diagnostics.application_paths=[];
   for(const script of manifest.diagnostic_scripts){
    try{
     // Read explicitly reviewed public application code; never execute it or collect its prose.
     await new Promise(resolve=>setTimeout(resolve,30000));
     const code=await loadPage({manifest:{...manifest,source_url:script},fetchText});
     const paths=[...code.matchAll(/["']([^"'\s<>]{1,200})["']/g)].map(m=>m[1].split(/[?#]/)[0]).filter(p=>/api|grant|scholar|bourse|search|filter|rest|country/i.test(p));
     error.diagnostics.application_paths.push({script,paths:[...new Set(paths)].slice(0,100)});
    }catch(probe){error.diagnostics.application_paths.push({script,error:String(probe.message).slice(0,200)});}
   }
  }
  throw error;
 }
 return items;
}
