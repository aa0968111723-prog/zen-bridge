import {createReadStream} from 'node:fs';
import {readFile} from 'node:fs/promises';
import {createGunzip} from 'node:zlib';
import {createInterface} from 'node:readline';
import {createHash} from 'node:crypto';
import {dirname,join} from 'node:path';
import {localDatabase,localDbPath,localDataDir} from '../lib/platform/local.ts';
const sha=text=>createHash('sha256').update(text).digest('hex');
export async function importLocalDictionaries(catalogPath=join(process.cwd(),'dictionary-data/catalog.json'),path=localDbPath(localDataDir())){
 const catalog=JSON.parse(await readFile(catalogPath,'utf8')),db=localDatabase(path),results=[];
 if(catalog.schema!==1||!Array.isArray(catalog.sources))throw Error('Invalid local dictionary catalog');
 for(const source of catalog.sources){
  if(!/^[a-z0-9-]{1,80}$/.test(source.id)||!/^[-a-z0-9.]+\.ndjson\.gz$/.test(source.file))throw Error('Invalid local dictionary path');
  const file=join(dirname(catalogPath),source.file),hash=createHash('sha256');for await(const bytes of createReadStream(file))hash.update(bytes);
  if(hash.digest('hex')!==source.data_sha256)throw Error('Local dictionary checksum mismatch');
  const old=db.prepare('SELECT version,entry_count FROM dictionary_sources WHERE id=?').get(source.id);
  if(old?.version===source.data_sha256&&old.entry_count===source.entry_count){results.push({id:source.id,status:'current',entries:old.entry_count});continue;}
  db.prepare("INSERT OR IGNORE INTO dictionary_sources(id,title,license,license_url,source_url,version,sha256,entry_count,public,updated_at) VALUES(?,?,?,?,?,'',?,0,1,?)").run(source.id,source.title,source.license,source.license_url,source.source_url,source.input_sha256,new Date().toISOString());
  const insert=db.prepare('INSERT OR IGNORE INTO dictionary_entries(id,source_id,snapshot,word,alternative,pronunciation,zh,en) VALUES(?,?,?,?,?,?,?,?)');
  const insertKey=db.prepare('INSERT OR IGNORE INTO dictionary_keys(entry_id,key) VALUES(?,?)');
  let pending=[],count=0;
  const flush=()=>{db.exec('BEGIN IMMEDIATE');try{for(const item of pending){
    if(!/^[a-f0-9]{32}$/.test(item.id)||!Array.isArray(item.keys)||item.keys.length>64||['word','alternative','pronunciation','zh','en'].some(k=>typeof item[k]!=='string'||item[k].length>8000)||item.keys.some(k=>typeof k!=='string'||k.length>200))throw Error('Invalid dictionary row');
    const id=sha(source.id+'\0'+source.data_sha256+'\0'+item.id).slice(0,32);
    insert.run(id,source.id,source.data_sha256,item.word,item.alternative,item.pronunciation,item.zh,item.en);
    for(const key of new Set(item.keys))insertKey.run(id,key);
  }db.exec('COMMIT');pending=[];}catch(error){db.exec('ROLLBACK');throw error;}};
  for await(const line of createInterface({input:createReadStream(file).pipe(createGunzip()),crlfDelay:Infinity})){if(!line)continue;pending.push(JSON.parse(line));count++;if(pending.length>=1000)flush();}if(pending.length)flush();
  if(count!==source.entry_count)throw Error('Dictionary row count mismatch');
  const actual=db.prepare('SELECT count(*) AS n FROM dictionary_entries WHERE source_id=? AND snapshot=?').get(source.id,source.data_sha256).n;
  if(actual!==count)throw Error('Stored dictionary row count mismatch');
  db.prepare('UPDATE dictionary_sources SET version=?,entry_count=?,updated_at=? WHERE id=?').run(source.data_sha256,count,new Date().toISOString(),source.id);
  results.push({id:source.id,status:'imported',entries:count});
 }
 return results;
}
if(process.argv[1]?.endsWith('import-local-dictionaries.mjs'))console.log(JSON.stringify(await importLocalDictionaries()));
