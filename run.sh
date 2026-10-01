#!/bin/sh
set -eu

cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -z "${BOARD_PASSWORD_HASH:-}" ] && [ -z "${BOARD_PASSWORD_HASH_FILE:-}" ]; then
    BOARD_PASSWORD_HASH_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/session-board/password.hash"
    export BOARD_PASSWORD_HASH_FILE
fi

exec .venv/bin/gunicorn --workers 1 --threads 8 --timeout 30 \
    --bind "${BOARD_BIND:-127.0.0.1:8099}" 'session_board.app:create_app()'
