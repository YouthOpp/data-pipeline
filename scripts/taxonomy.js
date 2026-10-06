/** Canonical directory vocabulary; every public index uses these identifiers. */
export const taxonomy = Object.freeze({
  version: 1,
  categories: ['scholarships','internships','volunteering','fellowships','training','competitions','grants','jobs','other'].map(id => ({ id, label: id[0].toUpperCase() + id.slice(1) })),
  record_kinds: ['opportunity','programme-overview','institutional-grant','unknown'],
  publisher_types: ['aggregator','public-institution','nonprofit','university','employer','unknown'],
  source_content_mapping: { scholarship:'scholarships', internship:'internships', volunteering:'volunteering', grant:'grants', job:'jobs', fellowship:'fellowships', training:'training', competition:'competitions' },
  cross_cutting_content_types: ['mobility','research','guidance','youth']
});
export const categoryIds = taxonomy.categories.map(x => x.id);
const rules = [ /\b(?:scholarships?|burs)\b/i, /\binternships?\b/i, /\bvolunteer(?:ing|s)?\b/i, /\bfellowships?\b/i, /\b(?:training|courses?|workshops?)\b/i, /\b(?:competitions?|contests?)\b/i, /\bgrants?\b/i, /\b(?:jobs?|careers?)\b/i ];
export function classifyTags(tags = []) {
  const categories = categoryIds.slice(0,-1).filter((id,index) => tags.some(tag => rules[index].test(tag)));
  return { categories: categories.length ? categories : ['other'], classification: { method:'feed-tag-rules-v1', status:categories.length ? 'classified' : 'unknown', evidence:tags.filter(tag => rules.some(rule => rule.test(tag))) } };
}
export function classifyRecord(record, manifest = {}) {
  const inferred = classifyTags(record.tags);
  const categories = record.categories || (record.category && record.category !== 'other' ? [record.category] : inferred.categories);
  const reviewed = manifest.reviewed_items?.find(item => item.url === record.url);
  const kind = reviewed?.kind || record.kind || (record.tags || []).find(tag => ['programme-overview','institutional-grant'].includes(tag)) || 'unknown';
  return { ...record, category:categories[0], categories, kind,
    publisher_country:manifest.publisher_country || record.publisher_country || null,
    classification: reviewed ? { method:'editorial-review', status:'classified', evidence:[reviewed.url] } : record.classification || (record.category && record.category !== 'other' && !record.categories ? { method:'legacy-primary-category', status:'classified', evidence:[record.category] } : inferred.classification)
  };
}
export function validateClassification(record) {
  if (!Array.isArray(record.categories) || !record.categories.length || new Set(record.categories).size !== record.categories.length || record.categories.some(id => !categoryIds.includes(id)) || (record.categories.includes('other') && record.categories.length > 1) || record.category !== record.categories[0]) throw Error('Invalid category membership');
  if (!taxonomy.record_kinds.includes(record.kind)) throw Error('Invalid record kind');
  if (record.publisher_country !== null && !/^[A-Z]{2}$/.test(record.publisher_country)) throw Error('Invalid publisher country');
  if (!record.classification || !['classified','unknown'].includes(record.classification.status) || typeof record.classification.method !== 'string' || !Array.isArray(record.classification.evidence) || record.classification.evidence.some(value=>typeof value !== 'string') || (record.classification.status === 'unknown' && record.categories.some(id=>id !== 'other'))) throw Error('Invalid classification evidence');
}
export function classifySource(source, adapter = false) {
  const id=adapter ? source.source : source.id;
  if(typeof id !== 'string' || !/^[a-z0-9-]+$/.test(id))throw Error('Invalid source registry ID');
  const types = source.content_types || [];
  if(!Array.isArray(types))throw Error('Invalid source content types');
  const unsupported = types.filter(type => !Object.hasOwn(taxonomy.source_content_mapping,type) && !taxonomy.cross_cutting_content_types.includes(type));
  if (unsupported.length) throw Error(`Unsupported source content types: ${unsupported.join(',')}`);
  const categories = source.categories || [...new Set(types.map(type => taxonomy.source_content_mapping[type]).filter(Boolean))];
  if (!Array.isArray(categories) || categories.some(id => !categoryIds.includes(id)) || new Set(categories).size !== categories.length) throw Error('Invalid source category membership');
  const publisher_country=source.publisher_country || (source.country === 'GLOBAL' ? null : source.country) || null;
  if(publisher_country !== null && !/^[A-Z]{2}$/.test(publisher_country))throw Error('Invalid source publisher country');
  const publisher_type = source.publisher_type || 'unknown';
  if (!taxonomy.publisher_types.includes(publisher_type)) throw Error('Invalid publisher type');
  return { ...source, id:adapter ? source.source : source.id, categories:categories.length ? categories : ['other'], publisher_type,
    publisher_country,
    classification_status:categories.some(id=>id !== 'other') ? 'classified' : 'unknown',
    unmapped_content_types:types.filter(type => !Object.hasOwn(taxonomy.source_content_mapping,type)),
    adapter_source_id:adapter ? source.source : source.adapter_source_id || null
  };
}
export function buildCategoricalCatalog(opportunities, manifests, research = []) {
  const source_registry = research.map(source => classifySource(source));
  for (const manifest of manifests) {
    const target = source_registry.find(source => source.id === manifest.research_source_id || source.adapter_source_id === manifest.source);
    if (target) { Object.assign(target,{adapter_source_id:manifest.source,collection_enabled:manifest.enabled,...(manifest.adapter==='link-metadata'?{adapter_status:'configured_link_metadata',acquisition_state:manifest.collection_blocked_reason?'collection_blocked':manifest.enabled?'collection_enabled':'configured_disabled'}:{}),collection_blocked_reason:manifest.collection_blocked_reason || null,publisher_type:manifest.publisher_type || target.publisher_type,publisher_country:manifest.publisher_country || target.publisher_country}); continue; }
    source_registry.push(classifySource(manifest,true));
  }
  const empty = ids => Object.fromEntries(ids.map(id => [id,[]]));
  const indexes = { categories:empty(categoryIds), record_kinds:empty(taxonomy.record_kinds), sources:empty(manifests.filter(x=>x.enabled !== false).map(x=>x.source)), source_categories:empty(categoryIds), publisher_countries:{} };
  for (const record of opportunities) {
    for (const category of record.categories) indexes.categories[category].push(record.id);
    indexes.record_kinds[record.kind].push(record.id);
    if (!indexes.sources[record.source]) throw Error('Record source missing from adapters');
    indexes.sources[record.source].push(record.id);
  }
  for (const source of source_registry) {
    for (const category of source.categories) indexes.source_categories[category].push(source.id);
    const country = source.publisher_country || 'unknown';
    (indexes.publisher_countries[country] ||= []).push(source.id);
  }
  return { taxonomy, source_registry, indexes };
}
export function validateCategoricalCatalog(catalog) {
  const ids = catalog.opportunities.map(record => record.id);
  if (new Set(ids).size !== ids.length) throw Error('Duplicate canonical record ID');
  const sourceIds = catalog.source_registry.map(source=>source.id);
  if (new Set(sourceIds).size !== sourceIds.length) throw Error('Duplicate source registry ID');
  for (const record of catalog.opportunities) validateClassification(record);
  const expected = buildCategoricalCatalog(catalog.opportunities,catalog.sources,catalog.source_registry);
  if (JSON.stringify(catalog.taxonomy) !== JSON.stringify(taxonomy)) throw Error('Invalid taxonomy contract');
  if (JSON.stringify(catalog.indexes) !== JSON.stringify(expected.indexes)) throw Error('Derived indexes inconsistent');
  for (const adapter of catalog.sources) if (catalog.source_registry.filter(source => source.adapter_source_id === adapter.source).length !== 1) throw Error('Adapter source join must be unique');
}
