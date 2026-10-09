import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {assertTencentBuild} from '../scripts/tencent-build.mjs';
const folder=await mkdtemp(join(tmpdir(),'zen-target-'));
try{
 const file=join(folder,'externals.json');
 await assert.rejects(assertTencentBuild(file),/build missing/);
 for(const entries of [[],['ws'],['postgres'],{},['react','react-dom']]){
  await writeFile(file,JSON.stringify(entries));await assert.rejects(assertTencentBuild(file),/Wrong build target/);
 }
 await writeFile(file,JSON.stringify(['postgres','ws','react']));await assertTencentBuild(file);
 console.log('PASS: Tencent packaging rejects Cloudflare and incomplete builds before copying artifacts');
}finally{await rm(folder,{recursive:true,force:true});}
