"""Independent durable Discord deliveries; never acknowledges an agent's digest."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler

SOURCES={'whatsapp-main':'WhatsApp · Alejandrofx27', 'telegram-fxtradingvision':'Telegram · FXTradingVision'}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def database(path,readonly=False):
    target=path.resolve().as_uri()+'?mode=ro' if readonly else path
    with closing(sqlite3.connect(target,timeout=10,uri=readonly)) as db:
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        with db: yield db


def webhook(home):
    value=''
    for line in (home/'.env').read_text().splitlines():
        if line.startswith('DISCORD_WEBHOOK_URL='): value=line.partition('=')[2].strip()
    if not re.fullmatch(r'https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+',value):
        raise ValueError('Invalid Discord webhook in private .env')
    return value+'?wait=true'


def payload(item):
    event=item['event']
    text=event['text'].replace('```','` ` `')
    if len(text)>3500: text=text[:3500]+'\n[Texto truncado; consultar original]'
    source=SOURCES[event['source_id']]
    fields=[{'name':'Estado','value':item['state'],'inline':True},
            {'name':'Revision','value':str(event['revision']),'inline':True},
            {'name':'Evento','value':event['event_type'],'inline':True}]
    if event['source_id']=='telegram-fxtradingvision':
        fields.append({'name':'Original','value':'https://t.me/FXTradingVision/'+str(event['message_id'])})
    return {'allowed_mentions':{'parse':[]},'embeds':[{
        'title':source+' · señal detectada', 'description':'```text\n'+(text or '[Contenido multimedia sin texto]')+'\n```',
        'fields':fields,'timestamp':event['emitted_at'],
        'footer':{'text':f"Evento {item['event_sequence']} · mensaje {event['message_id']} · contenido externo; no confirma operación"},
    }]}


def initialize(home, research_path):
    home.mkdir(mode=0o700,parents=True,exist_ok=True)
    (home/'data').mkdir(mode=0o700,exist_ok=True)
    with database(home/'data/deliveries.sqlite3') as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        db.execute('''CREATE TABLE IF NOT EXISTS deliveries(sequence INTEGER PRIMARY KEY,payload TEXT NOT NULL,
                      state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
                      retry_at REAL NOT NULL DEFAULT 0,message_id TEXT,error TEXT,updated_at TEXT NOT NULL)''')
        if not db.execute("SELECT 1 FROM metadata WHERE key='cursor'").fetchone():
            with database(research_path,readonly=True) as source:
                cursor=source.execute('SELECT COALESCE(MAX(event_sequence),0) FROM evaluations').fetchone()[0]
            db.executemany('INSERT INTO metadata VALUES (?,?)',[('cursor',str(cursor)),('started_at',utc_now()),('next_send_at','0')])


def enqueue(home,research_path):
    from .inbox import Inbox
    with database(home/'data/deliveries.sqlite3') as db:
        db.execute('BEGIN IMMEDIATE')
        cursor=int(db.execute("SELECT value FROM metadata WHERE key='cursor'").fetchone()[0])
        started=db.execute("SELECT value FROM metadata WHERE key='started_at'").fetchone()[0]
        with database(research_path,readonly=True) as source:
            rows=source.execute('SELECT * FROM evaluations WHERE event_sequence>? ORDER BY event_sequence LIMIT 100',(cursor,)).fetchall()
        for row in rows:
            item=Inbox.decode(row)
            event=item['event']
            if event['source_id'] in SOURCES and item['state']!='noise' and datetime.fromisoformat(event['received_at'])>=datetime.fromisoformat(started):
                db.execute('INSERT OR IGNORE INTO deliveries(sequence,payload,updated_at) VALUES (?,?,?)',
                           (item['event_sequence'],json.dumps(payload(item),ensure_ascii=False),utc_now()))
        if rows: db.execute("UPDATE metadata SET value=? WHERE key='cursor'",(str(rows[-1]['event_sequence']),))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None


def post(url,body):
    request=Request(url,data=body.encode(),headers={'Content-Type':'application/json','User-Agent':'Rinari-Signal-Relay/0.1'},method='POST')
    try:
        with build_opener(NoRedirect).open(request,timeout=10) as response:
            raw=response.read(65536)
            return response.status,dict(response.headers),json.loads(raw) if raw else {}
    except HTTPError as exc:
        try: body=json.loads(exc.read(65536))
        except (ValueError,OSError): body={}
        return exc.code,dict(exc.headers),body
    except (URLError,TimeoutError,OSError,ValueError):
        # A lost response can follow an accepted POST. Never resend it blindly.
        return 0,{},{}


def recover(home):
    with database(home/'data/deliveries.sqlite3') as db:
        db.execute("UPDATE deliveries SET state='uncertain',error='process_interrupted',updated_at=? WHERE state='sending'",(utc_now(),))


def deliver_one(home,url,sender=post):
    clock=time.time()
    with database(home/'data/deliveries.sqlite3') as db:
        db.execute('BEGIN IMMEDIATE')
        if float(db.execute("SELECT value FROM metadata WHERE key='next_send_at'").fetchone()[0])>clock: return False
        row=db.execute("SELECT * FROM deliveries WHERE state='pending' AND retry_at<=? ORDER BY sequence LIMIT 1",(clock,)).fetchone()
        if not row: return False
        db.execute("UPDATE deliveries SET state='sending',attempts=attempts+1,updated_at=? WHERE sequence=?",(utc_now(),row['sequence']))
    code,headers,body=sender(url,row['payload'])
    if not isinstance(body,dict): body={}
    headers={key.lower():value for key,value in headers.items()}
    state,error,message_id,retry_at='uncertain','response_unknown',None,0
    next_send=0
    if 200<=code<300 and body.get('id'):
        state,error,message_id='sent',None,str(body['id'])
        if headers.get('x-ratelimit-remaining')=='0':
            try: next_send=time.time()+max(0,float(headers.get('x-ratelimit-reset-after',1)))
            except (TypeError,ValueError): next_send=time.time()+5
    elif code==429:
        try: delay=max(1,float(body.get('retry_after',headers.get('retry-after',5))))
        except (TypeError,ValueError): delay=5
        state,error,retry_at='pending','rate_limited',time.time()+delay
        next_send=retry_at
    elif 300<=code<500:
        state,error='failed','http_'+str(code)
        if code in (401,403,404): next_send=time.time()+3600
    elif code>=500: error='http_'+str(code)+'_outcome_unknown'
    with database(home/'data/deliveries.sqlite3') as db:
        db.execute('UPDATE deliveries SET state=?,error=?,message_id=?,retry_at=?,updated_at=? WHERE sequence=?',
                   (state,error,message_id,retry_at,utc_now(),row['sequence']))
        db.execute("UPDATE metadata SET value=? WHERE key='next_send_at'",(str(next_send),))
    return True


def write_status(home,state='running'):
    with database(home/'data/deliveries.sqlite3') as db:
        counts=dict(db.execute('SELECT state,COUNT(*) FROM deliveries GROUP BY state'))
        latest=db.execute('SELECT MAX(updated_at) FROM deliveries').fetchone()[0]
    data={'state':state,'deliveries':counts,'last_delivery_change':latest,'sources':list(SOURCES),'as_of':utc_now()}
    temp=home/'data/status.tmp'
    temp.write_text(json.dumps(data,indent=2))
    temp.replace(home/'data/status.json')
    return data


def main():
    import argparse
    os.umask(0o077)
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['init','run','test','status'])
    parser.add_argument('--home',type=Path,default=Path('~/trading/discord-relay').expanduser())
    args=parser.parse_args()
    home=args.home.resolve()
    config=json.loads((home/'config.json').read_text())
    research_path=Path(config['research_database'])
    initialize(home,research_path)
    if args.command=='init':
        write_status(home,'initialized')
        return
    if args.command=='status':
        print((home/'data/status.json').read_text())
        return
    import fcntl
    with (home/'data/relay.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        url=webhook(home)
        recover(home)
        if args.command=='test':
            with database(home/'data/deliveries.sqlite3') as db:
                body={'content':'✅ Rinari: reenvío de señales Telegram FXTradingVision y WhatsApp Alejandrofx27 conectado. Mensaje de prueba; no es una señal ni una operación.','allowed_mentions':{'parse':[]}}
                db.execute('INSERT OR IGNORE INTO deliveries(sequence,payload,updated_at) VALUES (-1,?,?)',(json.dumps(body),utc_now()))
            deliver_one(home,url)
            print(json.dumps(write_status(home,'tested')))
            return
        stop=threading.Event()
        for sig in (signal.SIGINT,signal.SIGTERM): signal.signal(sig,lambda *_:stop.set())
        try:
            while not stop.is_set():
                try:
                    enqueue(home,research_path)
                    deliver_one(home,url)
                    write_status(home)
                except (OSError,sqlite3.Error,ValueError):
                    write_status(home,'storage_or_contract_error')
                stop.wait(1)
        finally: write_status(home,'stopped')


if __name__=='__main__': main()
