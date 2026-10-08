/**
 * Select individual published opportunities from the Opportunity Desk RSS feed.
 * Exclude editorial articles and roundup pages; do not guess applicant criteria.
 */
const editorialCategories = new Set([
  'blog', 'general tips', 'how-to', 'how to', 'success stories',
  'od specials', 'mentorship', 'young person of the month', 'guidance'
]);

const editorialTitlePatterns = [
  /^\s*(?:\d{1,3}|top\s+\d{1,3})\s+.+\b(?:opportunities|scholarships|jobs|internships|fellowships|grants)\b/i,
  /\b(?:how to|tips for|ways to|career advice|step-by-step guide|ultimate guide)\b/i,
  /^\s*new job\?/i,
  /\b(?:opportunities|scholarships|jobs)\s+(?:currently open|closing in)\b/i
];

export function isOpportunityDeskListing(item) {
  const title = String(item?.title ?? '').replace(/<[^>]*>/g, ' ').trim();
  const categories = Array.isArray(item?.categories) ? item.categories : [];
  if (categories.some(category => editorialCategories.has(String(category).trim().toLowerCase()))) return false;
  return !editorialTitlePatterns.some(pattern => pattern.test(title));
}
