import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

from .config import ConfigurationError, load_config
from .store import get_status, initialize, list_events


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Observe-only Telegram signal hub")
    parser.add_argument("--home", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("status")
    commands.add_parser("poll")
    commands.add_parser("mcp")
    commands.add_parser("discover-chats")
    access = commands.add_parser("check-chats")
    access.add_argument("--chat-id", type=int, action="append", required=True)
    events = commands.add_parser("events")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    try:
        config = load_config(args.home)
        if args.command == 'check-chats':
            from .telegram import ApiError, check_chat_access
            try:
                print(json.dumps(check_chat_access(config, args.chat_id), ensure_ascii=False, indent=2))
                return
            except ApiError as exc:
                raise ConfigurationError(exc.code) from None
        if args.command == "poll":
            from .telegram import poll
            poll(config.home)
            return
        if args.command == "mcp":
            from .mcp_server import serve
            serve(config)
            return
        if args.command == "discover-chats":
            from .telegram import ApiError, TelegramAPI, check_bot, poller_lock, ALLOWED_UPDATES
            if config.enabled:
                raise ConfigurationError("Disable the source and stop its poller before chat discovery")
            if "BOT_TOKEN_MISSING_OR_INVALID" in config.blockers():
                raise ConfigurationError("Configure the bot token locally first")
            initialize(config)
            try:
                with poller_lock(config.home / "data" / "poller.lock"):
                    api = TelegramAPI(config.token)
                    check_bot(config, api)
                    updates = api.call("getUpdates", {"timeout": 0, "limit": 100, "allowed_updates": ALLOWED_UPDATES})
                    chats = {}
                    for update in updates:
                        message = next((update[key] for key in ALLOWED_UPDATES if isinstance(update.get(key), dict)), {})
                        chat = message.get("chat", {})
                        if type(chat.get("id")) is int:
                            chats[chat["id"]] = {"id": chat["id"], "type": chat.get("type"), "title": chat.get("title")}
                    print(json.dumps({"chats": list(chats.values()), "offset_advanced": False}, ensure_ascii=False, indent=2))
                    return
            except ApiError as exc:
                raise ConfigurationError(exc.code) from None
        if args.command == "init":
            initialize(config)
        output = list_events(config, args.after, args.limit) if args.command == "events" else get_status(config)
        print(json.dumps(output, ensure_ascii=False, indent=2))
    except (ConfigurationError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
    except (OSError, sqlite3.Error):
        print("Local storage unavailable; no remote update was acknowledged by this command", file=sys.stderr)
        raise SystemExit(3) from None


if __name__ == "__main__":
    main()
