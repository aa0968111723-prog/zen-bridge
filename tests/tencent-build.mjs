import assert from 'node:assert/strict';
import {mkdtemp,writeFile,readFile,rm} from 'node:fs/promises';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {assertTencentBuild} from '../scripts/tencent-build.mjs';
import {excludeIndexedDbFixture} from '../scripts/tencent-test-dependencies.mjs';
const manifest=JSON.parse(await readFile(new URL('../package.json',import.meta.url),'utf8'));
const lock=await readFile(new URL('../pnpm-lock.yaml',import.meta.url),'utf8');
const excluded=excludeIndexedDbFixture(manifest,lock);
assert.deepEqual(excluded.manifest.dependencies,manifest.dependencies);
assert.equal(manifest.devDependencies['fake-indexeddb'],'6.2.4');
assert.equal(excluded.manifest.devDependencies['fake-indexeddb'],undefined);
assert.ok(!excluded.lock.includes('fake-indexeddb'));
assert.throws(()=>excludeIndexedDbFixture({...manifest,dependencies:{...manifest.dependencies,'fake-indexeddb':'6.2.4'}},lock),/test-only/);
assert.throws(()=>excludeIndexedDbFixture(manifest,lock+'\n  extra: fake-indexeddb@6.2.4\n'),/additional/);
assert.throws(()=>excludeIndexedDbFixture(manifest,lock.replace('specifier: 6.2.4','specifier: 6.2.5')),/lock shape/);
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
