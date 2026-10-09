import assert from 'node:assert/strict';
import {waitForAppDatabase} from '../scripts/application-readiness.mjs';
let calls=0;
assert.equal(await waitForAppDatabase({baseUrl:'http://localhost',timeoutMs:100,intervalMs:1,fetcher:async()=>{calls++;if(calls===1)throw Error('warming');return {ok:calls>2,json:async()=>({sources:[]})};}}),true);
assert.equal(calls,3);
assert.equal(await waitForAppDatabase({baseUrl:'http://localhost',timeoutMs:8,intervalMs:1,fetcher:async()=>({ok:true,json:async()=>({error:'not data'})})}),false);
assert.equal(await waitForAppDatabase({baseUrl:'http://localhost',timeoutMs:8,intervalMs:1,fetcher:async()=>({ok:false})}),false);
console.log('PASS: database readiness allows bounded startup warmup and rejects persistent failures or invalid bodies');
