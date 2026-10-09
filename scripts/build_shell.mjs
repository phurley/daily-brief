#!/usr/bin/env node
import {realpathSync} from 'node:fs';
import {readFile,writeFile,readdir} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {resolve,dirname,relative,sep} from 'node:path';
import {fileURLToPath} from 'node:url';

export async function buildShell(root) {
  const template=await readFile(resolve(root,'scripts/sw-template.js'),'utf8');
  const files=(await readdir(root)).filter(name=>/\.(?:html|css|m?js)$/.test(name) && name!=='sw.js');
  files.push('favicon.svg','assets/moon-waxing-gibbous.png');
  // Follow relative imports as modules move into subdirectories. A nested module
  // change must invalidate the shell even when its importing file is unchanged.
  const known=new Set(files);
  for(let index=0;index<files.length;index++) {
    const name=files[index];
    if(!/\.(?:m?js|css)$/.test(name)) continue;
    const text=await readFile(resolve(root,name),'utf8');
    const pattern=name.endsWith('.css') ? /url\(\s*["']?([^"')\s]+)["']?\s*\)/g : /(?:\bfrom\s*|\bimport\s*(?:\(\s*)?)["'](\.[^"']+)["']/g;
    for(const match of text.matchAll(pattern)) {
      const target=match[1].split(/[?#]/)[0];
      if(!target || /^(?:[a-z]+:|\/)/i.test(target)) continue;
      const dependency=relative(root,resolve(root,dirname(name),target)).split(sep).join('/');
      if(dependency.startsWith('../')) throw new Error(`Shell dependency escapes root: ${dependency}`);
      if(!known.has(dependency)) {known.add(dependency);files.push(dependency);}
    }
  }
  files.sort();
  const hash=createHash('sha256').update(template);
  for(const file of files) hash.update(file+'\0').update(await readFile(resolve(root,file))).update('\0');
  const version=hash.digest('hex');
  const worker=template.replace('__SHELL_HASH__',version).replace('__SHELL_FILES__',JSON.stringify(files));
  await writeFile(resolve(root,'sw.js'),worker);
  return version;
}
if(process.argv[1] && realpathSync(process.argv[1])===realpathSync(fileURLToPath(import.meta.url))) {
  console.log(`Browser shell ${await buildShell(resolve(process.argv[2] || dirname(fileURLToPath(import.meta.url))+'/..'))}`);
}
