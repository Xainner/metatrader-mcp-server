"""Receiving and access checks only. No send or membership-changing methods."""

from contextlib import contextmanager
import json
import logging
from pathlib import Path
import signal
import threading
import urllib.error
import urllib.request

from .config import ConfigurationError, load_config
from .store import bind_bot, ingest_updates, initialize, metadata, set_metadata

ALLOWED_UPDATES = ["message", "edited_message", "channel_post", "edited_channel_post"]


class ApiError(RuntimeError):
    def __init__(self, code: str, retry_after: int = 5):
        super().__init__(code)  # Never retain the token-bearing URL or response text.
        self.code = code
        self.retry_after = min(max(retry_after, 1), 300)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ApiError("TELEGRAM_REDIRECT_REFUSED")


class TelegramAPI:
    def __init__(self, token: str):
        self._token = token
        # Ignore ambient proxies: no bot credentials through an unreviewed proxy.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, method: str, parameters: dict | None = None, timeout: int = 15):
        if method not in {"getMe", "getWebhookInfo", "getUpdates", "getChat", "getChatMember"}:
            raise ApiError("METHOD_NOT_ALLOWED")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self._token}/{method}",
            data=json.dumps(parameters or {}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                content = response.read(8_000_001)
            if len(content) > 8_000_000:
                raise ApiError("TELEGRAM_RESPONSE_TOO_LARGE")
            payload = json.loads(content)
        except urllib.error.HTTPError as exc:
            retry = 5
            if exc.code == 429:
                try:
                    retry = int(json.loads(exc.read(65536)).get("parameters", {}).get("retry_after", 5))
                except (ValueError, TypeError, AttributeError):
                    pass
            raise ApiError(f"TELEGRAM_HTTP_{exc.code}", retry) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise ApiError("TELEGRAM_TRANSPORT_ERROR") from None
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise ApiError("TELEGRAM_API_ERROR")
        return payload.get("result")


def check_bot(config, api):
    webhook = api.call("getWebhookInfo")
    if not isinstance(webhook, dict) or webhook.get("url"):
        raise ApiError("EXISTING_WEBHOOK_REFUSED")
    bot = api.call("getMe")
    if not isinstance(bot, dict) or type(bot.get("id")) is not int or bot.get("is_bot") is not True:
        raise ApiError("INVALID_BOT_IDENTITY")
    bind_bot(config, bot["id"])


def check_chat_access(config, chat_ids, api=None):
    """Read bot membership/visibility without polling, joining, or sending."""
    if not chat_ids or any(type(chat) is not int or chat == 0 for chat in chat_ids):
        raise ConfigurationError('Provide explicit nonzero Bot API chat IDs')
    api = api or TelegramAPI(config.token)
    bot = api.call('getMe')
    if not isinstance(bot, dict) or bot.get('is_bot') is not True or type(bot.get('id')) is not int:
        raise ApiError('INVALID_BOT_IDENTITY')
    items = []
    for chat_id in sorted(set(chat_ids)):
        item = {'chat_id': chat_id, 'access_verified': False, 'can_receive_messages': False}
        try:
            chat = api.call('getChat', {'chat_id': chat_id})
            if not isinstance(chat, dict) or chat.get('id') != chat_id:
                raise ApiError('CHAT_IDENTITY_MISMATCH')
            item.update(title=chat.get('title'), type=chat.get('type'))
            member = api.call('getChatMember', {'chat_id': chat_id, 'user_id': bot['id']})
            role = member.get('status') if isinstance(member, dict) else None
            joined = role in ('creator', 'administrator', 'member') or (role == 'restricted' and member.get('is_member') is True)
            visible = joined and (chat.get('type') == 'channel' or role in ('creator','administrator') or bot.get('can_read_all_group_messages') is True)
            item.update(access_verified=True, bot_role=role, bot_member=joined, can_receive_messages=visible,
                        blocker=None if visible else ('BOT_NOT_MEMBER' if not joined else 'GROUP_PRIVACY_MODE'))
        except ApiError as exc:
            item['blocker'] = exc.code
        items.append(item)
    return {'bot_username': bot.get('username'), 'chats': items, 'messages_sent': 0,
            'updates_consumed': False, 'execution_authorized': False}


@contextmanager
def poller_lock(path: Path):
    """OS-owned lock released on process death. Linux deployment only."""
    import fcntl
    with path.open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigurationError("Another poller owns this source home") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def poll(home: Path | None = None):
    config = load_config(home)
    initialize(config)
    stop = threading.Event()
    for name in (signal.SIGTERM, signal.SIGINT):
        signal.signal(name, lambda *_: stop.set())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("signal-hub")
    with poller_lock(config.home / "data" / "poller.lock"):
        api = None
        try:
            while not stop.is_set():
                if api is None:
                    config = load_config(home)
                    initialize(config)
                    blockers = config.blockers()
                    if blockers:
                        set_metadata(config, receiver_state="waiting_configuration", last_error=",".join(blockers))
                        stop.wait(5)
                        continue
                    api = TelegramAPI(config.token)
                    try:
                        check_bot(config, api)
                    except ApiError as exc:
                        set_metadata(config, receiver_state="blocked", last_error=exc.code)
                        logger.warning("Bot check blocked: %s", exc.code)
                        api = None
                        stop.wait(max(30, exc.retry_after))
                        continue
                try:
                    next_offset = int(metadata(config).get("next_offset", "0"))
                    updates = api.call("getUpdates", {"offset": next_offset, "timeout": config.poll_timeout_seconds,
                                                       "limit": 100, "allowed_updates": ALLOWED_UPDATES},
                                       timeout=config.poll_timeout_seconds + 10)
                    counts = ingest_updates(config, updates)
                    set_metadata(config, receiver_state="polling")
                    if counts["inserted"]:
                        logger.info("Events stored: %d", counts["inserted"])
                except ApiError as exc:
                    set_metadata(config, receiver_state="backoff", last_error=exc.code)
                    logger.warning("Receiver backoff: %s", exc.code)
                    if exc.code in {"TELEGRAM_HTTP_401", "TELEGRAM_HTTP_403", "TELEGRAM_HTTP_409"}:
                        # Do not fight another webhook/poller or loop on revoked credentials.
                        set_metadata(config, receiver_state="blocked")
                        raise ConfigurationError(exc.code) from None
                    stop.wait(exc.retry_after)
        finally:
            set_metadata(config, receiver_state="stopped")
