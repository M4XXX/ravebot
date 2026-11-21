#!/bin/sh
# Container entrypoint: make the backup folder writable, then run the bot as user ravebot.
#
# Unraid and plain `docker run -v /host/path:/data/backups` create a missing host folder
# owned by root. The bot runs as uid 1000 and could not write its safety backups there,
# so restores, rebuilds and the one-time Notion import would be refused. When the
# container starts as root, this script gives the folder to ravebot and drops privileges.
# Started with --user (not root), it changes nothing and runs the bot directly.
set -eu

if [ "$(id -u)" = "0" ]; then
    dir="${BACKUP_DIR:-/data/backups}"
    mkdir -p "$dir"
    if ! chown ravebot:ravebot "$dir" 2>/dev/null; then
        echo "entrypoint: cannot change the owner of $dir; backups may fail" >&2
    fi
    chmod 700 "$dir" 2>/dev/null || true
    # setpriv keeps the environment: without this HOME stays /root, and libraries that
    # look into the home folder (asyncpg reads ~/.postgresql/postgresql.key) fail with
    # "Permission denied" instead of finding nothing.
    export HOME=/home/ravebot USER=ravebot LOGNAME=ravebot
    exec setpriv --reuid=ravebot --regid=ravebot --init-groups "$@"
fi

exec "$@"
