export const dynamic='force-dynamic';
import {z} from 'zod';
import {bindings,one,rows,id,saveSegment,translateText,failure} from '@/lib/server';
import {hotwordPhrases,qwenLiveTranslate} from '@/lib/qwen-live';
import type {Memory,Person,Session} from '@/lib/domain';
export async function POST(r:Request){try{
 if(Number(r.headers.get('content-length')??0)>6_000_000)throw new Error('單段錄音請小於 5 MB。');
 const env=bindings();
 const qwen=!!env.DASHSCOPE_API_KEY;
 const openai=!!env.OPENAI_API_KEY;
 const provider=env.SPEECH_PROVIDER??(qwen?'qwen':'openai');
 if(provider==='qwen'&&!qwen){await r.arrayBuffer();throw new Error('阿里雲尚未連接。請在伺服器設定 DASHSCOPE_API_KEY，不要寫進程式。');}
 if(provider!=='qwen'&&!openai){await r.arrayBuffer();throw new Error('OpenAI 尚未連接，無法進行語音辨識。');}
 const form=await r.formData(),file=form.get('audio');
 const direction=z.enum(['zh-en','en-zh']).parse(form.get('direction')??'zh-en');
 const sid=z.string().uuid().parse(form.get('sessionId')),auto=form.get('auto')==='true',personId=String(form.get('personId')??''),role=String(form.get('role')??'講者').slice(0,80),offset=Number(form.get('offset')??0);
 if(!(file instanceof File)||file.size>5_000_000||!file.size||!Number.isFinite(offset)||offset<0||offset>86400)throw new Error('錄音資料格式不正確。');
 const session=await one<Session>('SELECT * FROM sessions WHERE id=?',sid);if(!session)throw new Error('找不到活動。');
 const person=personId?await one<Person>('SELECT * FROM people WHERE id=?',personId):null;
 const audioBytes=new Uint8Array(await file.arrayBuffer());
 const audioKey='recordings/'+sid+'/'+id();if(env.BUCKET)await env.BUCKET.put(audioKey,audioBytes,{httpMetadata:{contentType:file.type}});
 const saved:string[]=[];
 if(provider==='qwen'){
  const memories=await rows<Memory>("SELECT * FROM memories WHERE status='verified' ORDER BY updated_at DESC LIMIT 200");
  const phrases=hotwordPhrases(memories,direction);
  const live=await qwenLiveTranslate(audioBytes,{apiKey:env.DASHSCOPE_API_KEY!,region:env.DASHSCOPE_REGION,direction,phrases});
  let zh=direction==='zh-en'?live.source:live.translation,en=direction==='zh-en'?live.translation:live.source,note=`Qwen3.8 熱詞 ${Object.keys(phrases).length} 條，不是模型微調。`;
  if(direction==='zh-en'&&zh.trim()&&!en.trim()&&openai){try{en=await translateText(zh,session,person,role,direction);}catch(e){note=(e instanceof Error?e.message:'翻譯尚未完成。')+' '+note;}}
  if(direction==='en-zh'&&en.trim()&&!zh.trim()&&openai){try{zh=await translateText(en,session,person,role,direction);}catch(e){note=(e instanceof Error?e.message:'翻譯尚未完成。')+' '+note;}}
  if(!zh.trim()&&!en.trim())throw new Error('這一段沒有辨識出文字。請靠近麥克風再試。');
  const key=await saveSegment({sessionId:sid,personId:person?.id??null,label:person?.name??(direction==='en-zh'?'英文發言者':'待確認講者'),role:auto?(JSON.parse(session.roles)[person?.id??'']??person?.role??'待確認'):role,direction,zh,en,note:auto?note+'阿里雲這條不套用講者聲紋。':note,source:auto?'automatic':'microphone',audioKey,offset});
  saved.push(key);
  return Response.json({ids:saved,provider:'qwen3.8-livetranslate'});
 }
 const apiKey=env.OPENAI_API_KEY!;
 const out=new FormData();out.set('file',new File([audioBytes],file.name,{type:file.type}));out.set('model',auto?'gpt-4o-transcribe-diarize':env.OPENAI_TRANSCRIPTION_MODEL??'gpt-transcribe');
 const selected:string[]=JSON.parse(session.speaker_ids);const references=auto?await rows<Person>('SELECT * FROM people WHERE reference_key IS NOT NULL ORDER BY created_at'):[];const known=(selected.length?references.filter(p=>selected.includes(p.id)):references).slice(0,4);
 if(auto){out.set('response_format','diarized_json');out.set('chunking_strategy','auto');for(const p of known){const object=await env.BUCKET?.get(p.reference_key!);if(object){const bytes=new Uint8Array(await object.arrayBuffer());let s='';for(const b of bytes)s+=String.fromCharCode(b);out.append('known_speaker_names[]',p.id);out.append('known_speaker_references[]','data:'+p.reference_type+';base64,'+btoa(s));}}}
 else{out.set('response_format','json');out.append('languages[]',direction==='en-zh'?'en':'zh-tw');out.set('prompt','Tamkang University Zen Club / 淡江禪學社。主題：'+session.topic.slice(0,250)+'。'+(person?.notes.slice(0,400)??''));}
 const result=await fetch('https://api.openai.com/v1/audio/transcriptions',{method:'POST',headers:{Authorization:'Bearer '+apiKey},body:out,signal:AbortSignal.timeout(55000)});
 if(!result.ok)throw new Error(result.status===429?'語音服務額度不足或忙碌，請稍後重試。':'語音辨識連線失敗，請檢查服務設定。');
 const body=await result.json() as {text?:string;segments?:{text:string;speaker:string;start:number}[]};
 const parts=auto?(body.segments??[]):[{text:body.text??'',speaker:personId,start:0}];
 for(const part of parts){if(!part.text.trim())continue;const detected=auto?known.find(p=>p.id===part.speaker)??null:person;const currentRole=auto?(JSON.parse(session.roles)[detected?.id??'']??detected?.role??'待確認'):role;let translated='',note='';try{translated=await translateText(part.text,session,detected,currentRole,direction);}catch(e){note=e instanceof Error?e.message:'翻譯尚未完成。';}
 const key=await saveSegment({sessionId:sid,personId:detected?.id??null,label:detected?.name??(direction==='en-zh'?'英文發言者':'待確認講者'),role:currentRole,direction,zh:direction==='zh-en'?part.text:translated,en:direction==='en-zh'?part.text:translated,note,source:auto?'automatic':'microphone',audioKey,offset:offset+(part.start??0)});saved.push(key);}
 return Response.json({ids:saved,provider:'openai'});
 }catch(e){return failure(e);}}
