/** One disposable preview in flight. Late responses cannot revive a stopped/paused line. */
export function createPreviewClient(request:(audio:Blob,signal:AbortSignal)=>Promise<string|null>,show:(text:string)=>void){
 let generation=0,active:AbortController|null=null,closed=false,disabled=false;
 const reset=()=>{generation++;active?.abort();active=null;show('');};
 return {reset,close(){closed=true;reset();},async send(audio:Blob){
  if(closed||disabled||active)return;
  const stamp=generation,controller=new AbortController();active=controller;
  try{const text=await request(audio,controller.signal);if(stamp!==generation||closed)return;if(text===null){disabled=true;show('');}else show(text);}
  catch{/* a missed preview must not stop final ASR or persist partial text */}
  finally{if(active===controller)active=null;}
 }};
}
