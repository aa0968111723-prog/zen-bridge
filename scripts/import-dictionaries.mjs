import {createReadStream} from 'node:fs';
import {readFile} from 'node:fs/promises';
import {createGunzip} from 'node:zlib';
import {createInterface} from 'node:readline';
import {createHash} from 'node:crypto';
import {dirname,join,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {queryPostgres,batchPostgres,withPostgresImportLock} from '../lib/platform/node.ts';

const digest=value=>createHash('sha256').update(value).digest('hex');
async function fileHash(path){const h=createHash('sha256');for await(const chunk of createReadStream(path))h.update(chunk);return h.digest('hex');}
export async function importDictionaries(catalogPath=join(process.cwd(),'dictionary-data/catalog.json')){
 if(!process.env.DATABASE_URL)return {status:'database-not-configured'};
 return withPostgresImportLock(process.env.DATABASE_URL,()=>importCatalog(catalogPath));
}
async function importCatalog(catalogPath){
 let catalog;try{catalog=JSON.parse(await readFile(catalogPath,'utf8'));}catch(error){if(error.code==='ENOENT')return {status:'no-catalog'};throw error;}
 const url=process.env.DATABASE_URL,folder=dirname(resolve(catalogPath)),results=[];
 if(catalog.schema!==1||!Array.isArray(catalog.sources)||catalog.sources.length>20)throw Error('Invalid dictionary catalog');
 for(const source of catalog.sources){
  if(!/^[a-z0-9-]{1,80}$/.test(source.id)||!/^[-a-z0-9.]+\.ndjson\.gz$/.test(source.file)||!source.license||!/^https:\/\//.test(source.source_url)||!/^https:\/\//.test(source.license_url)||!Number.isSafeInteger(source.entry_count)||source.entry_count<1||source.entry_count>2_000_000||! /^[a-f0-9]{64}$/.test(source.data_sha256)||! /^[a-f0-9]{64}$/.test(source.input_sha256))throw Error('Invalid dictionary catalog');
  const path=join(folder,source.file),snapshot=source.data_sha256;
  if(await fileHash(path)!==snapshot)throw Error('Dictionary data checksum mismatch');
  const [old]=await queryPostgres(url,'SELECT version,entry_count FROM dictionary_sources WHERE id=$1',[source.id]);
  if(old?.version===snapshot&&Number(old.entry_count)===source.entry_count){results.push({id:source.id,status:'current',entries:source.entry_count});continue;}
  await queryPostgres(url,`INSERT INTO dictionary_sources(id,title,license,license_url,source_url,version,sha256,entry_count,public,updated_at)
   VALUES($1,$2,$3,$4,$5,'',$6,0,1,$7) ON CONFLICT(id) DO UPDATE SET title=EXCLUDED.title,license=EXCLUDED.license,license_url=EXCLUDED.license_url,source_url=EXCLUDED.source_url`,[source.id,source.title,source.license,source.license_url,source.source_url,source.input_sha256,new Date().toISOString()]);
  let pending=[],count=0;
  async function flush(){
   if(!pending.length)return;
   const entries=[],entryValues=[],keys=[],keyValues=[];
   for(const item of pending){
    const id=digest(source.id+'\0'+snapshot+'\0'+item.id).slice(0,32);
    entries.push('('+Array.from({length:8},(_,i)=>'$'+(entryValues.length+i+1)).join(',')+')');entryValues.push(id,source.id,snapshot,item.word,item.alternative||'',item.pronunciation||'',item.zh||'',item.en||'');
    for(const key of new Set(item.keys.slice(0,64))){keys.push('($'+(keyValues.length+1)+',$'+(keyValues.length+2)+')');keyValues.push(id,key);}
   }
   const commands=[{sql:'INSERT INTO dictionary_entries(id,source_id,snapshot,word,alternative,pronunciation,zh,en) VALUES '+entries.join(',')+' ON CONFLICT(id) DO NOTHING',args:entryValues}];
   if(keys.length)commands.push({sql:'INSERT INTO dictionary_keys(entry_id,key) VALUES '+keys.join(',')+' ON CONFLICT(entry_id,key) DO NOTHING',args:keyValues});
   await batchPostgres(url,commands);pending=[];
  }
  const lines=createInterface({input:createReadStream(path).pipe(createGunzip()),crlfDelay:Infinity});
  for await(const line of lines){if(!line)continue;const item=JSON.parse(line);
   const valid=(value,max)=>typeof value==='string'&&value.length<=max&&!/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(value);
   if(!/^[a-f0-9]{32}$/.test(item.id)||!valid(item.word,160)||!item.word||!valid(item.alternative,160)||!valid(item.pronunciation,200)||!valid(item.zh,8000)||!valid(item.en,8000)||!Array.isArray(item.keys)||item.keys.length>64||item.keys.some(k=>!valid(k,160)||!k||/[\r\n\t]/.test(k)))throw Error('Invalid dictionary entry');
   pending.push(item);count++;if(count>source.entry_count)throw Error('Dictionary entry count mismatch');if(pending.length>=400)await flush();}
  await flush();if(count!==source.entry_count)throw Error('Dictionary entry count mismatch');
  await queryPostgres(url,'UPDATE dictionary_sources SET version=$1,sha256=$2,entry_count=$3,updated_at=$4 WHERE id=$5',[snapshot,source.input_sha256,count,new Date().toISOString(),source.id]);
  results.push({id:source.id,status:'imported',entries:count});
 }
 return {status:'ready',sources:results};
}
if(process.argv[1]&&resolve(process.argv[1])===fileURLToPath(import.meta.url)){
 importDictionaries(process.argv[2]).then(result=>console.log(JSON.stringify(result))).catch(()=>{console.error('Dictionary import failed; existing dictionaries and user data retained');process.exitCode=1;});
}
