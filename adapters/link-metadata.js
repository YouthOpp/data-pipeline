import { setTimeout as delay } from 'node:timers/promises';
import { normalizeItem, plainText } from './rss.js';
import { decode } from 'html-entities';

const ROBOT_NAME = 'youthopp';
const MAX_TITLE_LENGTH = 300;
const MAX_CRAWL_DELAY_SECONDS = 30;

function attributes(tag) {
  const result = {};
  for (const match of tag.matchAll(/([^\s=/>]+)\s*=\s*(?:(["'])(.*?)\2|([^\s>]+))/gs)) {
    result[match[1].toLowerCase()] = decode(match[3] ?? match[4] ?? '', { level: 'html5', scope: 'attribute' });
  }
  return result;
}

function robotsGroups(text) {
  const groups = [];
  let group = null;
  let recognized = false;
  let meaningful = false;

  for (const rawLine of String(text).split(/\r?\n/)) {
    const line = rawLine.replace(/\s*#.*$/, '').trim();
    if (!line) continue;
    meaningful = true;
    const match = line.match(/^([^:]+):\s*(.*)$/);
    if (!match) continue;
    const directive = match[1].trim().toLowerCase();
    const value = match[2].trim();

    if (directive === 'user-agent') {
      recognized = true;
      if (!group || group.hasDirectives) {
        group = { agents: [], rules: [], crawlDelays: [], crawlDelayInvalid: false, hasDirectives: false };
        groups.push(group);
      }
      if (value) group.agents.push(value.toLowerCase());
      continue;
    }

    if (directive === 'sitemap') {
      recognized = true;
      continue;
    }
    if (!group || !group.agents.length) continue;
    if (directive === 'allow' || directive === 'disallow') {
      recognized = true;
      group.hasDirectives = true;
      if (value) group.rules.push({ type: directive, pattern: value });
    } else if (directive === 'crawl-delay') {
      recognized = true;
      group.hasDirectives = true;
      const seconds = Number(value);
      if (value && Number.isFinite(seconds) && seconds >= 0) group.crawlDelays.push(seconds);
      else group.crawlDelayInvalid = true;
    }
  }

  if (meaningful && !recognized) throw new Error('Robots policy is unavailable or invalid; collection denied');
  return groups;
}

function matchingGroups(groups) {
  const candidates = groups.map(group => {
    const matches = group.agents.filter(agent => agent === '*' || ROBOT_NAME.startsWith(agent));
    const specificity = matches.reduce((best, agent) => Math.max(best, agent === '*' ? 0 : agent.length), -1);
    return { group, specificity };
  }).filter(candidate => candidate.specificity >= 0);
  if (!candidates.length) return [];
  const specificity = Math.max(...candidates.map(candidate => candidate.specificity));
  return candidates.filter(candidate => candidate.specificity === specificity).map(candidate => candidate.group);
}

function ruleMatch(pattern, path) {
  const endAnchored = pattern.endsWith('$');
  const body = endAnchored ? pattern.slice(0, -1) : pattern;
  const expression = body.split('*').map(value => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('.*');
  return new RegExp(`^${expression}${endAnchored ? '$' : ''}`).test(path);
}

function robotsDecision(text, sourceUrl) {
  const selected = matchingGroups(robotsGroups(text));
  if (selected.some(group => group.crawlDelayInvalid)) throw new Error('Robots crawl delay is invalid; collection denied');
  const declaredDelays = selected.flatMap(group => group.crawlDelays);
  if (declaredDelays.some(seconds => seconds > MAX_CRAWL_DELAY_SECONDS)) throw new Error('Robots crawl delay exceeds the safe collection window');
  const path = `${sourceUrl.pathname}${sourceUrl.search}` || '/';
  const matches = selected.flatMap(group => group.rules).filter(rule => ruleMatch(rule.pattern, path)).map(rule => ({
    ...rule,
    specificity: rule.pattern.replace(/\*/g, '').replace(/\$$/, '').length
  }));
  matches.sort((left, right) => right.specificity - left.specificity || (left.type === 'allow' ? -1 : 1));
  const allowed = !matches.length || matches[0].type === 'allow';
  const crawlDelay = Math.max(0, ...declaredDelays);
  return { allowed, crawlDelay };
}

function sensibleTitle(value) {
  const title = plainText(decode(value, { level: 'html5' }));
  if (!title || title.length > MAX_TITLE_LENGTH) return null;
  const generic = title.toLowerCase().replace(/[.!?\s]+$/g, '').trim();
  if (/^(?:(?:401|403|429|500|502|503|504)\s+)?(?:log[ -]?in|sign[ -]?in|access denied|forbidden|unauthorized|just a moment|attention required|checking your browser|security check|access challenge|captcha|human verification|verify (?:that )?you are human|enable javascript and cookies to continue|sorry,? you have been blocked|request unsuccessful|too many requests|service unavailable|temporarily unavailable|error|home|welcome)(?:\s*[|\-–—:]\s*.*)?$/.test(generic)) return null;
  return title;
}

function selectTitle(html) {
  const content = html.replace(/<!--([\s\S]*?)-->/g, '').replace(/<(script|style|template|noscript)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, '');
  const metas = [...content.matchAll(/<meta\b[^>]*>/gi)].map(match => attributes(match[0]));
  const candidates = [
    { name: 'h1', values: [...content.matchAll(/<h1\b[^>]*>([\s\S]*?)<\/h1\s*>/gi)].map(match => match[1]) },
    { name: 'og:title', values: metas.filter(attrs => (attrs.property || attrs.name || '').toLowerCase() === 'og:title').map(attrs => attrs.content ?? '') },
    { name: 'title', values: [...content.matchAll(/<title\b[^>]*>([\s\S]*?)<\/title\s*>/gi)].map(match => match[1]) }
  ];

  for (const candidate of candidates) {
    if (!candidate.values.length) continue;
    if (candidate.values.length !== 1) throw new Error(`Link metadata has ambiguous ${candidate.name}`);
    if(candidate.name==='h1' && /^(?:navigation|nav|menu|navegação)$/i.test(plainText(decode(candidate.values[0])).trim()))continue;
    const title = sensibleTitle(candidate.values[0]);
    if (!title) throw new Error(`Link metadata has an empty, generic, or excessive ${candidate.name}`);
    return title;
  }
  throw new Error('Link metadata title is missing');
}

function assertPageIdentity(html, sourceUrl) {
  const canonicals = [...html.matchAll(/<link\b[^>]*>/gi)].map(match => attributes(match[0])).filter(attrs => (attrs.rel || '').toLowerCase().split(/\s+/).includes('canonical'));
  if (canonicals.length > 1) throw new Error('Link metadata canonical is ambiguous');
  if (canonicals.length === 1) {
    if (!canonicals[0].href) throw new Error('Link metadata canonical is empty');
    let canonical;
    try { canonical = new URL(canonicals[0].href, sourceUrl).href; } catch { throw new Error('Link metadata canonical is invalid'); }
    if (canonical !== sourceUrl.href) throw new Error('Link metadata canonical does not match source URL');
  }

  const openGraphUrls = [...html.matchAll(/<meta\b[^>]*>/gi)].map(match => attributes(match[0])).filter(attrs => (attrs.property || attrs.name || '').toLowerCase() === 'og:url');
  for (const metadata of openGraphUrls) {
    let identity;
    try { identity = new URL(metadata.content, sourceUrl).href; } catch { throw new Error('Link metadata Open Graph URL is invalid'); }
    if (identity !== sourceUrl.href) throw new Error('Link metadata Open Graph URL conflicts with source URL');
  }
}

async function loadRobots(fetchText, sourceUrl) {
  try {
    return await fetchText(new URL('/robots.txt', sourceUrl).href);
  } catch (error) {
    if (String(error?.message).trim() === 'HTTP 404') return '';
    throw new Error('Robots policy is unavailable; collection denied');
  }
}

export async function collect({ manifest, fetchText, now }) {
  if (manifest.collection_blocked_reason) throw new Error('Source collection is blocked by its reviewed permission policy');

  const sourceUrl = new URL(manifest.source_url);
  const robots = robotsDecision(await loadRobots(fetchText, sourceUrl), sourceUrl);
  if (!robots.allowed) throw new Error('Robots policy disallows collection');
  if (robots.crawlDelay) await delay(robots.crawlDelay * 1000);

  const html = await fetchText(sourceUrl.href);
  assertPageIdentity(html, sourceUrl);
  const title = selectTitle(html);
  const record = normalizeItem({ title, link: sourceUrl.href }, { ...manifest, default_tags: [] }, now);
  return [{
    ...record,
    summary: '',
    published_at: null,
    deadline: null,
    host_countries: [],
    eligible_countries: [],
    status: 'unknown',
    tags: [],
    category: 'other',
    categories: ['other'],
    kind: 'unknown',
    classification: { method: 'link-metadata-v1', status: 'unknown', evidence: [] }
  }];
}
