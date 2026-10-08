import Parser from 'rss-parser';
import { normalizeItem } from './rss.js';
import { isOpportunityDeskListing } from './opportunitydesk-filter.js';

export async function collect({ manifest, fetchText, now }) {
  if (manifest.source !== 'opportunitydesk') throw new Error('Opportunity Desk adapter used for another source');
  const feed = await new Parser().parseString(await fetchText(manifest.source_url));
  if (!Array.isArray(feed.items) || feed.items.length === 0) throw new Error('Empty Opportunity Desk feed');

  const eligible = feed.items.filter(isOpportunityDeskListing);
  if (eligible.length === 0) throw new Error('No individual opportunities in Opportunity Desk feed');

  return eligible.map(item => {
    const record = normalizeItem(item, manifest, now);
    const host = new URL(record.url).hostname.toLowerCase();
    if (host !== 'opportunitydesk.org' && host !== 'www.opportunitydesk.org') {
      throw new Error('Opportunity Desk record URL leaves publisher host');
    }
    return { ...record, kind: 'opportunity' };
  });
}
