import {readFile} from 'node:fs/promises';
export async function assertTencentBuild(manifest='dist/server/vinext-externals.json'){
 let externals;try{externals=JSON.parse(await readFile(manifest,'utf8'));}catch{throw Error('Tencent build missing; build with DEPLOY_TARGET=zeabur first');}
 if(!Array.isArray(externals)||!['postgres','ws'].every(name=>externals.includes(name)))throw Error('Wrong build target; Tencent requires DEPLOY_TARGET=zeabur and Node database/WebSocket drivers');
}
