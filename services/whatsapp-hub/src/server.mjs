import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import { Store, validateConfig, wid } from './store.mjs';

process.umask(0o077);
const home = process.env.WHATSAPP_HOME || '/data';
fs.mkdirSync(path.join(home, 'data'), {recursive:true, mode:0o700});
const readConfig = () => validateConfig(JSON.parse(fs.readFileSync(path.join(home, 'config.json'), 'utf8')));
const initial = readConfig();
// Entrypoint holds a host filesystem flock for this runtime. A killed Chromium
// container can leave hostname/PID symlinks that block every subsequent start.
// Remove only these ephemeral symlinks; never cookies, tokens or profile data.
for (const name of ['SingletonLock','SingletonCookie','SingletonSocket']) {
  const lock=path.join(home,'browser',name);
  try { if (fs.lstatSync(lock).isSymbolicLink()) fs.unlinkSync(lock); else throw Error('Unexpected profile lock file'); }
  catch (error) { if (error.code!=='ENOENT') throw error; }
}
const store = new Store(path.join(home, 'data/signals.sqlite3'), initial.source_id);
let state = 'starting', qr = null, qrAt = 0, client = null, groups = [], channels = [], lastError = null, authenticated = false, mainReady = false;
const status = () => ({state:!authenticated && qr ? 'awaiting_qr' : state, source_id:initial.source_id, enabled:readConfig().enabled,
  authorized_groups:readConfig().allowed_group_ids.length, stored_events:store.count(),
  authorized_channels:(readConfig().allowed_newsletter_ids || []).length,
  qr_available:!!qr && Date.now()-qrAt < 45000, last_error:lastError,
  qr_version:qrAt, authenticated, receiver_ready:authenticated && mainReady,
  execution_authorized:false, as_of:new Date().toISOString()});
const fail = code => { lastError=code; console.error(code); };
setInterval(() => {
  try {
    const target=path.join(home,'data/status.json');
    fs.writeFileSync(target+'.tmp', JSON.stringify(status()), {mode:0o600});
    fs.renameSync(target+'.tmp',target);
  } catch { fail('RECEIVER_STORAGE_OR_CONFIG_ERROR'); process.exit(1); }
},5000);
const record = (msg, kind='new') => {
  try { store.receive(msg, readConfig(), kind); }
  catch { fail('RECEIVER_STORAGE_OR_CONFIG_ERROR'); process.exit(1); }
};
for (const name of ['SIGTERM','SIGINT']) process.on(name,async()=>{
  qr=null; state='stopping'; setTimeout(()=>process.exit(0),5000).unref();
  await client?.close(); store.db.close(); process.exit(0);
});

// Only read endpoints. The Docker port is published exclusively on host loopback.
http.createServer(async (req,res) => {
  res.setHeader('Cache-Control','no-store');
  res.setHeader('X-Content-Type-Options','nosniff');
  res.setHeader('Referrer-Policy','no-referrer');
  res.setHeader('Content-Security-Policy',"default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'");
  if (req.method !== 'GET') { res.writeHead(405); return res.end(); }
  try {
    if (req.url === '/status') { res.setHeader('Content-Type','application/json'); return res.end(JSON.stringify(status())); }
    if (req.url === '/groups') { res.setHeader('Content-Type','application/json'); return res.end(JSON.stringify(groups)); }
    if (req.url === '/channels') { res.setHeader('Content-Type','application/json'); return res.end(JSON.stringify(channels)); }
    if (req.url.split('?')[0] === '/qr.png') {
      if (!qr || Date.now()-qrAt > 45000) { res.writeHead(404); return res.end(); }
      res.setHeader('Content-Type','image/png'); return res.end(qr);
    }
    if (req.url !== '/') { res.writeHead(404); return res.end(); }
    res.setHeader('Content-Type','text/html; charset=utf-8');
    res.end(`<!doctype html><html lang="es"><meta charset="utf-8"><title>Rinari · WhatsApp</title>
      <style>body{font:18px system-ui;max-width:760px;margin:40px auto;background:#101920;color:#edf8f4;padding:20px}img{width:456px;max-width:calc(100% - 48px);background:white;padding:24px;image-rendering:pixelated}pre{white-space:pre-wrap}h1{color:#80e8bc}</style>
      <h1>WhatsApp · Señales para Rinari</h1><p>En tu teléfono: WhatsApp → Dispositivos vinculados → Vincular dispositivo. Escanea el QR.</p>
      <img id="qr" alt="Esperando código QR"><pre id="state"></pre><h2>Grupos y canales disponibles</h2><p>Indica al agente la fuente exacta que autorizas. El receptor solo guarda mensajes de su lista permitida.</p><pre id="groups"></pre>
      <script>let version=0;async function refresh(){const s=await(await fetch('/status')).json();document.getElementById('state').textContent=JSON.stringify(s,null,2);const q=document.getElementById('qr');q.hidden=!s.qr_available;if(s.qr_available&&version!==s.qr_version){q.src='/qr.png?t='+s.qr_version;version=s.qr_version;}document.getElementById('groups').textContent=JSON.stringify({grupos:await(await fetch('/groups')).json(),canales:await(await fetch('/channels')).json()},null,2);}setInterval(refresh,2000);refresh();</script></html>`);
  } catch { res.writeHead(503); res.end('Receiver unavailable'); }
}).listen(3000,'0.0.0.0');

