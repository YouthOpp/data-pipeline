import {execFileSync} from 'node:child_process';
import {mkdtempSync, mkdirSync, writeFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join, dirname} from 'node:path';
import {pathToFileURL} from 'node:url';

export const repositories = ['YouthOpp/.github', 'YouthOpp/youthopp.github.io', 'YouthOpp/data-pipeline'];

export function summarizeCommits(records) {
  const seen = new Set();
  const users = new Map();
  let unresolved = 0;
  let excluded = 0;
  for (const record of records) {
    if (seen.has(record.sha)) continue;
    seen.add(record.sha);
    if (record.parents?.length > 1 || /\[bot\]|bot@|open ai agent/i.test(`${record.name} ${record.email}`) || /^(Open AI agent:|chore: update opportunities dataset)/i.test(record.subject)) {
      excluded++;
      continue;
    }
    const match = /^((?:\d+\+)?)([a-z\d](?:[a-z\d-]{0,38}))@users\.noreply\.github\.com$/i.exec(record.email || '');
    const login = record.login || match?.[2];
    if (!login || !/^[a-z\d][a-z\d-]{0,38}$/i.test(login)) { unresolved++; continue; }
    const key = login.toLowerCase();
    const user = users.get(key) || {login, name: login, avatar_url: `https://github.com/${login}.png?size=160`, html_url: `https://github.com/${login}`, score: 0, commits: 0, repositories: []};
    user.commits++;
    user.score++;
    if (!user.repositories.includes(record.repository)) user.repositories.push(record.repository);
    users.set(key, user);
  }
  return {contributors: [...users.values()].sort((a,b) => b.score-a.score || a.login.localeCompare(b.login)), unresolved_commits: unresolved, excluded_commits: excluded, unique_commits: seen.size};
}

export async function collect(output = 'dist/contributors.json') {
  const temporary = mkdtempSync(join(tmpdir(), 'youthopp-history-'));
  const records = [];
  const errors = [];
  try {
    for (const repository of repositories) {
      const directory = join(temporary, repository.split('/')[1]);
      try {
        execFileSync('git', ['clone', '--bare', '--filter=blob:none', '--single-branch', `https://github.com/${repository}.git`, directory], {stdio:'pipe', timeout:180000, env: {...process.env, GIT_TERMINAL_PROMPT:'0'}});
        const log = execFileSync('git', ['--git-dir',directory,'log','--format=%H%x1f%P%x1f%an%x1f%ae%x1f%s'], {encoding:'utf8', maxBuffer:20*1024*1024});
        for (const line of log.trim().split('\n')) {
          if (!line) continue;
          const [sha,parents,name,email,subject] = line.split('\x1f');
          records.push({sha,parents:parents?parents.split(' '):[],name,email,subject,repository});
        }
      } catch (error) { errors.push({repository,error:'Public history collection failed'}); }
    }
    // API author mapping recovers non-noreply authors; missing mappings remain explicit.
    const mappings = new Map();
    const mappingErrors = [];
    for (const repository of repositories) {
      for (let page=1; page<=100; page++) {
        try {
          const headers = {'Accept':'application/vnd.github+json','User-Agent':'YouthOpp-contributors'};
          if (process.env.GITHUB_TOKEN) headers.Authorization=`Bearer ${process.env.GITHUB_TOKEN}`;
          const response=await fetch(`https://api.github.com/repos/${repository}/commits?per_page=100&page=${page}`,{headers,signal:AbortSignal.timeout(20000)});
          if (!response.ok) { mappingErrors.push({repository,error:`Author mapping HTTP ${response.status}`}); break; }
          const commits=await response.json();
          for (const commit of commits) if(commit.author?.login) mappings.set(commit.sha,commit.author.login);
          if(commits.length<100) break;
          if(page===100) mappingErrors.push({repository,error:'Author mapping page limit reached'});
        } catch { mappingErrors.push({repository,error:'Author mapping request failed'}); break; }
      }
    }
    for (const record of records) record.login=mappings.get(record.sha);
    const canonicalRepositories = [...repositories];
    for (const record of records) record.repository = canonicalRepositories[repositories.indexOf(record.repository)];
    const result={generated_at:new Date().toISOString(),status:errors.length||mappingErrors.length?'partial':'complete',repositories:canonicalRepositories,history_repositories:repositories,errors:errors.map(error=>({...error,repository:canonicalRepositories[repositories.indexOf(error.repository)]})),mapping_errors:mappingErrors.map(error=>({...error,repository:canonicalRepositories[repositories.indexOf(error.repository)]})),scoring:'One point per attributable non-merge authored commit; bots and explicitly AI-authored automation excluded; unresolved identities not guessed.',...summarizeCommits(records)};
    if (errors.length===repositories.length) throw new Error('No repository history could be collected; previous output preserved');
    mkdirSync(dirname(output),{recursive:true});
    writeFileSync(output,JSON.stringify(result,null,2)+'\n');
    return result;
  } finally { rmSync(temporary,{recursive:true,force:true}); }
}

if (process.argv[1] && import.meta.url===pathToFileURL(process.argv[1]).href) collect(process.argv[2]).then(result=>console.log(`Collected ${result.contributors.length} contributors; ${result.unresolved_commits} unresolved commits; status ${result.status}`)).catch(error=>{console.error(error.message);process.exitCode=1;});
