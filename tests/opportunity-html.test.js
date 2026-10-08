import test from 'node:test';
import assert from 'node:assert/strict';
import {extractListing,collect} from '../adapters/opportunity-html.js';
import {titleDestinations} from '../scripts/geography.js';
import {runPipeline} from '../scripts/collect.js';
import {inspectCoverage} from '../scripts/verify-catalog.js';

const now='2026-10-07T12:00:00Z';
const manifest={source:'fixture',source_url:'https://example.org/',website_url:'https://example.org/',adapter:'opportunity-html',enabled:true,language:'en',publisher_country:'IT'};
test('reviewed-only programme extraction excludes generic tuition guidance and withdraws known bad historical URLs',async()=>{
 const url='https://example.org/tuition-fees-and-scholarships';
 const selected={...manifest,reviewed_links_only:true,reviewed_links:[{url:'https://external.example/ceepus',title:'Central European Exchange Programme for University Studies',category:'scholarships',reviewed_at:'2026-10-08'}],excluded_record_urls:[url]};
 const records=extractListing('<a href="https://external.example/ceepus">Central European Exchange Programme for University Studies</a><a href="/tuition-fees-and-scholarships">TUITION FEES AND SCHOLARSHIPS</a>',selected,now);
 assert.equal(records.length,1);assert.equal(records[0].kind,'programme-overview');
 const bad={...records[0],id:'bad-guidance',url,kind:'opportunity'};
 const catalog=await runPipeline([selected],{opportunities:[bad],sources:[]},{now,adapter:async()=>records});assert.equal(catalog.opportunities.length,1);assert.ok(!catalog.opportunities.some(r=>r.url===url));
});
test('native publisher call titles exclude category/date wrappers and winner news',()=>{
 const selected={...manifest,listing_title_rules:{competitions:'^(?:Konkurs(?:\\s|:)|(?:[IVX]+\\s+)?edycja konkursu|Nabór)'},listing_exclude_title_pattern:'laureat|wyniki|rozstrzygnię|zwycię',listing_path_pattern:'^/aktualnosci/'};
 const html='<main><a href="/aktualnosci/discovereu"><span>Konkursy Młodzież 01.10.2026 r.</span><h3>Konkurs DiscoverEU: ruszyła runda jesienna!</h3></a><a href="/aktualnosci/winners"><span>Konkursy 01.10.2026 r.</span><h3>Poznaj laureatów EITA 2026!</h3></a><a href="/aktualnosci/closed"><h3>Konkurs rozstrzygnięty: wyniki edycji</h3></a><a href="/aktualnosci/school"><h3>Nabór uzupełniający: Profesjonalna Szkoła Roku</h3></a></main>';
 const records=extractListing(html,selected,now);assert.equal(records.length,2);assert.equal(records[0].title,'Konkurs DiscoverEU: ruszyła runda jesienna!');assert.ok(records.every(r=>r.category==='competitions'));assert.ok(!records.some(r=>/laureat|wyniki/.test(r.title)));
});
test('named programmes on one original page have stable distinct IDs and require every live section',async()=>{
 const selected={...manifest,reviewed_sections:[{title:'The UN Volunteer Program (UNV)',category:'volunteering',reviewed_at:'2026-10-07'},{title:'The Junior Professional Officer Programme (JPO/JEA)',category:'jobs',reviewed_at:'2026-10-07'}]};
 const load=html=>async url=>url.endsWith('/robots.txt')?'':html;
 const records=await collect({manifest:selected,now,fetchText:load('<h2>The UN Volunteer Program (UNV)</h2><h2>The Junior Professional Officer Programme (JPO/JEA)</h2>')});assert.equal(new Set(records.map(r=>r.id)).size,2);assert.ok(records.every(r=>r.url===manifest.source_url&&r.kind==='programme-overview'));
 await assert.rejects(collect({manifest:selected,now,fetchText:load('<h2>The UN Volunteer Program (UNV)</h2>')}),/section is missing/);
});
test('card headings identify scholarship programmes behind generic Learn More links',()=>{
 const html='<h3>Visegrad Scholarship Program</h3><p>Publisher description excluded</p><a href="/scholarships/visegrad-scholarships/">Learn More</a><h3>Visegrad Fellowship Program</h3><a href="/fellowships/">Learn More</a><h3>Unrelated news</h3><a href="/news/">Learn More</a>';
 const records=extractListing(html,manifest,now);assert.equal(records.length,2);assert.equal(records[0].title,'Visegrad Scholarship Program');assert.equal(records[0].summary,'');assert.equal(records[1].category,'fellowships');
});
test('reviewed links require a live exact title and URL; large calls are not silently truncated at 50',()=>{
 const selected={...manifest,reviewed_links:[{url:'https://other.org/msp',title:'MENA Scholarship Programme (MSP)',category:'scholarships',reviewed_at:'2026-10-07'}]};
 assert.equal(extractListing('<nav><a href="https://other.org/msp">MENA Scholarship Programme (MSP)</a></nav>',selected,now)[0].kind,'programme-overview');
 assert.equal(extractListing('<a href="https://other.org/changed">MENA Scholarship Programme (MSP)</a>',selected,now).length,0);
 const html=Array.from({length:65},(_,i)=>`<a href="/scholarship-${i}">Verified student scholarship number ${i}</a>`).join('');assert.equal(extractListing(html,manifest,now).length,65);
});
test('programme cards retain article headers and allow only reviewed external call documents',()=>{
 const html='<main><article><header><a href="/mena-scholarship-programme/">MENA Scholarship Programme (MSP)</a></header></article><h3>IT Kolledži stipendium (BA, MA)</h3><p><a href="https://haldus.example.org/call.pdf">SIIT</a></p></main>';
 assert.equal(extractListing(html,manifest,now).length,1);
 const records=extractListing(html,{...manifest,listing_allowed_hosts:['haldus.example.org'],listing_allow_pdf:true},now);assert.equal(records.length,2);assert.ok(records.some(r=>r.title==='IT Kolledži stipendium (BA, MA)'));
});
test('reviewed programme title must exist in live headings, including pages with navigation headings',async()=>{
 const selected={...manifest,source_url:'https://example.org/scholar/',reviewed_opportunity:{url:'https://example.org/scholar/',title:'Fulbright Swedish Scholar Program',category:'scholarships',kind:'programme-overview'}};
 const load=html=>async url=>url.endsWith('/robots.txt')?'':html;
 const records=await collect({manifest:selected,now,fetchText:load('<h1>Navigation</h1><h1>Fulbright Swedish Scholar Program</h1>')});assert.equal(records[0].title,selected.reviewed_opportunity.title);
 await assert.rejects(collect({manifest:selected,now,fetchText:load('<h1>Unrelated programme</h1>')}),/title is missing/);
});
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
