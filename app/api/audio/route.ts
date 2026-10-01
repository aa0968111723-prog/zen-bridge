export const dynamic='force-dynamic';
import {z} from 'zod';
import {bindings,one,rows,id,saveSegment,translateText,failure} from '@/lib/server';
import type {Person,Session} from '@/lib/domain';
export async function POST(r:Request){try{
 if(Number(r.headers.get('content-length')??0)>6_000_000)throw new Error('單段錄音請小於 5 MB。');
 const apiKey=bindings().OPENAI_API_KEY;if(!apiKey){await r.arrayBuffer();throw new Error('OpenAI 尚未連接，無法進行語音辨識。');}
 const form=await r.formData(),file=form.get('audio');
 const direction=z.enum(['zh-en','en-zh']).parse(form.get('direction')??'zh-en');
 const sid=z.string().uuid().parse(form.get('sessionId')),auto=form.get('auto')==='true',personId=String(form.get('personId')??''),role=String(form.get('role')??'講者').slice(0,80),offset=Number(form.get('offset')??0);
 if(!(file instanceof File)||file.size>5_000_000||!file.size||!Number.isFinite(offset)||offset<0||offset>86400)throw new Error('錄音資料格式不正確。');
 const session=await one<Session>('SELECT * FROM sessions WHERE id=?',sid);if(!session)throw new Error('找不到活動。');
 const person=personId?await one<Person>('SELECT * FROM people WHERE id=?',personId):null;
 const out=new FormData();out.set('file',file,file.name);out.set('model',auto?'gpt-4o-transcribe-diarize':bindings().OPENAI_TRANSCRIPTION_MODEL??'gpt-transcribe');
 const selected:string[]=JSON.parse(session.speaker_ids);const references=auto?await rows<Person>('SELECT * FROM people WHERE reference_key IS NOT NULL ORDER BY created_at'):[];const known=(selected.length?references.filter(p=>selected.includes(p.id)):references).slice(0,4);
 if(auto){out.set('response_format','diarized_json');out.set('chunking_strategy','auto');for(const p of known){const object=await bindings().BUCKET?.get(p.reference_key!);if(object){const bytes=new Uint8Array(await object.arrayBuffer());let s='';for(const b of bytes)s+=String.fromCharCode(b);out.append('known_speaker_names[]',p.id);out.append('known_speaker_references[]','data:'+p.reference_type+';base64,'+btoa(s));}}}
 else{out.set('response_format','json');out.append('languages[]',direction==='en-zh'?'en':'zh-tw');out.set('prompt','Tamkang University Zen Club / 淡江禪學社。主題：'+session.topic.slice(0,250)+'。'+(person?.notes.slice(0,400)??''));}
 const result=await fetch('https://api.openai.com/v1/audio/transcriptions',{method:'POST',headers:{Authorization:'Bearer '+apiKey},body:out,signal:AbortSignal.timeout(55000)});
 if(!result.ok)throw new Error(result.status===429?'語音服務額度不足或忙碌，請稍後重試。':'語音辨識連線失敗，請檢查服務設定。');
 const body=await result.json() as {text?:string;segments?:{text:string;speaker:string;start:number}[]};
 const parts=auto?(body.segments??[]):[{text:body.text??'',speaker:personId,start:0}],saved:string[]=[];
 const audioKey='recordings/'+sid+'/'+id();if(bindings().BUCKET)await bindings().BUCKET!.put(audioKey,await file.arrayBuffer(),{httpMetadata:{contentType:file.type}});
 for(const part of parts){if(!part.text.trim())continue;const detected=auto?known.find(p=>p.id===part.speaker)??null:person;const currentRole=auto?(JSON.parse(session.roles)[detected?.id??'']??detected?.role??'待確認'):role;let translated='',note='';try{translated=await translateText(part.text,session,detected,currentRole,direction);}catch(e){note=e instanceof Error?e.message:'翻譯尚未完成。';}
 const key=await saveSegment({sessionId:sid,personId:detected?.id??null,label:detected?.name??(direction==='en-zh'?'英文發言者':'待確認講者'),role:currentRole,direction,zh:direction==='zh-en'?part.text:translated,en:direction==='en-zh'?part.text:translated,note,source:auto?'automatic':'microphone',audioKey,offset:offset+(part.start??0)});saved.push(key);}
 return Response.json({ids:saved});
 }catch(e){return failure(e);}}