// Library logs can contain session/phone information. Never forward them.
const { defaultLogger, create } = await import('@wppconnect-team/wppconnect');
defaultLogger.transports.forEach(transport => { transport.silent = true; });
try {
  client = await create({session:'rinari-signals', deviceName:'Rinari Signal Receiver',
    headless:true, useChrome:false, logQR:false, autoClose:0, waitForLogin:true,
    disableWelcome:true, updatesLog:false, disableGoogleAnalytics:true,
    folderNameToken:path.join(home,'tokens'),
    puppeteerOptions:{executablePath:'/usr/bin/chromium', userDataDir:path.join(home,'browser'),
      args:['--no-sandbox','--disable-setuid-sandbox','--disable-dev-shm-usage','--disable-gpu']},
    catchQR(base64) { qr = Buffer.from(base64.replace(/^data:image\/png;base64,/,''),'base64'); qrAt=Date.now(); state='awaiting_qr'; },
    statusFind(value) { state=String(value).replace(/[^a-zA-Z0-9_-]/g,'').slice(0,80); }
  });
  authenticated=await client.isAuthenticated(); mainReady=await client.isMainReady();
  if (authenticated) qr=null;
  state=authenticated && mainReady ? 'connected' : 'awaiting_login';
  client.onMessage(msg => record(msg));
  client.onAnyMessage(msg => record(msg));
  client.onMessageEdit(data => record({...data.msg, id:data.id, chatId:wid(data.chat)}, 'edit'));
  client.onRevokedMessage(data => {
    const group = wid(data.from), id = String(data.refId || '');
    const prior = store.known(group,id);
    if (prior) record({chatId:group,id,author:prior.author_id,timestamp:Date.now()/1000,body:'',type:'revoked'},'revoke');
  });
  client.onStateChange(value => { state=String(value).replace(/[^a-zA-Z0-9_-]/g,'').slice(0,80); });
  const refreshGroups = async () => {
    try {
      groups=(await client.listChats({onlyGroups:true,count:1000})).map(g=>({id:wid(g.id), name:g.name || g.formattedTitle || '',
        community:!!g.groupMetadata?.isParentGroup, announcements:!!g.groupMetadata?.isDefaultSubgroup}));
      channels=(await client.listChats({onlyNewsletter:true,count:1000})).map(g=>({id:wid(g.id), name:g.name || g.formattedTitle || ''}));
    }
    catch { fail('GROUP_DISCOVERY_FAILED'); }
  };
  await refreshGroups(); setInterval(refreshGroups,60000);
  let missed=0;
  setInterval(async()=>{
    try {
      authenticated=await client.isAuthenticated(); mainReady=await client.isMainReady();
      if (authenticated && mainReady && await client.isConnected()) { missed=0; qr=null; state='connected'; }
      else if (authenticated) { state='disconnected'; if (++missed>=3) process.exit(1); }
      else state='awaiting_login';
    }
    catch { state='connection_unknown'; if (++missed>=3) process.exit(1); }
  },30000);
} catch { state='failed'; fail('WHATSAPP_START_FAILED'); setTimeout(()=>process.exit(1),30000); }
