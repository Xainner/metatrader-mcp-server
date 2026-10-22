import argparse
import asyncio
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import getpass
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3


def now():
    return datetime.now(timezone.utc).isoformat()


def load(home):
    config = json.loads((home/'config.json').read_text())
    if config.get('target') != 'FXTradingVision' or config.get('source_id') != 'telegram-fxtradingvision':
        raise ValueError('Source outside owner authorization')
    datetime.fromisoformat(config['capture_after'])
    return config


def credentials(home):
    values = {}
    for line in (home/'.env').read_text().splitlines():
        key, sep, value = line.partition('=')
        if sep and key.strip() in ('TELEGRAM_API_ID', 'TELEGRAM_API_HASH'):
            values[key.strip()] = value.strip().strip('\"\'')
    app_id = int(values['TELEGRAM_API_ID'])
    app_hash = values['TELEGRAM_API_HASH']
    if app_id <= 0 or len(app_hash) != 32 or any(c not in '0123456789abcdefABCDEF' for c in app_hash):
        raise ValueError('Invalid private API credentials; edit .env locally')
    return app_id, app_hash


def initialize(home, config):
    from trading_signal_hub.config import Config
    from trading_signal_hub.store import initialize as init
    init(Config(home,config['source_id'],True,frozenset(),5))


