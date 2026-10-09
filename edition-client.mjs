import {BRIEF_VERSION,validateManifest,validateSection} from './brief-selection.mjs';
export const SAVED_KEY=`daily-brief-edition:v${BRIEF_VERSION}`;
export async function fetchJSON(path,{sha256,timeout=8000,cache='no-cache'}={}) {
 const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),timeout);
 try {
  const response=await fetch(path,{cache,signal:controller.signal});
  if(!response.ok) throw new Error(`${response.status} ${path}`);
  const text=await response.text();
  if(sha256) {
   const hash=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(text));
   const actual=[...new Uint8Array(hash)].map(b=>b.toString(16).padStart(2,'0')).join('');
   if(actual!==sha256) throw new Error('Published data changed; refresh the brief and retry');
  }
  return JSON.parse(text);
 } finally {clearTimeout(timer)}
}
export function readSavedEdition() {
 try {
  const saved=JSON.parse(localStorage.getItem(SAVED_KEY));validateManifest(saved.manifest);
  for(const name of Object.keys(saved.manifest.sections)) {
   validateSection(name,saved.data[name]);
   if(saved.data[name].generatedAt!==saved.manifest.sections[name].generatedAt) return null;
  }
  return saved;
 }catch{return null}
}
export function saveEdition(manifest,data) {
 try {localStorage.setItem(SAVED_KEY,JSON.stringify({manifest,data}));return true}catch{return false}
}
