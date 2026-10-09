import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {readFileSync} from 'node:fs';
import {runWithEnv} from '../lib/env';
import {dictionarySources,lookupDictionary,dictionaryReferences,importPersonalDictionary} from '../lib/dictionary';
import {GET,POST} from '../app/api/dictionaries/route';

// Actual SQLite execution checks parameter ordering, joins, snapshot visibility,
// private access and rollback rather than returning canned query results.
const sql=new DatabaseSync(':memory:');
sql.exec(readFileSync('drizzle/0003_dictionary.sql','utf8').replaceAll('--> statement-breakpoint',''));
const execute=(command:{sql:string;args:unknown[]})=>{
 const statement=sql.prepare(command.sql);
 if(/^\s*SELECT/i.test(command.sql))return statement.all(...command.args as (string|number|null)[]);
 statement.run(...command.args as (string|number|null)[]);return [];
};
const d1={prepare(query:string){return {bind(...args:unknown[]){const command={sql:query,args};return {async all(){return {results:execute(command)};},command};}};},async batch(commands:{command:{sql:string;args:unknown[]}}[]){sql.exec('BEGIN');try{for(const c of commands)execute(c.command);sql.exec('COMMIT');}catch(e){sql.exec('ROLLBACK');throw e;}}} as unknown as D1Database;
const token=crypto.randomUUID();
try {
 const bindings={DB:d1,DEPLOY_TARGET:'cloudflare',BREEZE_AGENT_TOKEN:token};
 await runWithEnv(bindings,async()=>{
  sql.exec("INSERT INTO dictionary_sources(id,title,license,version,public,entry_count,updated_at) VALUES('open','Open','MIT','new',1,2,'now')");
  const insert=sql.prepare('INSERT INTO dictionary_entries(id,source_id,snapshot,word,zh,en) VALUES(?,?,?,?,?,?)');
  insert.run('one','open','new','meditation','靜坐','');insert.run('two','open','new','meditative','冥想的','');insert.run('old','open','old','meditation','舊版本','');
  for(const id of ['one','old'])sql.prepare('INSERT INTO dictionary_keys VALUES(?,?)').run(id,'meditation');
  sql.prepare('INSERT INTO dictionary_keys VALUES(?,?)').run('two','meditative');
  const imported=await importPersonalDictionary({title:'My words',entries:[{word:'meditation',zh:'私人釋義'}]});
  assert.equal(imported.count,1);
  assert.equal((await dictionarySources()).length,1);assert.equal((await dictionarySources(true)).length,2);
  assert.equal((await lookupDictionary('ＭＥＤＩＴＡＴＩＯＮ')).length,1);
  assert.equal((await lookupDictionary('medit')).length,2);
  assert.equal((await lookupDictionary('meditation',true)).length,2);
  assert.deepEqual(await lookupDictionary("' OR 1=1--"),[]);
  assert.equal((await dictionaryReferences('meditation')).length,1);
  assert.equal((await dictionaryReferences('meditation',true)).length,2);
  const publicResult=await GET(new Request('http://test/api/dictionaries?q=meditation'));
  assert.equal((await publicResult.json() as {privateAllowed:boolean}).privateAllowed,false);
  const req=(body:BodyInit,authorized=true)=>new Request('http://test/api/dictionaries',{method:'POST',headers:authorized?{'x-zen-host':token}:{},body});
  assert.equal((await POST(req('{}',false))).status,403);
  assert.equal((await POST(req('{'))).status,400);
  assert.equal((await POST(req(JSON.stringify({title:'x',entries:[{word:'\u0000'}]})))).status,400);
  const before=sql.prepare('SELECT count(*) AS n FROM dictionary_sources').get()!.n;
  const oversized=new ReadableStream({start(c){c.enqueue(new Uint8Array(2_000_001));c.close();}});
  const large=new Request('http://test/api/dictionaries',{method:'POST',headers:{'x-zen-host':token},body:oversized,duplex:'half'} as RequestInit);
  assert.equal((await POST(large)).status,413);
  assert.equal(sql.prepare('SELECT count(*) AS n FROM dictionary_sources').get()!.n,before);
 });
 console.log('Dictionary indexed search, snapshots, private authorization, context and bounded import passed.');
}finally{sql.close();}
