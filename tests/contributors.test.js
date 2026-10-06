import test from 'node:test';
import assert from 'node:assert/strict';
import {repositories, summarizeCommits} from '../scripts/contributors.js';
test('production contributor history uses the canonical YouthOpp repositories',()=>{
 assert.deepEqual(repositories,['YouthOpp/.github','YouthOpp/youthopp.github.io','YouthOpp/data-pipeline']);
});
test('attribution deduplicates histories and excludes merges, automation, and unresolved identities',()=>{
 const base={repository:'YouthOpp/.github',parents:[],name:'Example',email:'123+student@users.noreply.github.com',subject:'Improve docs'};
 const result=summarizeCommits([{...base,sha:'1'},{...base,sha:'1'},{...base,sha:'2',parents:['a','b']},{...base,sha:'3',subject:'Open AI agent: update'},{...base,sha:'4',email:'private@example.org'},{...base,sha:'5',email:'private@example.org',login:'another-student'}]);
 assert.equal(result.unique_commits,5); assert.equal(result.excluded_commits,2); assert.equal(result.unresolved_commits,1); assert.equal(result.contributors.length,2); assert.ok(result.contributors.every(x=>x.score===1));
});
