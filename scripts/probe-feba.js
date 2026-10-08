const base = 'https://febalumni.org';
const paths = ['/feed/', '/wp-json/wp/v2/posts?per_page=2&_fields=date_gmt,link,title,categories', '/news/'];
let lastRequest = 0;
let minGapMs = 6100;
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

function evaluateRobots(text, path) {
  const groups = [];
  let current = { agents: [], rules: [], delays: [] };
  for (const line of text.split(/\r?\n/)) {
    const match = line.replace(/#.*$/, '').trim().match(/^([^:]+):\s*(.*)$/);
    if (!match) continue;
    const directive = match[1].toLowerCase().trim();
    const value = match[2].trim();
    if (directive === 'user-agent') {
      if (current.rules.length || current.delays.length) {
        groups.push(current);
        current = { agents: [], rules: [], delays: [] };
      }
      current.agents.push(value.toLowerCase());
    } else if (directive === 'allow' || directive === 'disallow') {
      current.rules.push({ directive, value });
    } else if (directive === 'crawl-delay') {
      if (!/^\d+(?:\.\d+)?$/.test(value)) throw Error('Invalid robots crawl delay');
      current.delays.push(Number(value));
    }
  }
  groups.push(current);
  const matched = groups.filter(group => group.agents.some(agent => agent === '*' || 'youthopp'.startsWith(agent)));
  if (!matched.length) return { allowed: true, crawlDelaySeconds: 0 };
  const exact = matched.filter(group => group.agents.some(agent => agent !== '*' && 'youthopp'.startsWith(agent)));
  const relevant = exact.length ? exact : matched;
  const rules = relevant.flatMap(group => group.rules).filter(rule => {
    const prefix = rule.value.split('*')[0].replace(/\$$/, '');
    return prefix.length > 0 && path.startsWith(prefix);
  }).sort((a,b) => b.value.length - a.value.length || (a.directive === 'allow' ? -1 : 1));
  const winner = rules[0];
  const allowed = !winner || (winner.directive === 'allow' && !winner.value.includes('*'));
  const crawlDelaySeconds = Math.max(0, ...relevant.flatMap(group => group.delays));
  return { allowed, crawlDelaySeconds };
}
async function get(path) {
  const remaining = minGapMs - (Date.now() - lastRequest);
  if (remaining > 0) await delay(remaining);
  lastRequest = Date.now();
  const response = await fetch(new URL(path, base), {
    headers: { 'User-Agent': 'YouthOpp/1.0 (+https://github.com/YouthOpps/data-pipeline)', Accept: 'application/json, application/rss+xml, text/html, text/plain' },
    signal: AbortSignal.timeout(25000),
    redirect: 'manual'
  });
  if (response.status >= 300 && response.status < 400) {
    console.log(JSON.stringify({ endpoint: path, status: response.status, redirect: response.headers.get('location') }));
    throw Error('Redirect needs explicit review; refusing to follow');
  }
  const body = await response.text();
  return { status: response.status, body: body.slice(0, 300000), headers: response.headers };
}
if (process.env.FEBA_PROBE_FIXTURE === '1') {
  const fixture = 'User-agent: *\nDisallow: /wp-json/\nAllow: /feed/\nCrawl-delay: 7';
  if (evaluateRobots(fixture, '/wp-json/wp/v2/posts').allowed) throw Error('robots disallow test failed');
  if (!evaluateRobots(fixture, '/feed/').allowed) throw Error('robots allow test failed');
  if (evaluateRobots(fixture, '/feed/').crawlDelaySeconds !== 7) throw Error('robots delay test failed');
  console.log('PASS robots and crawl-delay fixture');
} else {
  const robots = await get('/robots.txt');
  console.log(JSON.stringify({ endpoint: 'robots.txt', status: robots.status, bytes: robots.body.length }));
  if (robots.status !== 200 && robots.status !== 404) throw Error('Publisher robots policy cannot be verified; probe aborted');
  const policy = robots.status === 404 ? '' : robots.body;
  for (const path of paths) {
    const decision = evaluateRobots(policy, path);
    if (!decision.allowed) { console.log(JSON.stringify({ endpoint: path, skipped: 'robots disallow' })); continue; }
    if (decision.crawlDelaySeconds > 30) throw Error('Crawl delay exceeds collection window; probe aborted');
    minGapMs = Math.max(6100, decision.crawlDelaySeconds * 1000);
    const result = await get(path);
    const title = result.body.match(/<title[^>]*>([\s\S]*?)<\/title>/i)?.[1]?.replace(/\s+/g,' ').slice(0,130);
    const posts = result.body.trim().startsWith('[') ? (() => { try { const data = JSON.parse(result.body); return Array.isArray(data) ? data.length : null; } catch { return null; } })() : null;
    console.log(JSON.stringify({ endpoint: path, status: result.status, bytes: result.body.length, contentType: result.headers.get('content-type'), title, posts, items: (result.body.match(/<item[\s>]/gi) || []).length }));
  }
}
