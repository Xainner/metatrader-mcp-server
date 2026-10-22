import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Store } from '../src/store.mjs';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const config = {source_id:'test',enabled:true,allowed_group_ids:['123@g.us'],allowed_author_ids:[],capture_after:'2026-01-01T00:00:00Z'};
const message = {id:'false_123@g.us_ABC',chatId:'123@g.us',author:'456@lid',timestamp:Date.now()/1000,type:'chat',body:'COMPRA ORO\nEntrada: 2500\nSL: 2495\nTP1: 2510'};
test('filter before storage and reject own, stale, disabled and unauthorized messages',()=>{
  const store=new Store(':memory:','test');
  for (const msg of [{...message,chatId:'999@g.us'},{...message,chatId:'123@c.us'},{...message,fromMe:true},{...message,timestamp:1}]) assert.equal(store.receive(msg,config),false);
  assert.equal(store.receive(message,{...config,enabled:false}),false);
  assert.equal(store.receive(message,{...config,allowed_author_ids:['999@lid']}),false);
  assert.equal(store.count(),0);
});
test('dedup delivery, preserve edits and original text including A B A',()=>{
  const store=new Store(':memory:','test');
  assert.equal(store.receive(message,config),true);
  assert.equal(store.receive(message,config),false);
  assert.equal(store.receive({...message,body:'SL: 2496'},config,'edit'),true);
  assert.equal(store.receive({...message,body:'SL: 2496'},config,'edit'),false);
  assert.equal(store.receive(message,config,'edit'),true);
  assert.equal(store.count(),3);
  assert.equal(store.known(message.chatId,message.id).text,message.body);
  assert.equal(store.known(message.chatId,message.id).revision,3);
});
test('events survive reopening and revoked message is a separate revision',()=>{
  const home=mkdtempSync(join(tmpdir(),'whatsapp-test-'));
  const file=join(home,'signals.sqlite3');
  let store=new Store(file,'test');
  try {
    store.receive(message,config); store.db.close(); store=new Store(file,'test');
    assert.equal(store.receive(message,config),false);
    assert.equal(store.receive({...message,body:'',type:'revoked'},config,'revoke'),true);
    assert.equal(store.known(message.chatId,message.id).content_kind,'revoked');
    assert.equal(store.count(),2);
    assert.throws(()=>store.receive(message,{...config,source_id:'other'}),/identity changed/);
  } finally { store.db.close(); rmSync(home,{recursive:true}); }
});
test('WhatsApp channels require a separate exact allowlist and never grant private chat access',()=>{
  const store=new Store(':memory:','test');
  const channel={...message,chatId:'321@newsletter',id:'channel-msg'};
  assert.equal(store.receive(channel,config),false);
  const enabled={...config,allowed_newsletter_ids:['321@newsletter']};
  assert.equal(store.receive(channel,enabled),true);
  assert.equal(store.receive({...channel,chatId:'999@newsletter'},enabled),false);
  assert.equal(store.receive({...channel,chatId:'321@c.us'},enabled),false);
  assert.equal(store.count(),1);
});
