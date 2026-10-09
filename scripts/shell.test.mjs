import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,copyFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {buildShell} from './build_shell.mjs';
test('deployment cache changes with shell contents, including modules, but not data-only updates',async()=>{
 const root=await mkdtemp(resolve(tmpdir(),'brief-shell-'));
 try {
  await mkdir(resolve(root,'scripts'));await mkdir(resolve(root,'assets'));
  await copyFile(new URL('./sw-template.js',import.meta.url),resolve(root,'scripts/sw-template.js'));
  for(const name of ['index.html','app.js','starship.mjs','favicon.svg','assets/moon-waxing-gibbous.png']) await writeFile(resolve(root,name),name);
  const old=await buildShell(root);assert.equal(await buildShell(root),old);
  await writeFile(resolve(root,'starship.json'),'new data');assert.equal(await buildShell(root),old);
  await writeFile(resolve(root,'starship.mjs'),'updated module');const updated=await buildShell(root);assert.notEqual(updated,old);
  await writeFile(resolve(root,'app.js'),'updated UI');assert.notEqual(await buildShell(root),updated);
  await mkdir(resolve(root,'nested'));await writeFile(resolve(root,'nested/dependency.mjs'),'export const value=1;');
  await writeFile(resolve(root,'starship.mjs'),"export {value} from './nested/dependency.mjs?v=1';");
  const nested=await buildShell(root);
  await writeFile(resolve(root,'nested/dependency.mjs'),'export const value=2;');
  assert.notEqual(await buildShell(root),nested);
  const worker=await readFile(resolve(root,'sw.js'),'utf8');assert.ok(worker.includes('starship.mjs'));assert.ok(!worker.includes('__SHELL_HASH__'));
 } finally {await rm(root,{recursive:true,force:true});}
});
