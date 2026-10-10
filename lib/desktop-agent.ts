import {getEnv} from './env';
export async function desktopAgent(path:string,body?:unknown){
 const env=getEnv();if(!env.BREEZE_ASR_URL||!env.BREEZE_AGENT_TOKEN)throw Error('桌面代理尚未配對。');
 const url=new URL(env.BREEZE_ASR_URL);url.pathname=path;
 const r=await fetch(url,{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+env.BREEZE_AGENT_TOKEN,...(body===undefined?{}:{'Content-Type':'application/json'})},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(body===undefined?5000:65000),redirect:'error'});
 if(!r.ok)throw Error('桌面代理目前未就緒，請檢查主持 App 與模型連線。');return r.json();
}
export async function desktopTranslation(instructions:string,input:string,mode:'local'|'cloud'|'hybrid'){
 const result=await desktopAgent('/translate',{instructions,input,mode}) as {text?:unknown};
 if(typeof result.text!=='string'||!result.text.trim()||result.text.length>16000)throw Error('桌面翻譯回傳格式不正確。');return result.text;
}
