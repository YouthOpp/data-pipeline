import test from 'node:test';
import assert from 'node:assert/strict';
import { isOpportunityDeskListing } from '../adapters/opportunitydesk-filter.js';

test('accepts specific scholarships, fellowships and internships', () => {
  for (const title of [
    'Apple Hardware PhD Internships 2026',
    'Stanford University CISAC Fellowship 2027-2028',
    'SafeTech Africa HackLab 2026',
    'Hamburg Sustainability Conference Youth Ambassador Program 2027'
  ]) assert.equal(isOpportunityDeskListing({ title, categories: [] }), true, title);
});

test('rejects roundup articles and guidance', () => {
  for (const title of [
    '27 Scholarship Opportunities Closing in October – October 7, 2026',
    'Study in the USA: How to Find Scholarships and Turn Your American Dream into an Admission Offer',
    'New Job? Start Strong with Healthy Work Habits',
    'Top 10 Internship Opportunities Closing in November'
  ]) assert.equal(isOpportunityDeskListing({ title, categories: [] }), false, title);
});

test('rejects editorial categories while keeping real feed categories', () => {
  assert.equal(isOpportunityDeskListing({ title: 'A programme', categories: ['General Tips'] }), false);
  assert.equal(isOpportunityDeskListing({ title: 'A fellowship', categories: ['Fellowships'] }), true);
});
