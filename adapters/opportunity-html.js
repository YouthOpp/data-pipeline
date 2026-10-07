import {decode} from 'html-entities';
import {loadPage,collect as collectMetadata} from './link-metadata.js';
import {normalizeItem,plainText} from './rss.js';

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
 const content=html.replace(/<!--[^]*?-->/g,'').replace(/<(script|style|nav|header|footer)\b[^>]*>[^]*?<\/\1\s*>/gi,'');
 const records=new Map();
 for(const match of content.matchAll(/<a\b([^>]*?)>([\s\S]*?)<\/a\s*>/gi)){
  const href=match[1].match(/\bhref\s*=\s*(["'])(.*?)\1/i)?.[2];if(!href)continue;
  let url;try{url=new URL(decode(href),manifest.source_url);}catch{continue;}
  if(url.protocol!=='https:'||url.username||url.password||url.origin!==new URL(manifest.source_url).origin||url.hash||url.href===manifest.source_url)continue;
  if(/\.(?:pdf|jpg|jpeg|png|zip)$/i.test(url.pathname)||/\/(?:category|tag|page|author|contact|privacy|terms|about|feed)(?:\/|$)/i.test(url.pathname))continue;
  const title=plainText(decode(match[2],{level:'html5'}));
  if(title.length<25||title.length>300||/^(?:all |tutte |read more|learn more|how to |guide |news |newsletter)/i.test(title))continue;
  const categories=Object.entries(rules).filter(([,rule])=>rule.test(title)).map(([category])=>category);
  if(!categories.length)continue;
  // Listing scope is reviewed per publisher; an anchor must identify a distinct call.
  if(manifest.listing_path_pattern&&!new RegExp(manifest.listing_path_pattern).test(url.pathname))continue;
  const item=normalizeItem({title,link:url.href},manifest,now);
  const record={...item,category:categories[0],categories,kind:'opportunity',classification:{method:'reviewed-listing-title',status:'classified',evidence:[manifest.source_url,url.href]}};
  const existing=records.get(record.id);if(!existing||record.title.length<existing.title.length)records.set(record.id,record);
  if(records.size>=50)break;
 }
 return [...records.values()];
}
export async function collect({manifest,fetchText,now}){
 if(manifest.reviewed_opportunity){
  const items=await collectMetadata({manifest:{...manifest,title_fallback:true},fetchText,now});
  const selected=manifest.reviewed_opportunity;
  return items.map(r=>({...r,category:selected.category,categories:[selected.category],kind:selected.kind,classification:{method:'reviewed-exact-programme',status:'classified',evidence:[selected.url]},...(selected.host_countries?{host_countries:selected.host_countries,country_evidence:selected.host_countries.map(country=>({country,method:'reviewed-programme-location',url:selected.url,text:selected.location_evidence}))}:{})}));
 }
 const items=extractListing(await loadPage({manifest,fetchText}),manifest,now);
 if(!items.length)throw Error('No opportunity listings extracted; publisher directory is not an opportunity');
 return items;
}
