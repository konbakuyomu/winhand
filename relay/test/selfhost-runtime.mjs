// Explicit opt-in real-runtime integration. Uses synthetic secrets, isolated loopback port/state.
// Never bundled into the deployed worker. No production credentials or agents are touched.
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createHash, randomBytes } from 'node:crypto';
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import WebSocket from 'ws';
import http from 'node:http';
import { render } from '../selfhost/config.mjs';

const root = fileURLToPath(new URL('..', import.meta.url));
const binary = process.env.WORKERD_BIN;
assert.ok(binary && path.isAbsolute(binary), 'Set absolute WORKERD_BIN');
await fs.mkdir(path.join(root, 'dist'), { recursive: true });
const dir = await fs.mkdtemp(path.join(root, 'dist/runtime-integration-'));
const settings = { PUBLIC_ORIGIN: 'https://relay.example.com', OWNER_PASSPHRASE: randomBytes(32).toString('hex'), AGENT_TOKEN: randomBytes(32).toString('hex'), PROXY_TOKEN: randomBytes(32).toString('hex') };
for (const part of ['bridge','kv']) await fs.mkdir(path.join(dir, 'state',part), { recursive: true });
await fs.writeFile(path.join(dir,'config.capnp'), render(settings,path.join(dir,'state')), {mode:0o600});
await build({ stdin: { contents: `import relay from './selfhost/entry.ts'; export {Bridge} from './selfhost/entry.ts';
export default {async fetch(r,e,c) { if(new URL(r.url).pathname === '/__kv_test') { const p=await r.json(); return Response.json(await e.OAUTH_KV[p.op](...p.args) ?? null); } return relay.fetch(r,e,c); }};`, resolveDir:root, sourcefile:'integration-only.ts',loader:'ts'}, outfile:path.join(dir,'worker.mjs'), bundle:true,format:'esm',platform:'neutral',mainFields:['module','main'],target:'es2022',conditions:['workerd','worker'],external:['cloudflare:*','node:*'],logLevel:'warning' });
const base='http://127.0.0.1:18787';
const headers={'host':'relay.example.com','x-winhand-proxy-token':settings.PROXY_TOKEN,'x-winhand-client-ip':'192.0.2.9'};
const call=(url,options={})=>new Promise((resolve,reject)=>{
  const req=http.request(base+url,{method:options.method??'GET',headers:{...headers,...options.headers}},res=>{
    const chunks=[];res.on('data',b=>chunks.push(b));res.on('end',()=>{const h=new Headers();for(const [k,v] of Object.entries(res.headers)){for(const item of Array.isArray(v)?v:[v])if(item!==undefined)h.append(k,item);}resolve(new Response(Buffer.concat(chunks),{status:res.statusCode,headers:h}));});
  });req.setTimeout(15000,()=>req.destroy(new Error('isolated request timeout')));req.on('error',reject);if(options.body)req.write(options.body.toString());req.end();
});
const kv=async(op,...args)=>{ const r=await call('/__kv_test',{method:'POST',body:JSON.stringify({op,args})}); assert.equal(r.status,200);return r.json(); };
let child; let runtimeError='';
async function start(){
  child=spawn(binary,['serve',path.join(dir,'config.capnp'),'--experimental','--socket-addr=http=127.0.0.1:18787'],{stdio:['ignore','ignore','pipe'],windowsHide:true});
  child.stderr.on('data',b=>{runtimeError+=b.toString();});
  for(let i=0;i<60;i++){if(child.exitCode!==null)throw Error('workerd exited: '+runtimeError);try{if((await call('/status.json')).status===200)return;}catch{}await new Promise(r=>setTimeout(r,100));}
  throw Error('runtime readiness timeout');
}
async function stop(){if(child && child.exitCode===null){child.kill();await once(child,'exit');}}
let tokens; let client; let ws;
try{
  await start();
  assert.equal((await fetch(base+'/status.json')).status,403);
  assert.equal((await call('/mcp')).status,401);
  assert.equal((await call('/agent')).status,401);
  const meta=await (await call('/.well-known/oauth-authorization-server')).json();
  assert.equal(meta.issuer,settings.PUBLIC_ORIGIN);
  console.log('PASS ingress boundary and OAuth discovery');
  await kv('put','test:persist',JSON.stringify({ok:1}),{metadata:{fixture:true}});
  assert.deepEqual(await kv('get','test:persist',{type:'json'}),{ok:1});
  await kv('put','test:second','two');
  const page=await kv('list',{prefix:'test:',limit:1});
  assert.equal(page.keys.length,1);assert.equal(page.list_complete,false);assert.deepEqual(page.keys[0].metadata,{fixture:true});
  assert.equal((await kv('list',{prefix:'test:',limit:1,cursor:page.cursor})).keys[0].name,'test:second');
  await kv('delete','test:second');assert.equal(await kv('get','test:second'),null);
  await kv('put','test:ttl','short',{expirationTtl:60});const ttlStart=Date.now();
  console.log('PASS real KV JSON/metadata/pagination/delete');
  const registered=await call('/oauth/register',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({client_name:'isolated runtime test',redirect_uris:['https://client.example/callback'],token_endpoint_auth_method:'none',grant_types:['authorization_code','refresh_token'],response_types:['code']})});
  assert.equal(registered.status,201);client=await registered.json();
  const verifier=randomBytes(32).toString('base64url');
  const params=new URLSearchParams({client_id:client.client_id,redirect_uri:'https://client.example/callback',response_type:'code',scope:'winhand',state:'test-state',code_challenge:createHash('sha256').update(verifier).digest('base64url'),code_challenge_method:'S256',resource:settings.PUBLIC_ORIGIN+'/mcp'});
  const badRedirect=new URLSearchParams(params);badRedirect.set('redirect_uri','https://attacker.example/callback');
  assert.equal((await call('/authorize?'+badRedirect)).status,400);
  const consent=await call('/authorize?'+params);assert.equal(consent.status,200);
  const html=await consent.text();const handle=html.match(/name="handle" value="([^"]+)"/)[1];
  const cookie=consent.headers.getSetCookie().map(s=>s.split(';')[0]).join('; ');
  const form=new URLSearchParams({handle,client:'isolated runtime test',redirect:'https://client.example/callback',passphrase:settings.OWNER_PASSPHRASE,action:'approve'});
  const wrongPass=new URLSearchParams(form);wrongPass.set('passphrase','wrong');
  assert.equal((await call('/authorize',{method:'POST',headers:{'content-type':'application/x-www-form-urlencoded',cookie,origin:settings.PUBLIC_ORIGIN},body:wrongPass})).status,401);
  const approved=await call('/authorize',{method:'POST',headers:{'content-type':'application/x-www-form-urlencoded',cookie,origin:settings.PUBLIC_ORIGIN},body:form});assert.equal(approved.status,302);
  const code=new URL(approved.headers.get('location')).searchParams.get('code');assert.ok(code);
  const tokenForm=new URLSearchParams({grant_type:'authorization_code',client_id:client.client_id,redirect_uri:'https://client.example/callback',code,code_verifier:verifier,resource:settings.PUBLIC_ORIGIN+'/mcp'});
  const wrongVerifier=new URLSearchParams(tokenForm);wrongVerifier.set('code_verifier','x'.repeat(43));
  assert.equal((await call('/oauth/token',{method:'POST',headers:{'content-type':'application/x-www-form-urlencoded'},body:wrongVerifier})).status,400);
  const exchanged=await call('/oauth/token',{method:'POST',headers:{'content-type':'application/x-www-form-urlencoded'},body:tokenForm});assert.equal(exchanged.status,200);tokens=await exchanged.json();assert.ok(tokens.refresh_token);
  console.log('PASS real DCR, consent cookie and S256 token exchange');
  ws=new WebSocket(base.replace('http:','ws:')+'/agent',{headers:{...headers,authorization:'Bearer '+settings.AGENT_TOKEN}});
  ws.on('message',b=>{const m=JSON.parse(b);if(m.type==='welcome')ws.send(JSON.stringify({type:'hello',version:'integration-test',hostname:'synthetic',platform:'test',resume:1,pending:[]}));if(m.type==='req_end'){ws.send(JSON.stringify({type:'head',id:m.id,status:200,headers:[['content-type','text/event-stream']]}));ws.send(JSON.stringify({type:'body',id:m.id,data:Buffer.from('data: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n').toString('base64')}));ws.send(JSON.stringify({type:'end',id:m.id}));}});
  await once(ws,'open');
  const response=await call('/mcp',{method:'POST',headers:{authorization:'Bearer '+tokens.access_token,'content-type':'application/json'},body:JSON.stringify({jsonrpc:'2.0',id:1,method:'tools/list'})});assert.equal(response.status,200);assert.match(await response.text(),/"ok":true/);
  console.log('PASS authenticated MCP through actual WebSocket/SSE bridge');
  ws.close();await once(ws,'close');ws=null;
  await stop();
  // Copy only after process exit: includes the complete SQLite/WAL/blob tree.
  const restored=path.join(dir,'restored-state');
  await fs.cp(path.join(dir,'state'),restored,{recursive:true});
  await fs.writeFile(path.join(dir,'config.capnp'),render(settings,restored),{mode:0o600});
  await start();
  assert.deepEqual(await kv('get','test:persist',{type:'json'}),{ok:1});
  const refreshed=await call('/oauth/token',{method:'POST',headers:{'content-type':'application/x-www-form-urlencoded'},body:new URLSearchParams({grant_type:'refresh_token',client_id:client.client_id,refresh_token:tokens.refresh_token,resource:settings.PUBLIC_ORIGIN+'/mcp'})});assert.equal(refreshed.status,200);
  console.log('PASS full runtime restart and offline backup restore: KV and OAuth refresh persistence');
  const renewed=await refreshed.json();
  const revoked=await call('/oauth/token',{method:'POST',headers:{'content-type':'application/x-www-form-urlencoded'},body:new URLSearchParams({client_id:client.client_id,token:renewed.refresh_token,token_type_hint:'refresh_token'})});assert.equal(revoked.status,200);
  assert.equal((await call('/oauth/token',{method:'POST',headers:{'content-type':'application/x-www-form-urlencoded'},body:new URLSearchParams({grant_type:'refresh_token',client_id:client.client_id,refresh_token:renewed.refresh_token})})).status,400);
  console.log('PASS invalid redirect/passphrase/PKCE rejected; refresh revocation enforced');
  await new Promise(r=>setTimeout(r,Math.max(0,ttlStart+62000-Date.now())));
  assert.equal(await kv('get','test:ttl'),null);
  console.log('PASS real KV TTL expiration');
}finally{if(ws)ws.terminate();await stop();}
