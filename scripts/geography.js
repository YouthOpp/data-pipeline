// Only explicit location phrases in original titles establish destination evidence.
const names={AT:['Austria'],BE:['Belgium','Belgio'],BG:['Bulgaria'],HR:['Croatia','Croazia'],CY:['Cyprus','Cipro'],CZ:['Czechia','Czech Republic'],DK:['Denmark','Danimarca'],EE:['Estonia'],FI:['Finland','Finlandia'],FR:['France','Francia'],DE:['Germany','Germania'],GR:['Greece','Grecia'],HU:['Hungary','Ungheria'],IE:['Ireland','Irlanda'],IT:['Italy','Italia'],LV:['Latvia','Lettonia'],LT:['Lithuania','Lituania'],LU:['Luxembourg','Lussemburgo'],MT:['Malta'],NL:['Netherlands','Paesi Bassi'],PL:['Poland','Polonia'],PT:['Portugal','Portogallo'],RO:['Romania'],SK:['Slovakia','Slovacchia'],SI:['Slovenia'],ES:['Spain','Spagna'],SE:['Sweden','Svezia'],US:['United States','USA'],GB:['United Kingdom','UK'],TR:['Turkey','Türkiye','Turchia'],CH:['Switzerland','Svizzera'],CA:['Canada'],AU:['Australia']};
export function titleDestinations(title,url){
 const evidence=[];
 for(const [country,aliases] of Object.entries(names))for(const name of aliases){
  const escaped=name.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
  const match=String(title).match(new RegExp(`\\b(?:in|at|to|a|en|negli|nella|nel)\\s+(?:the\\s+)?${escaped}\\b`,'i'));
  if(match&&!evidence.some(x=>x.country===country))evidence.push({country,method:'explicit-title-location',url,text:match[0]});
 }
 return {host_countries:evidence.map(x=>x.country),country_evidence:evidence};
}
