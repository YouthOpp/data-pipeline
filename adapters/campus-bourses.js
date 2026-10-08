import {decode} from 'html-entities';
import {loadPage} from './link-metadata.js';
import {normalizeItem,plainText} from './rss.js';

export const CAMPUS_API='https://bourses-api.campusfrance.org/sgetgrants/en';
export function parseCampusList(data,manifest,now){
 if(data?.op!==1||!Number.isSafeInteger(data.count)||!Array.isArray(data.programs)||data.count!==data.programs.length)throw Error('Campus Bourses response count or success contract is invalid');
 if(!data.count||data.count>5000)throw Error('Campus Bourses catalogue is empty or exceeds its reviewed limit');
 const ids=new Set();
 return data.programs.map(program=>{
  const id=String(program.bourseId??'');
  if(!/^[1-9]\d{0,8}$/.test(id)||ids.has(id))throw Error('Campus Bourses programme ID is missing, invalid or duplicated');
  ids.add(id);
  if(typeof program.title!=='string')throw Error('Campus Bourses programme title is missing');
  const title=plainText(decode(program.title));
  if(!title||title.length>300)throw Error('Campus Bourses programme title is empty or excessive');
  const url=new URL(manifest.source_url);url.hash=`/program/${id}`;
  const item=normalizeItem({title,link:url.href},manifest,now);
  // countryListId is a search/eligibility dimension; it is never a destination.
  // updatedAt is not publication and endAt has not been reviewed as an application deadline.
  return {...item,category:'scholarships',categories:['scholarships'],kind:'programme-overview',classification:{method:'reviewed-public-programme-api',status:'classified',evidence:[CAMPUS_API,url.href]}};
 });
}
export async function collect({manifest,fetchText,now}){
 if(manifest.source!=='fr-campus-france'||new URL(manifest.source_url).origin!=='https://campusbourses.campusfrance.org'||manifest.api_url!==CAMPUS_API)throw Error('Unreviewed Campus Bourses source or API');
 const text=await loadPage({manifest:{...manifest,source_url:CAMPUS_API,reviewed_page_identity:undefined},fetchText});
 let data;try{data=JSON.parse(text);}catch{throw Error('Campus Bourses API did not return valid JSON');}
 return parseCampusList(data,manifest,now);
}
