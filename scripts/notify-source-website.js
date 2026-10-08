import {execFileSync} from 'node:child_process';
const token=process.env.WEBSITE_REPO_TOKEN;
if(!token)throw Error('WEBSITE_REPO_TOKEN is required');
const commit=execFileSync('git',['-C','data-source','rev-parse','HEAD'],{encoding:'utf8'}).trim();
if(!/^[a-f0-9]{40}$/.test(commit))throw Error('Invalid source commit');
const endpoint='https://api.github.com/repos/YouthOpps/youthopps.github.io/contents/catalog-release.json';
const headers={Authorization:`Bearer ${token}`,Accept:'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'};
const current=await fetch(endpoint+'?ref=main',{headers,signal:AbortSignal.timeout(30000)});
if(!current.ok)throw Error(`Cannot read website catalog pointer: ${current.status}`);
const file=await current.json();
const content=JSON.stringify({schema_version:2,repository:'YouthOpps/data-source',commit_sha:commit},null,2)+'\n';
if(Buffer.from(file.content,'base64').toString('utf8')===content){console.log('Website is already synchronized');process.exit(0);}
const update=await fetch(endpoint,{method:'PUT',headers:{...headers,'Content-Type':'application/json'},body:JSON.stringify({message:`data: publish source snapshot ${commit.slice(0,12)}`,branch:'main',sha:file.sha,content:Buffer.from(content).toString('base64')}),signal:AbortSignal.timeout(30000)});
if(!update.ok)throw Error(`Website pointer update failed: ${update.status}`);
console.log(`Website now points to data-source ${commit}`);
