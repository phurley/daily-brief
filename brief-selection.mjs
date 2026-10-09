// Shared by publication and the web brief. Widgets consume this ordered selection.
export const BRIEF_VERSION = 1;
export const SECTION_VERSIONS = {weather:'1.0.0', events:'2.0.0', news:'1.1.0', geeknews:'1.0.0', vibe:'1.0.0',starship:'1.0.0',scienceHealth:'1.0.0',rankingConfig:'1.0.0'};
const formatter = new Intl.DateTimeFormat('en-CA', {timeZone:'America/Detroit', year:'numeric', month:'2-digit', day:'2-digit'});
export function detroitDate(value = new Date()) {
  return formatter.format(value);
}
export function eventDay(value) { return value ? detroitDate(new Date(value)) : ''; }
export function rankEvents(a,b) {
  const spans = e => eventDay(e.start) !== eventDay(e.end || e.start);
  return Number(spans(a))-Number(spans(b)) || (b.score || 0)-(a.score || 0) || Date.parse(a.start)-Date.parse(b.start) || a.id.localeCompare(b.id);
}
export function activeEvents(events, date, now = Date.now()) {
  return [...new Map(events.map(e=>[e.id,e])).values()].filter(e=>eventDay(e.start)<=date && eventDay(e.end||e.start)>=date && (date!==detroitDate(new Date(now)) || !e.end || Date.parse(e.end)>now)).sort(rankEvents);
}
export function bestBets(events,date,now=Date.now(),limit=6) { return activeEvents(events,date,now).slice(0,limit); }
export function validateSection(name, data) {
  if (!data || data.schemaVersion !== SECTION_VERSIONS[name] || !Number.isFinite(Date.parse(data.generatedAt))) throw new Error(`Unsupported ${name} contract`);
  const list = {weather:'daily',events:'events',news:'stories',geeknews:'stories',vibe:'messages'}[name];
  if (list && !Array.isArray(data[list])) throw new Error(`Invalid ${name} content`);
  if (name === 'weather' && !data.location) throw new Error('Missing weather location');
  if (name === 'events' && data.events.some(e=>!e.id || !e.title || !Number.isFinite(Date.parse(e.start)))) throw new Error('Invalid event');
  if(name==='rankingConfig' && (!data.preferences || !data.weights)) throw new Error('Invalid public ranking config');
  if(name==='starship' && !data.forecast) throw new Error('Invalid Starship forecast');
  if(name==='scienceHealth' && !data.sources) throw new Error('Invalid science health');
  return data;
}
export function validateManifest(value) {
  if (value?.schemaVersion!==BRIEF_VERSION || !/^[a-f0-9]{64}$/.test(value.editionId || '') || !/^\d{4}-\d{2}-\d{2}$/.test(value.editionDate || '')) throw new Error('Unsupported brief edition');
  for (const name of Object.keys(SECTION_VERSIONS)) {
    const section=value.sections?.[name];
    if (!section || section.schemaVersion!==SECTION_VERSIONS[name] || !/^[a-f0-9]{64}$/.test(section.sha256 || '') || section.url!==`editions/${name.toLowerCase()}-${section.sha256}.json`) throw new Error(`Invalid ${name} manifest`);
  }
  return value;
}
