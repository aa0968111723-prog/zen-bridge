import { mkdir, cp, writeFile, readdir, rm } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { execFileSync } from 'node:child_process';
const root=process.cwd();
const output=resolve(process.argv[2]||'work/tencent-release');
if(output===root||root.startsWith(output+'/'))throw new Error('Use a separate artifact directory');
await mkdir(output,{recursive:true});
for(const name of ['dist','public','drizzle-pg','scripts','lib','app','components','db','.openai','package.json','pnpm-lock.yaml','vite.config.ts','next.config.ts','tsconfig.json'])await cp(resolve(name),join(output,name),{recursive:true});
await mkdir(join(output,'sidecar'),{recursive:true});
await cp('sidecar/breeze-gateway.mjs',join(output,'sidecar/breeze-gateway.mjs'));
await cp('sidecar/zen-front.mjs',join(output,'sidecar/zen-front.mjs'));
try{await cp('dictionary-data',join(output,'dictionary-data'),{recursive:true});}catch(error){if(error.code!=='ENOENT')throw error;}
const revision=execFileSync('git',['rev-parse','HEAD'],{encoding:'utf8'}).trim();
await writeFile(join(output,'REVISION'),revision+'\n');
console.log('Tencent runtime prepared:',output);
