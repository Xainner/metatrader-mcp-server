#!/bin/sh
# Run interactively by the owner. Never pipe codes or passwords into this script.
set -eu
systemctl --user stop trading-telegram-user.service
trap 'systemctl --user start trading-telegram-user.service' EXIT
"$HOME/trading/telegram-user/.venv/bin/python" -m trading_telegram_user login
