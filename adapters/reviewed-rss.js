import Parser from 'rss-parser';
import { classifyRecord } from '../scripts/taxonomy.js';
import { normalizeItem } from './rss.js';

export async function collect({ manifest, fetchText, now }) {
  const feed = await new Parser().parseString(await fetchText(manifest.source_url));
  const reviewed = new Map(manifest.reviewed_items.map(item => [item.url, item]));
  const records = [];
  for (const item of feed.items) {
    const selection = reviewed.get(item.link);
    if (!selection) continue;
    // Publish reviewed discovery metadata only, never source article prose.
    const record = normalizeItem({ title: item.title, link: item.link, isoDate: item.isoDate, pubDate: item.pubDate }, { ...manifest, default_tags: [] }, now);
    records.push(classifyRecord({ ...record, summary: '', category: selection.category, categories:[selection.category], kind:selection.kind, tags: [selection.kind] }, manifest));
  }
  if (!records.length) throw new Error('No reviewed programme items in feed: preserve last successful records');
  return records;
}
