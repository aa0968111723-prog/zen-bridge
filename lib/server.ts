import {z} from 'zod';
import type {Person,Session,Segment,Memory,State,Direction} from './domain';
import {interpretationInstructions} from './interpret';
import {getEnv} from './env';
import {db,rows,one,write,DatabaseUnavailableError} from './data';
import {providerName} from './speech/resolve';
export {db,rows,one,write} from './data';
export const now=()=>new Date().toISOString();
export const id=()=>crypto.randomUUID();
const field=z.string().trim().max(8000),short=z.string().trim().min(1).max(160),personId=z.string().uuid().nullable();
export async function workspace(sessionId?:string|null):Promise<State>{
 const [people,sessions,memories]=await Promise.all([rows<Person>('SELECT * FROM people ORDER BY created_at'),rows<Session>('SELECT * FROM sessions ORDER BY created_at DESC'),rows<Memory>("SELECT * FROM memories WHERE status!='archived' ORDER BY updated_at DESC LIMIT 300")]);
 const selected=sessions.find(s=>s.id===sessionId)?.id??sessions[0]?.id??null;
 const segments=selected?(await rows<Segment>('SELECT * FROM segments WHERE session_id=? ORDER BY created_at DESC,"offset" DESC LIMIT 500',selected)).reverse():[];
 const env=getEnv(),openai=!!env.OPENAI_API_KEY,qwen=!!env.DASHSCOPE_API_KEY;
 return {people,sessions,memories,segments,sessionId:selected,connection:{openai,qwen,speech:openai||qwen,provider:providerName(env),asr:qwen?env.QWEN_LIVE_MODEL:env.OPENAI_TRANSCRIPTION_MODEL,translation:qwen?'qwen3.8-livetranslate':env.OPENAI_TRANSLATION_MODEL,hermes:'等待連接',deployTarget:env.DEPLOY_TARGET}};
}
const mutations=z.discriminatedUnion('action',[
 z.object({action:z.literal('person'),id:z.string().uuid().optional(),name:short,role:short,notes:field.default('')}),
 z.object({action:z.literal('session'),title:short,topic:field.default(''),notes:field.default('')}),
 z.object({action:z.literal('editSession'),sessionId:z.string().uuid(),title:short,topic:field.default(''),notes:field.default('')}),
 z.object({action:z.literal('sessionRole'),sessionId:z.string().uuid(),personId:z.string().uuid(),role:short}),
 z.object({action:z.literal('sessionSpeakers'),sessionId:z.string().uuid(),personIds:z.array(z.string().uuid()).max(4)}),
 z.object({action:z.literal('editMemory'),id:z.string().uuid(),personId,role:short,zh:field.min(1),en:field.min(1),meaning:field.min(1),context:field.min(1)}),
 z.object({action:z.literal('finish'),sessionId:z.string().uuid()}),
 z.object({action:z.literal('segment'),sessionId:z.string().uuid(),personId,role:short,direction:z.enum(['zh-en','en-zh']).default('zh-en'),zh:field.default(''),en:field.default(''),note:field.default(''),translate:z.boolean().default(false)}),
 z.object({action:z.literal('correct'),id:z.string().uuid(),personId,role:short,zh:field,en:field,note:field.default(''),remember:z.boolean().default(false)}),
 z.object({action:z.literal('memory'),personId,role:short.default('通用'),zh:field.min(1),en:field.min(1),meaning:field.default(''),context:field.default('')}),
 z.object({action:z.literal('memoryState'),id:z.string().uuid(),status:z.enum(['verified','archived'])}),
]);
export async function mutate(payload:unknown){
 const p=mutations.parse(payload),stamp=now(),key=id();
 if(p.action==='person'){
  if(p.id){if(!await one('SELECT id FROM people WHERE id=?',p.id))throw new Error('找不到這位講者。');await write('UPDATE people SET name=?,role=?,notes=? WHERE id=?',p.name,p.role,p.notes,p.id);return {id:p.id};}
  await write('INSERT INTO people(id,name,role,notes,created_at) VALUES(?,?,?,?,?)',key,p.name,p.role,p.notes,stamp);return {id:key};
 }
 if(p.action==='session'){await write('INSERT INTO sessions(id,title,topic,notes,created_at) VALUES(?,?,?,?,?)',key,p.title,p.topic,p.notes,stamp);return {id:key};}
 if(p.action==='editSession'){
  if(!await one('SELECT id FROM sessions WHERE id=?',p.sessionId))throw new Error('找不到這場社課。');
  await write('UPDATE sessions SET title=?,topic=?,notes=? WHERE id=?',p.title,p.topic,p.notes,p.sessionId);return {id:p.sessionId};
 }
 if(p.action==='sessionRole'){const s=await one<Session>('SELECT * FROM sessions WHERE id=?',p.sessionId);if(!s)throw new Error('找不到活動。');if(!await one('SELECT id FROM people WHERE id=?',p.personId))throw new Error('找不到講者。');const roles=JSON.parse(s.roles);roles[p.personId]=p.role;await write('UPDATE sessions SET roles=? WHERE id=?',JSON.stringify(roles),p.sessionId);return {id:p.sessionId};}
 if(p.action==='sessionSpeakers'){if(!await one('SELECT id FROM sessions WHERE id=?',p.sessionId))throw new Error('找不到活動。');for(const person of p.personIds)if(!await one('SELECT id FROM people WHERE id=? AND reference_key IS NOT NULL',person))throw new Error('請先加入這位講者的聲音樣本。');await write('UPDATE sessions SET speaker_ids=? WHERE id=?',JSON.stringify([...new Set(p.personIds)]),p.sessionId);return {id:p.sessionId};}
 if(p.action==='editMemory'){const old=await one<Memory>('SELECT * FROM memories WHERE id=?',p.id);if(!old)throw new Error('找不到例句。');if(p.personId&&!await one('SELECT id FROM people WHERE id=?',p.personId))throw new Error('找不到講者。');await db().batch([db().prepare('INSERT INTO memory_revisions(id,memory_id,payload,created_at) VALUES(?,?,?,?)').bind(key,old.id,JSON.stringify(old),stamp),db().prepare("UPDATE memories SET person_id=?,role=?,zh=?,en=?,meaning=?,context=?,status='candidate',version=version+1,updated_at=? WHERE id=?").bind(p.personId,p.role,p.zh,p.en,p.meaning,p.context,stamp,p.id)]);return {id:p.id};}
 if(p.action==='finish'){await write("UPDATE sessions SET status='finished' WHERE id=?",p.sessionId);return {id:p.sessionId};}
 if(p.action==='segment'){
  const session=await one<Session>('SELECT * FROM sessions WHERE id=?',p.sessionId);if(!session)throw new Error('請先建立活動。');
  const person=p.personId?await one<Person>('SELECT * FROM people WHERE id=?',p.personId):null;if(p.personId&&!person)throw new Error('找不到講者。');
  const source=p.direction==='en-zh'?p.en:p.zh;if(!source.trim())throw new Error(p.direction==='en-zh'?'請填入英文原文。':'請填入中文原文。');
  const translated=p.translate?await translateText(source,session,person,p.role,p.direction):null;
  const zh=p.direction==='en-zh'&&translated!==null?translated:p.zh,en=p.direction==='zh-en'&&translated!==null?translated:p.en;
  const key=await saveSegment({sessionId:p.sessionId,personId:p.personId,label:person?.name??(p.direction==='en-zh'?'英文發言者':'未指定講者'),role:p.role,direction:p.direction,zh,en,note:p.note,source:'manual'});return {id:key};
 }
 if(p.action==='correct'){
  const original=await one<Segment>('SELECT * FROM segments WHERE id=?',p.id);if(!original)throw new Error('找不到這段紀錄。');
  field.min(1).parse(original.direction==='en-zh'?p.en:p.zh);
  const person=p.personId?await one<Person>('SELECT * FROM people WHERE id=?',p.personId):null;if(p.personId&&!person)throw new Error('找不到講者。');
  const commands=[db().prepare('INSERT INTO segment_revisions(id,segment_id,payload,created_at) VALUES(?,?,?,?)').bind(id(),p.id,JSON.stringify(original),stamp),db().prepare('UPDATE segments SET person_id=?,label=?,role=?,zh=?,en=?,note=? WHERE id=?').bind(p.personId,person?.name??'待確認講者',p.role,p.zh,p.en,p.note,p.id)];
  if(p.remember){if(!p.zh.trim()||!p.en.trim()||!p.note.trim())throw new Error('請填入中英內容及原意說明，再存成確認例句。');
   const session=await one<Session>('SELECT * FROM sessions WHERE id=?',original.session_id),old=await one<Memory>('SELECT * FROM memories WHERE source_id=?',p.id);
   if(old){commands.push(db().prepare('INSERT INTO memory_revisions(id,memory_id,payload,created_at) VALUES(?,?,?,?)').bind(key,old.id,JSON.stringify(old),stamp));commands.push(db().prepare("UPDATE memories SET person_id=?,role=?,zh=?,en=?,meaning=?,status='verified',version=version+1,updated_at=? WHERE id=?").bind(p.personId,p.role,p.zh,p.en,p.note,stamp,old.id));}
   else commands.push(db().prepare("INSERT INTO memories(id,person_id,role,zh,en,meaning,context,status,source_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'verified',?,?,?)").bind(key,p.personId,p.role,p.zh,p.en,p.note,session?.topic||session?.title||'社課',p.id,stamp,stamp));
  }await db().batch(commands);return {id:p.id};
 }
 if(p.action==='memory'){
  if(p.personId&&!await one('SELECT id FROM people WHERE id=?',p.personId))throw new Error('找不到講者。');
  const dupe=await one<{id:string}>('SELECT id FROM memories WHERE zh=? AND en=? AND person_id IS ? AND role=? AND context=? AND status!=?',p.zh,p.en,p.personId,p.role,p.context,'archived');if(dupe)return dupe;
  await write('INSERT INTO memories(id,person_id,role,zh,en,meaning,context,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',key,p.personId,p.role,p.zh,p.en,p.meaning,p.context,stamp,stamp);return {id:key};
 }
 if(p.action==='memoryState'){
  const old=await one<Memory>('SELECT * FROM memories WHERE id=?',p.id);if(!old)throw new Error('找不到例句。');
  if(p.status==='verified'&&(!old.meaning.trim()||!old.context.trim()))throw new Error('確認例句需要原意說明與適用情境。');
  await db().batch([db().prepare('INSERT INTO memory_revisions(id,memory_id,payload,created_at) VALUES(?,?,?,?)').bind(key,old.id,JSON.stringify(old),stamp),db().prepare('UPDATE memories SET status=?,version=version+1,updated_at=? WHERE id=?').bind(p.status,stamp,p.id)]);return {id:p.id};
 }
}
export async function saveSegment(p:{sessionId:string;personId:string|null;label:string;role:string;direction?:Direction;zh:string;en:string;source:string;note?:string;audioKey?:string;offset?:number}){
 const key=id();await write('INSERT INTO segments(id,session_id,person_id,label,role,direction,zh,en,original_zh,original_en,note,audio_key,"offset",source,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',key,p.sessionId,p.personId,p.label,p.role,p.direction??'zh-en',p.zh,p.en,p.zh,p.en,p.note??'',p.audioKey??null,p.offset??0,p.source,now());return key;
}
export async function translateText(text:string,session:Session,person:Person|null,role:string,direction:Direction='zh-en'){
 const env=getEnv(),key=env.OPENAI_API_KEY;if(!key)throw new Error('OpenAI 尚未連接。請設定 OPENAI_API_KEY。');
 const [terms,previous]=await Promise.all([rows<Memory>("SELECT * FROM memories WHERE status='verified' AND (person_id IS NULL OR person_id=?) AND (role='通用' OR role=?) ORDER BY updated_at DESC LIMIT 30",person?.id??null,role),rows<Segment>('SELECT zh,en,label,role FROM segments WHERE session_id=? ORDER BY created_at DESC LIMIT 6',session.id)]);
 let r:Response;
 try{r=await fetch('https://api.openai.com/v1/responses',{method:'POST',headers:{Authorization:'Bearer '+key,'Content-Type':'application/json'},signal:AbortSignal.timeout(45000),body:JSON.stringify({model:env.OPENAI_TRANSLATION_MODEL,instructions:interpretationInstructions(direction),input:JSON.stringify({currentUtterance:text,direction,activity:{topic:session.topic,notes:session.notes},speaker:person?{name:person.name,notes:person.notes}:null,role,confirmedExamples:terms,previousUtterances:previous.reverse()}),max_output_tokens:1800})});}catch{throw new Error('OpenAI 翻譯連線失敗，請檢查平台網路與 OPENAI_API_KEY。');}
 if(!r.ok)throw new Error(r.status===429?'AI 服務忙碌或額度不足，請稍後重試。':'翻譯服務連線失敗，請檢查服務設定。');
 let body:{output?:{content?:{type:string;text?:string}[]}[]};
 try{body=await r.json() as typeof body;}catch{throw new Error('OpenAI 翻譯服務回傳格式不正確，請檢查 OPENAI_TRANSLATION_MODEL。');}
 const result=(body.output??[]).flatMap(o=>o.content??[]).filter(c=>c.type==='output_text').map(c=>c.text??'').join('').trim();if(!result)throw new Error('翻譯服務未回傳文字，請稍後重試。');return result;
}
export function failure(e:unknown){
 if(e instanceof DatabaseUnavailableError)return Response.json({error:e.message,code:e.code,deployTarget:getEnv().DEPLOY_TARGET},{status:503});
 if(e instanceof z.ZodError)return Response.json({error:'欄位格式不正確，請確認必填資料及長度。'},{status:400});
 const msg=e instanceof Error?e.message:'操作失敗，請稍後重試。';
 return Response.json({error:/(D1_|SQLITE_|no such table|binding)/i.test(msg)?'資料儲存尚未就緒，請稍後重試。':msg},{status:503});
}
