// A rolling deployment can meet the old pod's import lock. Retry while it owns
// the lock so an interrupted old import cannot leave the new pod idle forever.
export async function bootstrapDictionaries(run,{signal,delayMs=5000}={}){
 while(!signal?.aborted){
  const result=await run();
  if(result.status!=='import-in-progress')return result;
  await new Promise(resolve=>{
   const finish=()=>{clearTimeout(timer);signal?.removeEventListener('abort',finish);resolve();};
   const timer=setTimeout(finish,delayMs);
   signal?.addEventListener('abort',finish,{once:true});
   if(signal?.aborted)finish();
  });
 }
 return {status:'stopped'};
}
