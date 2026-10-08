import { classifyTags, classifyRecord } from '../scripts/taxonomy.js';
import { createHash } from 'node:crypto';
export function plainText(value = '') {
  return String(value).replace(/<[^>]*>/g, ' ').replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&').replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/\s+/g, ' ').trim();
}
export function normalizeItem(item, manifest, now) {
  const url = new URL(item.link).href;
  if (!['https:', 'http:'].includes(new URL(url).protocol)) throw new Error('Unsafe record URL');
  const title = plainText(item.title);
  if (!title) throw new Error('Missing title');
  const tags = [...new Set([...(item.categories || []).filter(x=>typeof x==='string'), ...(manifest.default_tags || [])].map(plainText).filter(Boolean))];
  const classified = classifyTags(tags);
  const category = classified.categories[0];
  const published = item.isoDate || item.pubDate;
  return classifyRecord({ id: createHash('sha256').update(`${manifest.source}|${url}`).digest('hex').slice(0,24), title, url, source: manifest.source, source_url: manifest.source_url, published_at: published && Number.isFinite(Date.parse(published)) ? new Date(published).toISOString() : null, summary: '', tags, location:null, deadline:null, language:manifest.language || null, category, host_countries:[], eligible_countries:[], created_at:now, updated_at:now, first_seen_at:now, last_seen_at:now, last_checked_at:now, status:'unknown', ...classified }, manifest);
}
