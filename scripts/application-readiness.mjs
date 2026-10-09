import {pathToFileURL} from 'node:url';
import {resolve} from 'node:path';
export async function waitForAppDatabase({baseUrl,timeoutMs=60000,intervalMs=1000,fetcher=fetch}={}){
 const deadline=Date.now()+timeoutMs;
 while(Date.now()<deadline){
  try{
   const response=await fetcher(baseUrl+'/api/dictionaries',{signal:AbortSignal.timeout(Math.max(1,Math.min(5000,deadline-Date.now())))});
   if(response.ok&&Array.isArray((await response.json()).sources))return true;
  }catch{}
  await new Promise(r=>setTimeout(r,Math.max(0,Math.min(intervalMs,deadline-Date.now()))));
 }
 return false;
}
if(process.argv[1]&&pathToFileURL(resolve(process.argv[1])).href===import.meta.url){
 const ok=await waitForAppDatabase({baseUrl:'http://127.0.0.1:'+(process.env.PORT||3000)});
 console.log(ok?'Application database endpoint ready':'Application database endpoint did not become ready');
 process.exit(ok?0:1);
}
