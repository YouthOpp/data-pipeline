import test from 'node:test';
import assert from 'node:assert/strict';
import {fetchText} from '../scripts/collect.js';
test('transient connection failure gets one retry while access restrictions remain errors',async()=>{
 let calls=0;const value=await fetchText('https://example.org/',async()=>{if(++calls===1)throw new TypeError('fetch failed');return new Response('original source');});assert.equal(value,'original source');assert.equal(calls,2);
});
test('source transport follows bounded same-host HTTPS redirects with one timeout',async()=>{
 const calls=[];const result=await fetchText('https://example.org/robots.txt',async(url,options)=>{calls.push({url,...options});return calls.length===1?new Response(null,{status:301,headers:{location:'https://www.example.org/robots.txt/'}}):new Response('User-agent: *\nAllow: /');});
 assert.equal(calls[1].url,'https://www.example.org/robots.txt/');assert.equal(calls[0].signal,calls[1].signal);assert.equal(calls[0].redirect,'manual');assert.match(result,/Allow/);
});
test('source transport rejects redirects to other hosts, HTTP and credentials',async()=>{
 for(const location of ['https://unreviewed.example/','http://example.org/','https://user:pass@example.org/','https://example.org:8443/'])await assert.rejects(fetchText('https://example.org/robots.txt',async()=>new Response(null,{status:302,headers:{location}})),/reviewed HTTPS host/);
});
test('source transport rejects loops and preserves HTTP access errors',async()=>{
 await assert.rejects(fetchText('https://example.org/robots.txt',async()=>new Response(null,{status:302,headers:{location:'/robots.txt'}})),/Too many/);
 await assert.rejects(fetchText('https://example.org/',async()=>new Response(null,{status:403})),/HTTP 403/);
});
