import {readFile} from 'node:fs/promises';
import {validateManifest,verifyAsset} from './release-state.js';
import {inspectCoverage} from './verify-catalog.js';
const directory=process.argv[2]||'published-verification';
const manifest=validateManifest(JSON.parse(await readFile(`${directory}/manifest.json`,'utf8')),{requireContributors:true});
for(const [name,expected] of Object.entries(manifest.assets)){
 if(!['catalog.json','contributors.json','collection-report.json'].includes(name))throw Error('Unexpected published asset');
 verifyAsset(await readFile(`${directory}/${name}`),expected);
}
const catalog=JSON.parse(await readFile(`${directory}/catalog.json`,'utf8'));
const sources=JSON.parse(await readFile('data/sources/sources.json','utf8'));
const coverage=inspectCoverage(catalog,sources);
const report=JSON.parse(await readFile(`${directory}/collection-report.json`,'utf8'));
if(JSON.stringify(report.coverage)!==JSON.stringify(coverage))throw Error('Published source coverage does not match the actual catalog');
console.log(JSON.stringify({release_tag:manifest.release_tag,records:coverage.total_records,sources:coverage.total_sources,collected_sources:coverage.collected_sources,all_sources_have_opportunities:coverage.all_sources_have_opportunities,integrity:'verified'}));
