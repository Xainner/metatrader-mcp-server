import { DatabaseSync } from 'node:sqlite';
import { createHash } from 'node:crypto';

export const wid = value => typeof value === 'string' ? value : value?._serialized || '';

export function validateConfig(config) {
  if (!/^[a-zA-Z0-9_-]{1,80}$/.test(config.source_id) || typeof config.enabled !== 'boolean' ||
      !Array.isArray(config.allowed_group_ids) || !Array.isArray(config.allowed_author_ids) ||
      config.allowed_group_ids.some(id => typeof id !== 'string' || !/^\d+(?:-\d+)?@g\.us$/.test(id)) ||
      config.allowed_author_ids.some(id => typeof id !== 'string' || !/^\d+@(c\.us|lid)$/.test(id)) ||
      !Number.isFinite(Date.parse(config.capture_after))) throw Error('Invalid receiver configuration');
  return config;
}

export class Store {
  constructor(path, source) {
    this.source = source;
    this.db = new DatabaseSync(path);
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=10000;
      CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL,
        chat_id TEXT NOT NULL, message_id TEXT NOT NULL, event_type TEXT NOT NULL,
        revision INTEGER NOT NULL, emitted_at TEXT NOT NULL, received_at TEXT NOT NULL,
        reply_to_message_id TEXT, text TEXT NOT NULL, content_hash TEXT NOT NULL,
        content_kind TEXT NOT NULL, author_id TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS message_identity ON events(source_id,chat_id,message_id,sequence);`);
    this.db.prepare('INSERT OR IGNORE INTO metadata VALUES (?,?)').run('source_id', source);
    if (this.db.prepare('SELECT value FROM metadata WHERE key=?').get('source_id').value !== source)
      throw Error('Source identity changed');
  }

  receive(message, config, eventType = 'new') {
    validateConfig(config);
    const group = wid(message.chatId) || wid(message.from);
    const author = wid(message.author);
    if (config.source_id !== this.source) throw Error('Source identity changed');
    // Filter before persistence; no private chats, own messages, or unauthorized groups.
    if (!config.enabled || message.fromMe || !config.allowed_group_ids.includes(group) ||
        !group.endsWith('@g.us') || (config.allowed_author_ids.length && !config.allowed_author_ids.includes(author))) return false;
    const timestamp = Number(message.timestamp || message.t);
    if (!Number.isFinite(timestamp) || timestamp * 1000 < Date.parse(config.capture_after) || timestamp * 1000 > Date.now()+300000) return false;
    const id = wid(message.id);
    if (!id || id.length > 1000) return false;
    // Retain the original text; overlong/media messages are quarantined downstream.
    const text = String(message.body || message.caption || '');
    const kind = eventType === 'revoke' ? 'revoked' : message.type === 'chat' ? 'text' : 'media';
    const hash = createHash('sha256').update(JSON.stringify([text, kind, eventType])).digest('hex');
    const last = this.db.prepare('SELECT * FROM events WHERE source_id=? AND chat_id=? AND message_id=? ORDER BY sequence DESC LIMIT 1').get(this.source, group, id);
    // Suppress duplicate delivery, but preserve A -> B -> A edits as separate revisions.
    if (last?.content_hash === hash || (eventType === 'new' && last)) return false;
    this.db.prepare(`INSERT INTO events(source_id,chat_id,message_id,event_type,revision,emitted_at,received_at,
      reply_to_message_id,text,content_hash,content_kind,author_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`).run(
      this.source, group, id, eventType, (last?.revision || 0)+1,
      new Date(timestamp*1000).toISOString(), new Date().toISOString(),
      wid(message.quotedMsgId) || null, text, hash, kind, author);
    return true;
  }

  known(group, id) {
    return this.db.prepare('SELECT * FROM events WHERE source_id=? AND chat_id=? AND message_id=? ORDER BY sequence DESC LIMIT 1').get(this.source, group, id);
  }
  count() { return this.db.prepare('SELECT COUNT(*) AS n FROM events').get().n; }
}
