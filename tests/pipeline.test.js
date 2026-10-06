import test from 'node:test';import assert from 'node:assert/strict';import {readFile} from 'node:fs/promises';
import {collect,normalizeItem} from '../adapters/rss.js';import {runPipeline,validateRecord} from '../scripts/collect.js';
const manifest={source:'example',source_url:'https://example.org/feed/',website_url:'https://example.org/',adapter:'rss',enabled:true,language:'en'};const now='2026-10-04T12:00:00.000Z';
test('real RSS fixture yields original links, no copied prose and unknown eligibility',async()=>{const items=await collect({manifest,now,fetchText:()=>readFile(new URL('./fixtures/sample.xml',import.meta.url),'utf8')});assert.equal(items.length,1);validateRecord(items[0]);assert.equal(items[0].summary,'');assert.equal(items[0].category,'scholarships');assert.deepEqual(items[0].eligible_countries,[]);assert.equal(items[0].deadline,null);});
test('stable IDs and first seen survive subsequent successful updates',async()=>{const a=normalizeItem({title:'Title',link:'https://example.org/a'},manifest,now);const result=await runPipeline([manifest],{opportunities:[a],sources:[]},{now:'2026-10-05T12:00:00.000Z',adapter:async()=>[normalizeItem({title:'New title',link:a.url},manifest,'2026-10-05T12:00:00.000Z')]});assert.equal(result.opportunities.length,1);assert.equal(result.opportunities[0].id,a.id);assert.equal(result.opportunities[0].first_seen_at,now);});
test('partial failure preserves previous record freshness and reports source error',async()=>{const old=normalizeItem({title:'Old',link:'https://example.org/a'},manifest,now);const second={...manifest,source:'second'};const result=await runPipeline([manifest,second],{opportunities:[old],sources:[{source:'example',last_success_at:now,record_count:1}]},{now:'2026-10-05T12:00:00.000Z',adapter:async({manifest:m})=>{if(m.source==='example')throw Error('outage');return [normalizeItem({title:'New',link:'https://example.org/b'},m,now)];}});assert.equal(result.opportunities.find(x=>x.id===old.id).last_checked_at,now);assert.equal(result.sources[0].last_success_at,now);assert.equal(result.sources[0].status,'error');});
test('total failure including empty feed refuses publication',async()=>{await assert.rejects(runPipeline([manifest],undefined,{adapter:async()=>[]}),/All enabled sources failed/);});
test('unsafe URLs fail validation',()=>{assert.throws(()=>normalizeItem({title:'X',link:'javascript:alert(1)'},manifest,now),/Unsafe/);});
test('malformed deadlines rejected rather than marked open',()=>{const record=normalizeItem({title:'X',link:'https://example.org/a'},manifest,now);assert.throws(()=>validateRecord({...record,deadline:'not-a-date'}),/Invalid date/);});
test('unchanged observations preserve updated_at while advancing last_seen',async()=>{const old=normalizeItem({title:'Same',link:'https://example.org/same'},manifest,now);const later='2026-10-05T12:00:00.000Z';const result=await runPipeline([manifest],{opportunities:[old],sources:[]},{now:later,adapter:async()=>[normalizeItem({title:'Same',link:old.url},manifest,later)]});assert.equal(result.opportunities[0].updated_at,now);assert.equal(result.opportunities[0].last_seen_at,later);});
test('duplicate IDs rejected before source batch is merged',async()=>{const old=normalizeItem({title:'X',link:'https://example.org/x'},manifest,now);await assert.rejects(runPipeline([manifest],undefined,{adapter:async()=>[old,old]}),/All enabled sources failed/);});
test('malformed country and credential-bearing links rejected',()=>{const old=normalizeItem({title:'X',link:'https://example.org/x'},manifest,now);assert.throws(()=>validateRecord({...old,host_countries:['Germany']}),/ISO/);assert.throws(()=>validateRecord({...old,url:'https://user:password@example.org/x'}),/Unsafe/);});

test('disabled source makes no request and removes its previously published records',async()=>{
 const disabled={...manifest,source:'disabled',enabled:false};
 const old=normalizeItem({title:'Old',link:'https://example.org/old'},disabled,now);
 const calls=[];
 const result=await runPipeline([manifest,disabled],{opportunities:[old],sources:[]},{now,adapter:async({manifest:m})=>{calls.push(m.source);return [normalizeItem({title:'Active',link:'https://example.org/active'},m,now)];}});
 assert.deepEqual(calls,['example']);assert.ok(result.opportunities.every(r=>r.source!=='disabled'));
 assert.equal(result.source_registry.find(s=>s.id==='disabled').enabled,false);
});
test('explicit collection restriction remains enforced even if enabled is changed',async()=>{
 const blocked={...manifest,source:'blocked',enabled:true,collection_blocked_reason:'Automated monitoring prohibited'};
 const calls=[];
 const result=await runPipeline([manifest,blocked],undefined,{now,adapter:async({manifest:m})=>{calls.push(m.source);return [normalizeItem({title:'Active',link:'https://example.org/active'},m,now)];}});
 assert.deepEqual(calls,['example']);assert.equal(result.sources.find(s=>s.source==='blocked').status,'error');assert.match(result.sources.find(s=>s.source==='blocked').error,/Collection blocked/);
});
test('outage retention removes historical prose without changing original freshness',async()=>{
 const old={...normalizeItem({title:'Old',link:'https://example.org/old'},manifest,now),summary:'Previously copied publisher description'};
 const second={...manifest,source:'second'};
 const result=await runPipeline([manifest,second],{opportunities:[old],sources:[]},{now:'2026-10-06T00:00:00Z',adapter:async({manifest:m})=>{if(m.source===manifest.source)throw Error('Outage');return [normalizeItem({title:'Other',link:'https://example.org/other'},m,now)];}});
 const retained=result.opportunities.find(r=>r.id===old.id);assert.equal(retained.summary,'');assert.equal(retained.last_checked_at,now);assert.equal(retained.first_seen_at,now);
});
