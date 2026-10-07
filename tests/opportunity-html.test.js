import test from 'node:test';
import assert from 'node:assert/strict';
import {extractListing,collect} from '../adapters/opportunity-html.js';
import {titleDestinations} from '../scripts/geography.js';
import {runPipeline} from '../scripts/collect.js';
import {inspectCoverage} from '../scripts/verify-catalog.js';

const now='2026-10-07T12:00:00Z';
const manifest={source:'fixture',source_url:'https://example.org/',website_url:'https://example.org/',adapter:'opportunity-html',enabled:true,language:'en',publisher_country:'IT'};
test('listing extraction deduplicates real calls and excludes navigation, external links and copied descriptions',()=>{
 const html='<nav><a href="/scholarships-navigation/">All available scholarships to study</a></nav><a href="/training-course-in-germany-2027/">Training course in Germany for young people</a><a href="/training-course-in-germany-2027/">Training course in Germany for young people</a><a href="/contact/">Contact our scholarships administrator</a><a href="https://other.org/internship/">Paid internships for young people</a>';
 const records=extractListing(html,manifest,now);assert.equal(records.length,1);assert.equal(records[0].kind,'opportunity');assert.equal(records[0].category,'training');assert.equal(records[0].summary,'');assert.deepEqual(records[0].eligible_countries,[]);
});
test('explicit destination evidence does not confuse publisher or applicant countries',()=>{
 assert.deepEqual(titleDestinations('Scholarships for German citizens','https://example.org/a').host_countries,[]);
 assert.deepEqual(titleDestinations('Training course in Germania for young people','https://example.org/a').host_countries,['DE']);
 assert.deepEqual(titleDestinations('Internships in the UK and in France','https://example.org/a').host_countries,['FR','GB']);
});
test('exact reviewed programme classification requires live original page and preserves unknown deadlines',async()=>{
 const selected={...manifest,source_url:'https://example.org/scholarship/',reviewed_opportunity:{url:'https://example.org/scholarship/',category:'scholarships',kind:'programme-overview'}};
 const calls=[];const records=await collect({manifest:selected,now,fetchText:async url=>{calls.push(url);return url.endsWith('/robots.txt')?'User-agent: *\nAllow: /':'<h1>Menu</h1><h1>Extra navigation</h1><title>Masters scholarship programme</title>';}});
 assert.equal(calls.length,2);assert.equal(records[0].kind,'programme-overview');assert.equal(records[0].deadline,null);
});
test('coverage reports a failed publisher with retained metadata as incomplete',async()=>{
 const second={...manifest,source:'failed'};
 const catalog=await runPipeline([manifest,second],undefined,{now,adapter:async({manifest:m})=>{if(m.source==='failed')throw Error('HTTP 403');return extractListing('<a href="/training-course-in-germany-2027/">Training course in Germany for young people</a>',m,now);}});
 const coverage=inspectCoverage(catalog,[manifest,second]);assert.equal(coverage.collected_sources,1);assert.equal(coverage.failed_sources,1);assert.equal(coverage.all_sources_have_opportunities,false);assert.deepEqual(catalog.opportunities[0].host_countries,['DE']);
});
