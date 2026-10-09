import { rows, batch } from './data';
import { z } from 'zod';

export type DictionarySource={id:string;title:string;license:string;license_url:string;source_url:string;version:string;sha256:string;entry_count:number;public:number;updated_at:string};
export type DictionaryEntry={id:string;source_id:string;word:string;alternative:string;pronunciation:string;zh:string;en:string;source_title:string;license:string;license_url:string;source_url:string};
export const dictionaryKey=(value:string)=>value.normalize('NFKC').trim().toLowerCase();
const text=(limit:number,min=0)=>z.string().min(min).max(limit).refine(s=>!/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(s));
export const dictionaryImport=z.object({title:text(160,1),license:text(300).default('使用者提供，僅主持機可查詢'),entries:z.array(z.object({word:text(160,1),alternative:text(160).default(''),pronunciation:text(200).default(''),zh:text(8000).default(''),en:text(8000).default('')})).min(1).max(5000)});

export async function dictionarySources(privateAllowed=false){
 return rows<DictionarySource>('SELECT * FROM dictionary_sources WHERE (public=1 OR ?=1) ORDER BY title',privateAllowed?1:0);
}
export async function lookupDictionary(query:string,privateAllowed=false,limit=30){
 const key=dictionaryKey(query).slice(0,160);if(!key)return [];
 // Indexed exact/prefix keys avoid scanning millions of definitions.
 return rows<DictionaryEntry>(`SELECT DISTINCT e.id,e.source_id,e.word,e.alternative,e.pronunciation,e.zh,e.en,s.title AS source_title,s.license,s.license_url,s.source_url,CASE WHEN lower(e.word)=? OR lower(e.alternative)=? THEN 0 WHEN k.key=? THEN 1 ELSE 2 END AS match_rank
 FROM dictionary_keys k JOIN dictionary_entries e ON e.id=k.entry_id JOIN dictionary_sources s ON s.id=e.source_id
 WHERE e.snapshot=s.version AND (s.public=1 OR ?=1) AND k.key>=? AND k.key<?
 ORDER BY match_rank,e.word,e.id LIMIT ?`,key,key,key,privateAllowed?1:0,key,key+'\uffff',Math.max(1,Math.min(limit,50)));
}
export async function dictionaryReferences(input:string,privateAllowed=false){
 const normalized=dictionaryKey(input).slice(0,600),keys=new Set<string>();
 for(const word of normalized.match(/[a-z][a-z'-]{1,39}/g)??[])if(keys.size<80)keys.add(word);
 for(const phrase of normalized.match(/[\p{Script=Han}]+/gu)??[]){
  for(let length=Math.min(8,phrase.length);length>=1;length--)for(let i=0;i+length<=phrase.length&&keys.size<96;i++)keys.add(phrase.slice(i,i+length));
 }
 if(!keys.size)return [];
 try {
  const terms=Array.from(keys),placeholders=terms.map(()=>'?').join(',');
  const result=await rows<DictionaryEntry>(`SELECT DISTINCT e.id,e.source_id,e.word,e.alternative,e.pronunciation,e.zh,e.en,s.title AS source_title,s.license,s.license_url,s.source_url,CASE WHEN lower(e.word) IN (${placeholders}) OR lower(e.alternative) IN (${placeholders}) THEN 0 ELSE 1 END AS match_rank
   FROM dictionary_keys k JOIN dictionary_entries e ON e.id=k.entry_id JOIN dictionary_sources s ON s.id=e.source_id
   WHERE e.snapshot=s.version AND (s.public=1 OR ?=1) AND k.key IN (${placeholders}) ORDER BY match_rank,e.word,e.id LIMIT 12`,...terms,...terms,privateAllowed?1:0,...terms);
  let remaining=2400;const references=[];for(const e of result){if(remaining<=0)break;const zh=e.zh.slice(0,Math.min(240,remaining));remaining-=zh.length;const en=e.en.slice(0,Math.min(240,remaining));remaining-=en.length;references.push({word:e.word,zh,en,source:e.source_title});}return references;
 } catch {return [];}
}
export async function importPersonalDictionary(input:unknown){
 const parsed=dictionaryImport.parse(input),source='user-'+crypto.randomUUID(),snapshot=crypto.randomUUID(),stamp=new Date().toISOString();
 const commands=[{sql:'INSERT INTO dictionary_sources(id,title,license,version,public,entry_count,updated_at) VALUES(?,?,?,?,?,?,?)',args:[source,parsed.title,parsed.license,snapshot,0,parsed.entries.length,stamp]}];
 for(const entry of parsed.entries){const id=crypto.randomUUID();commands.push({sql:'INSERT INTO dictionary_entries(id,source_id,snapshot,word,alternative,pronunciation,zh,en) VALUES(?,?,?,?,?,?,?,?)',args:[id,source,snapshot,entry.word,entry.alternative,entry.pronunciation,entry.zh,entry.en]});for(const key of new Set([entry.word,entry.alternative].map(dictionaryKey).filter(Boolean)))commands.push({sql:'INSERT INTO dictionary_keys(entry_id,key) VALUES(?,?)',args:[id,key]});}
 await batch(commands);return {sourceId:source,count:parsed.entries.length};
}
