#!/usr/bin/env node
import {realpathSync} from 'node:fs';
import {readFile,writeFile,readdir} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {resolve,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';

export async function buildShell(root) {
  const template=await readFile(resolve(root,'scripts/sw-template.js'),'utf8');
  const files=(await readdir(root)).filter(name=>/\.(?:html|css|m?js)$/.test(name) && name!=='sw.js');
  files.push('favicon.svg','assets/moon-waxing-gibbous.png');
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
