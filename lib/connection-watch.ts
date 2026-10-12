/** Refresh readiness independently of session data; never queue overlapping probes. */
export function createConnectionWatcher<T extends {breeze:boolean}>(
 request:(signal:AbortSignal)=>Promise<T>,show:(connection:T)=>void,canPoll=()=>true,
){
 let closed=false,active:AbortController|null=null,timer:ReturnType<typeof setTimeout>|null=null;
 const schedule=(delay:number)=>{if(!closed)timer=setTimeout(()=>{timer=null;void refresh();},delay);};
 async function refresh(){
  if(closed||active)return;
  if(timer!==null){clearTimeout(timer);timer=null;}
  if(!canPoll()){schedule(15000);return;}
  const controller=new AbortController();active=controller;let delay=4000;
  try{const connection=await request(controller.signal);if(closed||controller.signal.aborted)return;
   show(connection);if(connection.breeze)delay=15000;
  }catch{/* A transient failure must not erase sessions or confirmed captions. */}
  finally{if(active===controller)active=null;if(!closed)schedule(delay);}
 }
 return {refresh,close(){closed=true;active?.abort();active=null;if(timer!==null)clearTimeout(timer);timer=null;}};
}
