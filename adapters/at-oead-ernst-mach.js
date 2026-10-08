export const manifest = Object.freeze({
  "source": "at-oead-ernst-mach",
  "source_url": "https://studyinaustria.at/en/news/article/2026/08/ernst-mach-stipendium-weltweit-bewerbung-bis-1-dezember-2026",
  "website_url": "https://studyinaustria.at/",
  "enabled": true,
  "adapter": "reviewed-html",
  "language": "en",
  "default_tags": [],
  "added_at": "2026-10-05",
  "scope": "exact reviewed scholarship notice metadata; application availability and encoded eligibility unknown",
  "reviewed_page": {
    "url": "https://studyinaustria.at/en/news/article/2026/08/ernst-mach-stipendium-weltweit-bewerbung-bis-1-dezember-2026",
    "title": "Ernst Mach Scholarship – Worldwide: Application Deadline 1 December 2026",
    "category": "scholarships",
    "kind": "programme-overview",
    "metadata_format": "heading"
  },
  "attribution": "© OeAD",
  "access_review": {
    "reviewed_at": "2026-10-05",
    "robots_url": "https://studyinaustria.at/robots.txt",
    "policy_url": "https://oead.at/en/imprint-en",
    "scope": "Attributed factual notice title and exact canonical URL only. Text excerpts permitted with credit © OeAD; no multimedia, source prose, framing or database access. One exact-page request per six-hour run. Required source attribution must render on source directory, detail and every associated catalogue/home/category listing row before publication."
  },
  "research_source_id": "at-oead",
  "categories": [
    "scholarships"
  ],
  "publisher_country": "AT",
  "publisher_type": "public-institution"
});

import { normalizeItem, plainText } from '../scripts/record.js';

function attributes(tag) {
  return Object.fromEntries([...tag.matchAll(/([\w:-]+)\s*=\s*(["'])(.*?)\2/gs)].map(match => [match[1].toLowerCase(), match[3]]));
}

export async function collect({ fetchText, now, manifest: selectedManifest = manifest }) {
  if (selectedManifest.source !== manifest.source) throw new Error('Source mismatch');
  const html = await fetchText(manifest.source_url);
  const page = manifest.reviewed_page;
  const canonicals = [...html.matchAll(/<link\b[^>]*>/gi)].map(match => attributes(match[0])).filter(attrs => attrs.rel?.toLowerCase().split(/\s+/).includes('canonical'));
  if (canonicals.length !== 1 || canonicals[0].href !== page.url) throw new Error('Reviewed OeAD canonical does not match selected programme URL');
  const content = html.replace(/<(script|style)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, '');
  const headings = [...content.matchAll(/<h1\b[^>]*>([\s\S]*?)<\/h1\s*>/gi)].map(match => plainText(match[1]));
  if (headings.length !== 1 || headings[0] !== page.title) throw new Error('Reviewed OeAD programme heading changed');
  const record = normalizeItem({ title: page.title, link: page.url }, { ...manifest, default_tags: [] }, now);
  return [{ ...record, summary: '', category: page.category, categories: [page.category], kind: page.kind, tags: [page.kind], classification: { method: 'editorial-review', status: 'classified', evidence: [page.url] } }];
}
