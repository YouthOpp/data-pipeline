import test from 'node:test';
import assert from 'node:assert/strict';
import {collect,parseCampusList,CAMPUS_API} from '../adapters/campus-bourses.js';
import {runPipeline,validateRecord} from '../scripts/collect.js';

const now='2026-10-08T06:00:00Z';
const manifest={source:'fr-campus-france',source_url:'https://campusbourses.campusfrance.org/?lang=en',website_url:'https://campusbourses.campusfrance.org/',api_url:CAMPUS_API,adapter:'campus-bourses',enabled:true,language:'en',publisher_country:'FR',infer_title_destinations:false};
const program={bourseId:255,title:'Embassy in Canada: Master &amp; PhD grants',synthese:'Publisher prose must not be stored',countryListId:'1,2',updatedAt:'2026-10-01',endAt:'2027-01-01'};
const data={op:1,count:1,programs:[program]};
test('the public catalogue API yields every programme and keeps title, stable original route and unknown eligibility',()=>{
 const records=parseCampusList(data,manifest,now);validateRecord(records[0]);
 assert.equal(records[0].title,'Embassy in Canada: Master & PhD grants');assert.equal(records[0].url,'https://campusbourses.campusfrance.org/?lang=en#/program/255');
 assert.equal(records[0].kind,'programme-overview');assert.equal(records[0].summary,'');assert.equal(records[0].deadline,null);assert.equal(records[0].published_at,null);assert.deepEqual(records[0].eligible_countries,[]);assert.deepEqual(records[0].host_countries,[]);assert.ok(!JSON.stringify(records).includes(program.synthese));
 const large={op:1,count:381,programs:Array.from({length:381},(_,i)=>({...program,bourseId:i+1}))};assert.equal(parseCampusList(large,manifest,now).length,381);
});
test('API errors, truncated catalogues and duplicate IDs refuse partial source success',()=>{
 for(const invalid of [{...data,op:0},{...data,count:2},{...data,count:0,programs:[]},{op:1,count:2,programs:[program,program]},{...data,programs:[{...program,bourseId:null}]},{...data,programs:[{...program,title:null}]}])assert.throws(()=>parseCampusList(invalid,manifest,now));
});
test('the reviewed API observes robots and fails on denied access or invalid JSON',async()=>{
 const calls=[];const fetchText=async url=>{calls.push(url);return url.endsWith('/robots.txt')?'User-agent: *\nAllow: /':JSON.stringify(data);};
 assert.equal((await collect({manifest,fetchText,now})).length,1);assert.deepEqual(calls,['https://bourses-api.campusfrance.org/robots.txt',CAMPUS_API]);
 await assert.rejects(collect({manifest,now,fetchText:async()=> 'User-agent: *\nDisallow: /'}),/disallows/);
 await assert.rejects(collect({manifest,now,fetchText:async url=>url.endsWith('/robots.txt')?'':'not-json'}),/valid JSON/);
 await assert.rejects(collect({manifest:{...manifest,api_url:'https://other.example/'},now,fetchText}),/Unreviewed/);
});
test('publisher/search country and embassy location never become an API programme destination',async()=>{
 const catalog=await runPipeline([manifest],undefined,{now,adapter:async()=>parseCampusList(data,manifest,now)});
 assert.deepEqual(catalog.opportunities[0].host_countries,[]);
});

test('successful API migration withdraws its old publisher directory while outages preserve it',async()=>{
 const {normalizeItem}=await import('../adapters/rss.js');
 const old=normalizeItem({title:'CampusBourses catalogue',link:manifest.source_url},manifest,now);
 const previous={opportunities:[old],sources:[]};
 const catalog=await runPipeline([manifest],previous,{now,adapter:async()=>parseCampusList(data,manifest,now)});
 assert.equal(catalog.opportunities.length,1);assert.equal(catalog.opportunities[0].kind,'programme-overview');
 const other={...manifest,source:'other',adapter:'rss',source_url:'https://other.example/feed'};
 const outage=await runPipeline([manifest,other],previous,{now,adapter:async({manifest:m})=>{if(m.source===manifest.source)throw Error('Outage');return [normalizeItem({title:'Other programme',link:'https://other.example/item'},m,now)];}});
 assert.ok(outage.opportunities.some(r=>r.id===old.id));
});
