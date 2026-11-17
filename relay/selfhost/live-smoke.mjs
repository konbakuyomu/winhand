// Run only inside the authorized deployment container. No credentials or tool content are printed.
// Creates a temporary OAuth grant, performs read-only help(), then revokes the grant.
import fs from 'node:fs/promises';
import { randomBytes, createHash } from 'node:crypto';
import assert from 'node:assert/strict';
const origin=process.env.PUBLIC_ORIGIN;
const owner=(await fs.readFile(process.env.OWNER_PASSPHRASE_FILE,'utf8')).trim();
const call=(p,o={})=>fetch(origin+p,{...o,redirect:'manual'});
const formHeaders={'content-type':'application/x-www-form-urlencoded'};
let client,tokens;
try {
  const metadataURL='https://claude.ai/oauth/mcp-oauth-client-metadata';
  const m=await fetch(metadataURL,{redirect:'manual'});assert.equal(m.status,200,'CIMD upstream status');
  const metadata=await m.json();assert.ok(metadata.redirect_uris?.length,'CIMD redirects');
  const verifier=randomBytes(32).toString('base64url');
  const challenge=createHash('sha256').update(verifier).digest('base64url');
  const cimd=new URLSearchParams({client_id:metadataURL,redirect_uri:metadata.redirect_uris[0],response_type:'code',scope:'winhand',state:'cimd-smoke',code_challenge:challenge,code_challenge_method:'S256',resource:origin+'/mcp'});
  assert.equal((await call('/authorize?'+cimd)).status,200,'real runtime CIMD consent');
  console.log('PASS real public CIMD resolved by deployed OAuth runtime');
  const r=await call('/oauth/register',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({client_name:'WinHand deployment read-only smoke',redirect_uris:['https://client.example/callback'],token_endpoint_auth_method:'none',grant_types:['authorization_code','refresh_token'],response_types:['code']})});assert.equal(r.status,201);client=await r.json();
  const params=new URLSearchParams({client_id:client.client_id,redirect_uri:'https://client.example/callback',response_type:'code',scope:'winhand',state:'smoke',code_challenge:challenge,code_challenge_method:'S256',resource:origin+'/mcp'});
  const consent=await call('/authorize?'+params);assert.equal(consent.status,200);
  const handle=(await consent.text()).match(/name="handle" value="([^"]+)"/)[1];
  const cookie=consent.headers.getSetCookie().map(s=>s.split(';')[0]).join('; ');
  const approve=await call('/authorize',{method:'POST',headers:{...formHeaders,cookie,origin},body:new URLSearchParams({handle,client:'WinHand deployment read-only smoke',redirect:'https://client.example/callback',passphrase:owner,action:'approve'})});assert.equal(approve.status,302);
  const code=new URL(approve.headers.get('location')).searchParams.get('code');
  const exchanged=await call('/oauth/token',{method:'POST',headers:formHeaders,body:new URLSearchParams({grant_type:'authorization_code',client_id:client.client_id,redirect_uri:'https://client.example/callback',code,code_verifier:verifier,resource:origin+'/mcp'})});assert.equal(exchanged.status,200);tokens=await exchanged.json();
  let session;
  async function rpc(body){
    const headers={authorization:'Bearer '+tokens.access_token,'content-type':'application/json',accept:'application/json, text/event-stream','mcp-protocol-version':'2025-06-18'};
    if(session)headers['mcp-session-id']=session;
    const response=await call('/mcp',{method:'POST',headers,body:JSON.stringify(body),signal:AbortSignal.timeout(45000)});
    assert.ok(response.status===200 || response.status===202,'MCP response status '+response.status);
    session=response.headers.get('mcp-session-id')||session;
    const raw=await response.text();if(!raw)return null;
    if(response.headers.get('content-type')?.includes('text/event-stream'))return raw.split('\n').filter(l=>l.startsWith('data:')).map(l=>JSON.parse(l.slice(5))).find(v=>v.id===body.id);
    return JSON.parse(raw);
  }
  const init=await rpc({jsonrpc:'2.0',id:1,method:'initialize',params:{protocolVersion:'2025-06-18',capabilities:{},clientInfo:{name:'winhand-readonly-smoke',version:'1'}}});assert.ok(init.result,'MCP initialize');
  await rpc({jsonrpc:'2.0',method:'notifications/initialized'});
  const listed=await rpc({jsonrpc:'2.0',id:2,method:'tools/list'});assert.ok(listed.result?.tools?.some(t=>t.name==='help'),'help tool present');
  const help=await rpc({jsonrpc:'2.0',id:3,method:'tools/call',params:{name:'help',arguments:{}}});assert.ok(help.result && !help.result.isError,'read-only help result');
  console.log('PASS deployed OAuth -> live Windows agent -> MCP initialize/tools/list/help');
}finally{
  if(tokens?.refresh_token && client?.client_id){const r=await call('/oauth/token',{method:'POST',headers:formHeaders,body:new URLSearchParams({client_id:client.client_id,token:tokens.refresh_token,token_type_hint:'refresh_token'})});assert.equal(r.status,200,'smoke grant revocation');console.log('PASS temporary test grant revoked');}
}
