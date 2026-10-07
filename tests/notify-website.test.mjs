import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {notifyWebsite} from '../scripts/notify-website.mjs';

test('website pointer commit uses main, revision checks and idempotent content',async t=>{
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'youthopp-notify-'));
 const manifestPath=path.join(dir,'manifest.json');
 await fs.writeFile(manifestPath,JSON.stringify({schema_version:1,release_tag:'catalog-123-1'}));
 const calls=[];
 const fetchMock=t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([url,options]);return new Response('{}',{status:options.method==='PUT'?201:404});});
 try{
  assert.equal(await notifyWebsite({manifestPath,token:'test-token'}),true);
  assert.equal(calls.length,2);
  const body=JSON.parse(calls[1][1].body);
  assert.equal(body.branch,'main');assert.equal(body.sha,undefined);
  const pointer=JSON.parse(Buffer.from(body.content,'base64').toString());
  assert.equal(pointer.repository,'YouthOpp/data-pipeline');assert.equal(pointer.release_tag,'catalog-123-1');assert.match(pointer.manifest_sha256,/^[a-f0-9]{64}$/);
  assert.ok(calls[0][0].endsWith('?ref=main'));
  fetchMock.mock.mockImplementation(async()=>new Response(JSON.stringify({content:body.content,sha:'revision'})));
  assert.equal(await notifyWebsite({manifestPath,token:'test-token'}),false);
  fetchMock.mock.mockImplementation(async(url,options)=>options.method==='PUT'?new Response('{}',{status:409}):new Response(JSON.stringify({content:Buffer.from('old pointer').toString('base64'),sha:'revision'})));
  await assert.rejects(()=>notifyWebsite({manifestPath,token:'test-token'}),/HTTP 409/);
  const update=fetchMock.mock.calls.at(-1).arguments;
  assert.equal(JSON.parse(update[1].body).sha,'revision');
 }finally{await fs.rm(dir,{recursive:true,force:true});}
});
test('missing credentials, invalid manifest and rejected lookup cannot write to the website',async t=>{
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'youthopp-notify-error-'));const manifestPath=path.join(dir,'manifest.json');
 const fetchMock=t.mock.method(globalThis,'fetch',async()=>new Response('{}',{status:403}));
 try{
  await assert.rejects(()=>notifyWebsite({manifestPath,token:''}),/WEBSITE_REPO_TOKEN/);
  await fs.writeFile(manifestPath,JSON.stringify({schema_version:1,release_tag:'catalog-latest'}));
  await assert.rejects(()=>notifyWebsite({manifestPath,token:'secret-token'}),/immutable release/);
  assert.equal(fetchMock.mock.callCount(),0);
  await fs.writeFile(manifestPath,JSON.stringify({schema_version:1,release_tag:'catalog-123-1'}));
  await assert.rejects(()=>notifyWebsite({manifestPath,token:'secret-token'}),/HTTP 403/);
  assert.equal(fetchMock.mock.callCount(),1);
  fetchMock.mock.mockImplementation(async()=>{throw Error('secret-token');});
  await assert.rejects(()=>notifyWebsite({manifestPath,token:'secret-token'}),error=>!error.message.includes('secret-token'));
 }finally{await fs.rm(dir,{recursive:true,force:true});}
});
