import { normalizeItem, plainText } from './rss.js';

function attributes(tag) {
  return Object.fromEntries([...tag.matchAll(/([\w:-]+)\s*=\s*(["'])(.*?)\2/gs)].map(match => [match[1].toLowerCase(), match[3]]));
}

export async function collect({ manifest, fetchText, now }) {
  const html = await fetchText(manifest.source_url);
  const selected = manifest.reviewed_page;
  const canonicals = [...html.matchAll(/<link\b[^>]*>/gi)].map(match => attributes(match[0])).filter(attrs => attrs.rel?.toLowerCase().split(/\s+/).includes('canonical'));
  if (canonicals.length !== 1 || canonicals[0].href !== selected.url) throw new Error('Reviewed HTML canonical does not match selected programme URL');

  if (selected.metadata_format === 'heading') {
    const content = html.replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, '');
    const headings = [...content.matchAll(/<h1\b[^>]*>([\s\S]*?)<\/h1\s*>/gi)].map(match => plainText(match[1]));
    if (headings.length !== 1 || !headings[0] || headings[0] !== selected.title) throw new Error('Reviewed HTML requires one exact reviewed programme heading');
    // The reviewed notice gives a calendar date, not an original publication timestamp.
    const record = normalizeItem({ title: headings[0], link: selected.url }, { ...manifest, default_tags: [] }, now);
    return [{ ...record, summary: '', category: selected.category, categories: [selected.category], kind: selected.kind, tags: [selected.kind], classification: { method: 'editorial-review', status: 'classified', evidence: [selected.url] } }];
  }

  if (selected.metadata_format === 'open-graph') {
    const metas = [...html.matchAll(/<meta\b[^>]*>/gi)].map(match => attributes(match[0]));
    const values = property => metas.filter(attrs => attrs.property?.toLowerCase() === property).map(attrs => attrs.content);
    const urls = values('og:url');
    const titles = values('og:title');
    if (urls.length !== 1 || urls[0] !== selected.url || titles.length !== 1 || typeof titles[0] !== 'string' || !titles[0].trim()) throw new Error('Reviewed HTML requires one exact Open Graph programme identity');
    const dates = values('article:published_time');
    if (dates.length > 1 || (dates.length && !Number.isFinite(Date.parse(dates[0])))) throw new Error('Reviewed HTML original publication date is ambiguous or invalid');
    // Missing original publication stays unknown; modification is never publication.
    const record = normalizeItem({ title: titles[0], link: selected.url, isoDate: dates[0] }, { ...manifest, default_tags: [] }, now);
    return [{ ...record, summary: '', category: selected.category, categories: [selected.category], kind: selected.kind, tags: [selected.kind], classification: { method: 'editorial-review', status: 'classified', evidence: [selected.url] } }];
  }

  const pages = [];
  for (const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi)) {
    if (attributes(match[1]).type?.toLowerCase() !== 'application/ld+json') continue;
    const structured = JSON.parse(match[2]);
    const nodes = Array.isArray(structured) ? structured : structured['@graph'] || [structured];
    for (const node of nodes) {
      const types = Array.isArray(node['@type']) ? node['@type'] : [node['@type']];
      if (types.includes('WebPage') && node.url === selected.url) pages.push(node);
    }
  }
  if (pages.length !== 1) throw new Error('Reviewed HTML requires one exact programme WebPage');
  const page = pages[0];
  if (typeof page.name !== 'string' || !page.name.trim() || typeof page.datePublished !== 'string' || !Number.isFinite(Date.parse(page.datePublished))) throw new Error('Reviewed HTML programme title or publication date missing');

  // Keep the publisher's original publication date; modification is not publication.
  const record = normalizeItem({ title: page.name, link: selected.url, isoDate: page.datePublished }, { ...manifest, default_tags: [] }, now);
  return [{ ...record, summary: '', category: selected.category, categories: [selected.category], kind: selected.kind, tags: [selected.kind], classification: { method: 'editorial-review', status: 'classified', evidence: [selected.url] } }];
}
