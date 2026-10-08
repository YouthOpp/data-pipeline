import fs from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';

export async function notifyWebsite({manifestPath='dist/manifest.json',token=process.env.WEBSITE_REPO_TOKEN}={}){
 if(!token)throw new Error('WEBSITE_REPO_TOKEN is required to update the website catalog version');
 const bytes=await fs.readFile(manifestPath);
 const manifest=JSON.parse(bytes);
 if(manifest.schema_version!==1||!/^catalog-\d+-\d+$/.test(manifest.release_tag))throw new Error('Invalid immutable release manifest');
 const content=JSON.stringify({schema_version:1,repository:'YouthOpps/data-pipeline',release_tag:manifest.release_tag,manifest_sha256:createHash('sha256').update(bytes).digest('hex')},null,2)+'\n';
 const endpoint='https://api.github.com/repos/YouthOpps/youthopp.github.io/contents/catalog-release.json';
 const headers={Accept:'application/vnd.github+json',Authorization:`Bearer ${token}`,'X-GitHub-Api-Version':'2022-11-28','Content-Type':'application/json'};
 async function request(url,options){
  try{return await fetch(url,{headers,redirect:'error',signal:AbortSignal.timeout(30000),...options});}catch{throw new Error('Website catalog version request failed');}
 }
 const current=await request(endpoint+'?ref=main');
 let sha;
 if(current.ok){
  const file=await current.json();
  if(Buffer.from(file.content||'','base64').toString('utf8')===content){console.log('Website already references this catalog release.');return false;}
  sha=file.sha;
  if(!sha)throw new Error('Website catalog version response is missing its revision');
 }else if(current.status!==404)throw new Error(`Website catalog version lookup rejected (HTTP ${current.status})`);
 const result=await request(endpoint,{method:'PUT',body:JSON.stringify({message:`Open AI agent: refresh catalog to ${manifest.release_tag}`,branch:'main',content:Buffer.from(content).toString('base64'),...(sha?{sha}:{})})});
 if(!result.ok)throw new Error(`Website catalog version commit rejected (HTTP ${result.status})`);
 console.log(`Website catalog version committed: ${manifest.release_tag}. Check Cloudflare deployment completion separately.`);
 return true;
}
if(process.argv[1]===fileURLToPath(import.meta.url))await notifyWebsite();