def save_message(home, config, message, chat_id):
    """Atomic revision + duplicate suppression; original publication age is retained."""
    if chat_id != config.get('peer_id'):
        raise ValueError('Unapproved chat')
    if message.date.timestamp() < datetime.fromisoformat(config['capture_after']).timestamp():
        return 0
    text = message.message or ''
    if not isinstance(text,str) or len(text) > 32768:
        raise ValueError('Message outside storage contract')
    kind = 'text' if text else 'media_or_service'
    reply_id = getattr(message,'reply_to_msg_id',None)
    with closing(sqlite3.connect(home/'data/signals.sqlite3',timeout=10)) as db, db:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT revision,text,content_kind,reply_to_message_id FROM events WHERE chat_id=? AND message_id=? ORDER BY sequence DESC LIMIT 1', (chat_id,message.id)).fetchone()
        if old and old[1:] == (text,kind,reply_id):
            return 0
        revision = old[0]+1 if old else 1
        update_id = db.execute('SELECT COALESCE(MAX(update_id),0)+1 FROM events').fetchone()[0]
        import hashlib
        digest = hashlib.sha256(json.dumps([text,kind,reply_id],ensure_ascii=False).encode()).hexdigest()
        db.execute('''INSERT INTO events(source_id,update_id,chat_id,message_id,event_type,revision,
                      emitted_at,received_at,reply_to_message_id,text,content_hash,content_kind)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (config['source_id'],update_id,chat_id,message.id,'edit' if old else 'new',revision,
                    message.date.isoformat(),now(),reply_id,text,digest,kind))
        return 1


def status(home, config, state, **extra):
    count = 0
    if (home/'data/signals.sqlite3').exists():
        with closing(sqlite3.connect(home/'data/signals.sqlite3')) as db:
            count = db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
    data = dict(source_id=config['source_id'],state=state,enabled=True,
                authenticated=state in ('connected','source_unavailable'),receiver_ready=state=='connected',
                authorized_channels=1 if config.get('peer_id') else 0,stored_events=count,
                poll_interval_seconds=5,history_limit=50,capture_after=config['capture_after'],
                execution_authorized=False,as_of=now(),**extra)
    temp = home/'data/status.tmp'
    temp.write_text(json.dumps(data,indent=2))
    os.chmod(temp,0o600)
    temp.replace(home/'data/status.json')


@contextmanager
def session_lock(home):
    import fcntl
    with (home/'data/session.lock').open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def client_for(home):
    from telethon import TelegramClient
    app_id, app_hash = credentials(home)
    client = TelegramClient(str(home/'data/account'),app_id,app_hash,receive_updates=False,
                            request_retries=1,connection_retries=2,timeout=10,flood_sleep_threshold=0)
    client.session.save_entities = False
    return client


async def identify(client, home):
    user = await client.get_me()
    with closing(sqlite3.connect(home/'data/signals.sqlite3')) as db, db:
        old = db.execute("SELECT value FROM metadata WHERE key='account_binding'").fetchone()
        if old and old[0] != str(user.id):
            raise ValueError('Account changed; operator review required')
        db.execute("INSERT OR IGNORE INTO metadata VALUES ('account_binding',?)",(str(user.id),))


async def authenticate(home, config):
    from telethon.errors import SessionPasswordNeededError
    from telethon.tl.functions.channels import GetParticipantRequest
    from telethon import utils
    client = client_for(home)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            phone = getpass.getpass('Telefono con prefijo internacional (oculto): ')
            sent = await client.send_code_request(phone)
            try:
                await client.sign_in(phone=phone,code=getpass.getpass('Codigo de Telegram (oculto): '),phone_code_hash=sent.phone_code_hash)
            except SessionPasswordNeededError:
                await client.sign_in(password=getpass.getpass('Contrasena 2FA (oculta): '))
        await identify(client,home)
        entity = await client.get_entity(config['target'])
        await client(GetParticipantRequest(entity,'me'))
        peer_id = utils.get_peer_id(entity)
        if config.get('peer_id') and config['peer_id'] != peer_id:
            raise ValueError('Source identity changed')
        config.update(peer_id=peer_id,channel_id=entity.id,access_hash=entity.access_hash)
        temp=home/'config.tmp'
        temp.write_text(json.dumps(config,indent=2))
        os.chmod(temp,0o600)
        temp.replace(home/'config.json')
        status(home,config,'linked')
        print('Cuenta vinculada y acceso a FXTradingVision verificado. No se envio ningun mensaje.')
    finally:
        await client.disconnect()


async def receive(home, config):
    from telethon.tl.types import InputPeerChannel
    from telethon.errors import FloodWaitError
    stop=asyncio.Event()
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGTERM,signal.SIGINT):
        loop.add_signal_handler(sig,stop.set)
    client=client_for(home)
    try:
        while not stop.is_set():
            delay=5
            try:
                if not client.is_connected(): await client.connect()
                if not await client.is_user_authorized():
                    status(home,config,'needs_login')
                elif not config.get('peer_id'):
                    status(home,config,'needs_source_link')
                else:
                    await identify(client,home)
                    peer=InputPeerChannel(config['channel_id'],config['access_hash'])
                    messages=await client.get_messages(peer,limit=50)
                    inserted=sum(save_message(home,config,msg,config['peer_id']) for msg in reversed(messages) if getattr(msg,'date',None))
                    status(home,config,'connected',last_poll_at=now(),messages_seen=len(messages),inserted_last_poll=inserted)
            except FloodWaitError as exc:
                delay=max(5,exc.seconds)
                status(home,config,'rate_limited',retry_after_seconds=delay)
            except Exception as exc:
                # Never expose Telegram error text, phone, hash or message contents.
                status(home,config,'unavailable',last_error_type=type(exc).__name__)
                delay=15
            # Heartbeat remains current even during a long FloodWait.
            remaining=delay
            while remaining>0 and not stop.is_set():
                step=min(remaining,10)
                try: await asyncio.wait_for(stop.wait(),step)
                except asyncio.TimeoutError: pass
                remaining-=step
                if remaining>0:
                    prior=json.loads((home/'data/status.json').read_text())
                    prior['as_of']=now()
                    temp=home/'data/status.tmp'
                    temp.write_text(json.dumps(prior))
                    temp.replace(home/'data/status.json')
    finally:
        await client.disconnect()
        status(home,config,'stopped')


def main():
    os.umask(0o077)
    logging.basicConfig(handlers=[logging.NullHandler()],force=True)
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['init','login','receiver','status'])
    parser.add_argument('--home',type=Path,default=Path('~/trading/telegram-user').expanduser())
    args=parser.parse_args()
    home=args.home.resolve()
    config=load(home)
    initialize(home,config)
    if args.command=='status':
        print((home/'data/status.json').read_text())
        return
    with session_lock(home):
        if args.command=='init': status(home,config,'needs_login')
        elif args.command=='login':
            try: asyncio.run(authenticate(home,config))
            except Exception as exc:
                print('Vinculacion incompleta: '+type(exc).__name__+'. Revisa acceso/credenciales; no pegues secretos en el chat.')
                raise SystemExit(1)
        else: asyncio.run(receive(home,config))


if __name__=='__main__': main()
