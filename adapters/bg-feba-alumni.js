export const manifest = Object.freeze({
  "source": "bg-feba-alumni",
  "research_source_id": "bg-feba-alumni",
  "source_url": "https://febalumni.org/news/",
  "website_url": "https://febalumni.org/",
  "enabled": true,
  "adapter": "feba-wordpress",
  "language": "bg/en",
  "default_tags": [],
  "added_at": "2026-10-06",
  "scope": "Extract factual opportunity title/link records from reviewed programme pages or publisher listings; no descriptions, imagery or applicant eligibility inferred.",
  "publisher_country": "BG",
  "publisher_type": "unknown",
  "categories": [
    "scholarships",
    "training",
    "internships",
    "volunteering"
  ],
  "collection_blocked_reason": null
});

import { normalizeItem } from '../scripts/record.js';
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const relevant = /стипенд|scholarship|академи|академия|обучени|certificate|сертификат|training|стаж|internship|volunteer|добровол/i;
const call = /обявява конкурс|краен срок|кандидатств|възможност за|записван|academy|академи|training|certificate|покана|отворена/i;
const exclude = /победител|наградени|резултатите от|приключи|връчени|awarded|winners|отличи|партньори на представянето|проведе се|се проведе/i;
const explicitCall = /обявява конкурс|краен срок|кандидатств|възможност за|записван|покана за кандидатстване|прием на кандидатури|applications? open|apply now|open call/i;
const text = value => String(value||'').replace(/<[^>]*>/g,' ').replace(/&#(\d+);/g,(_,n)=>String.fromCodePoint(Number(n))).replace(/&amp;/g,'&').replace(/&quot;/g,'"').replace(/&nbsp;/g,' ').replace(/\s+/g,' ').trim();
export function robotRules(value,path) {
  const groups=[]; let group={agents:[],rules:[],delay:0};
  for(const line of value.split(/\r?\n/)){
    const match=line.replace(/\s+#.*$/,'').trim().match(/^([^:]+):\s*(.*)$/);
    if(!match)continue;
    const directive=match[1].toLowerCase().trim(), rule=match[2].trim();
    if(directive==='user-agent'){
      if(group.rules.length||group.delay){groups.push(group);group={agents:[],rules:[],delay:0};}
      group.agents.push(rule.toLowerCase());
    } else if(directive==='allow'||directive==='disallow')group.rules.push({directive,path:rule});
    else if(directive==='crawl-delay'){if(!/^\d+(?:\.\d+)?$/.test(rule))throw Error('Invalid crawl-delay');group.delay=Number(rule);}
  }
  groups.push(group);
  const matching=groups.filter(g=>g.agents.includes('youthopp')||g.agents.includes('*'));
  const exact=matching.filter(g=>g.agents.includes('youthopp'));
  const selected=exact.length?exact:matching;
  const rules=selected.flatMap(g=>g.rules).filter(r=>r.path&&path.startsWith(r.path.split('*')[0].replace(/\$$/,''))).sort((a,b)=>b.path.length-a.path.length||(a.directive==='allow'?-1:1));
  return {allowed:!rules.length||rules[0].directive==='allow',delay:Math.max(0,...selected.map(g=>g.delay))};
}
export async function collect({manifest: selectedManifest = manifest,fetchText,now}){
  if (selectedManifest.source !== manifest.source) throw Error('Source mismatch');
  const origin=new URL(manifest.source_url);
  if(origin.hostname.replace(/^www\./,'')!=='febalumni.org')throw Error('FEBA host mismatch');
  const robots=await fetchText(new URL('/robots.txt',origin).href).catch(e=>{if(e.message==='HTTP 404')return '';throw e;});
  const policy=robotRules(robots,'/wp-json/wp/v2/posts');
  if(!policy.allowed||policy.delay>30)throw Error('FEBA robots policy rejects API collection');
  const gap=Math.max(6100,policy.delay*1000);
  const records=new Map();let examined=0;
  for(let page=1;page<=25;page++){
    await pause(gap);
    const endpoint=new URL('/wp-json/wp/v2/posts',origin);
    endpoint.searchParams.set('per_page','100');
    endpoint.searchParams.set('page',String(page));
    endpoint.searchParams.set('_fields','id,date_gmt,link,title,content');
    let posts;try{posts=JSON.parse(await fetchText(endpoint.href));}catch(e){throw Error('FEBA WordPress API failed: '+e.message);}
    if(!Array.isArray(posts))throw Error('Unexpected FEBA API payload');
    examined+=posts.length;
    for(const post of posts){
      const title=text(post.title?.rendered);
      const snippet=text(post.content?.rendered).slice(0,500);
      if(!relevant.test(title)||!explicitCall.test(title)||exclude.test(title))continue;
      let url;try{url=new URL(post.link);}catch{continue;}
      if(url.protocol!=='https:'||url.hostname.replace(/^www\./,'')!=='febalumni.org'||!/^\/\d{4}\/\d{2}\/\d{2}\//.test(url.pathname))continue;
      const category=/стипенд|scholarship/i.test(title)?'scholarships':/академи|обучени|certificate|сертификат|training/i.test(title)?'training':/стаж|internship/i.test(title)?'internships':/volunteer|добровол/i.test(title)?'volunteering':'other';
      if(category==='other')continue;
      const publishedAt = typeof post.date_gmt === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(post.date_gmt) && Number.isFinite(Date.parse(post.date_gmt + 'Z')) ? new Date(post.date_gmt + 'Z').toISOString() : null;
      const record=normalizeItem({title,link:url.href,isoDate:publishedAt||undefined},manifest,now);
      records.set(record.id,{...record,category,categories:[category],kind:'opportunity',tags:[category],published_at:publishedAt,host_countries:['BG'],classification:{method:'feba-publisher-api',status:'classified',evidence:[url.href]}});
    }
    if(posts.length<100){
      if(!records.size)throw Error('FEBA returned no eligible opportunity records');
      console.log(JSON.stringify({source:manifest.source,posts:examined,opportunities:records.size,pages:page,complete:true}));
      return [...records.values()];
    }
  }
  throw Error('FEBA source exceeds safe pagination limit');
}
