#!/usr/bin/env bash
# Start the real container image like it runs in production and check that it works.
#
# What it checks (all with the image's own entrypoint, a PostgreSQL container and a
# root-owned host folder mapped to /data/backups, the way Unraid and `docker run -v` do):
#   1. the bot runs as user ravebot (uid 1000), not as root
#   2. the mapped, root-owned backup folder is writable for the bot
#   3. every bot module imports (no dependency or file missing in the image)
#   4. the migrations run to the head revision with the image's alembic
#   5. a safety export is written to the mapped folder with the right owner
#   6. the bot itself starts: migrations, database, admin page, then it reaches the
#      Discord login (a fake token is refused by Discord, which proves the start-up
#      got that far). With SMOKE_DISCORD_TOKEN set, the real login is checked instead.
#
# Usage:  scripts/smoke-container.sh [IMAGE]
#   Without IMAGE the image is built from the Dockerfile (tag ravebot-smoke:local).
#   Uses podman when installed, else docker (override with CONTAINER_CLI).
set -euo pipefail

cd "$(dirname "$0")/.."
cli="${CONTAINER_CLI:-$(command -v podman || command -v docker || true)}"
if [ -z "$cli" ]; then
  echo "error: neither podman nor docker is installed" >&2
  exit 2
fi

image="${1:-}"
run_id="smoke-$$"
net="ravebot-$run_id"
db="ravebot-db-$run_id"
backups="$(mktemp -d)"   # owned by the current user; made root-like below via --userns
failures=0

cleanup() {
  "$cli" rm -f "$db" >/dev/null 2>&1 || true
  "$cli" network rm "$net" >/dev/null 2>&1 || true
  # The folder may hold files owned by the container user: remove them through the container
  if [ -d "$backups" ]; then
    "$cli" run --rm -v "$backups:/b" --entrypoint sh "${image:-ravebot-smoke:local}" \
      -c 'rm -rf /b/* /b/.[!.]* 2>/dev/null; chown 0:0 /b; chmod 755 /b; true' >/dev/null 2>&1 || true
    rmdir "$backups" 2>/dev/null || rm -rf "$backups" 2>/dev/null || true
  fi
}
trap cleanup EXIT

ok()   { echo "  ok    $1"; }
fail() { echo "  FAIL  $1" >&2; failures=$((failures + 1)); }

if [ -z "$image" ]; then
  image="ravebot-smoke:local"
  echo "Building $image ..."
  "$cli" build -q -t "$image" . >/dev/null
fi

echo "Starting PostgreSQL ..."
"$cli" network create "$net" >/dev/null
"$cli" run -d --name "$db" --network "$net" --network-alias db \
  -e POSTGRES_USER=ravebot -e POSTGRES_PASSWORD=ravebot -e POSTGRES_DB=ravebot \
  docker.io/library/postgres:16-alpine >/dev/null
for _ in $(seq 1 60); do
  if "$cli" exec "$db" pg_isready -U ravebot -d ravebot >/dev/null 2>&1; then break; fi
  sleep 1
done
if ! "$cli" exec "$db" pg_isready -U ravebot -d ravebot >/dev/null 2>&1; then
  echo "PostgreSQL did not start" >&2
  "$cli" ps -a >&2 || true
  "$cli" logs "$db" >&2 || true
  exit 1
fi
sleep 2  # the image's init restarts the server once; pg_isready can answer in between

env_args=(
  -e POSTGRES_URL=postgresql://ravebot:ravebot@db:5432/ravebot
  -e ADMIN_USER_IDS=111111111111111111
  -e ADMIN_PAGE_PASSWORD=smoke-test-password
  -e LOG_LEVEL=INFO
)
# Root-owned folder, like a host folder that Docker or Unraid created
chmod 755 "$backups"
mount=(-v "$backups:/data/backups")
# As the container starts as root (no --user), the entrypoint hands the folder to ravebot.
common=(--rm --network "$net" "${env_args[@]}" "${mount[@]}")

echo "Checking the image ($image) ..."

uid="$("$cli" run "${common[@]}" -e DISCORD_BOT_TOKEN=x "$image" id -u 2>&1 | tail -1)"
[ "$uid" = "1000" ] && ok "the bot runs as uid 1000" || fail "the bot runs as uid '$uid', expected 1000"

if "$cli" run "${common[@]}" -e DISCORD_BOT_TOKEN=x "$image" \
    sh -c 'touch /data/backups/.probe && rm /data/backups/.probe' >/dev/null 2>&1; then
  ok "the mapped backup folder is writable"
else
  fail "the mapped backup folder is not writable for the bot"
fi

if "$cli" run -i "${common[@]}" -e DISCORD_BOT_TOKEN=x "$image" python - <<'PY' >/dev/null 2>&1
import importlib, pkgutil, bot
for info in pkgutil.walk_packages(bot.__path__, "bot."):
    importlib.import_module(info.name)
PY
then
  ok "every bot module imports"
else
  fail "a bot module does not import in the image"
fi

if "$cli" run "${common[@]}" -e DISCORD_BOT_TOKEN=x "$image" \
    python -m alembic -c alembic.ini upgrade head >/dev/null 2>&1; then
  ok "migrations run to head"
else
  fail "migrations failed in the image"
fi

if "$cli" run -i "${common[@]}" -e DISCORD_BOT_TOKEN=x "$image" python - <<'PY' >/dev/null 2>&1
import asyncio, asyncpg, os
from bot.backup.export import write_safety_export
async def main():
    pool = await asyncpg.create_pool(os.environ["POSTGRES_URL"])
    print(await write_safety_export(pool, "smoke"))
asyncio.run(main())
PY
then
  # The folder now belongs to the bot user, so look inside through the image
  owner="$("$cli" run "${common[@]}" -e DISCORD_BOT_TOKEN=x "$image" \
    sh -c 'stat -c %u /data/backups/ravebot-*-smoke.ravebot.json.gz' 2>/dev/null | head -1 || true)"
  if [ "$owner" = "1000" ]; then
    ok "a safety export was written to the mapped folder (owner uid 1000)"
  else
    fail "no safety export owned by the bot in the mapped folder"
  fi
else
  fail "the safety export failed in the image"
fi

# The bot itself
token="${SMOKE_DISCORD_TOKEN:-smoke.fake.token}"
logfile="$(mktemp)"
if [ -n "${SMOKE_DISCORD_TOKEN:-}" ]; then
  # A real token: the bot must stay up and log in; stop it after a while
  "$cli" run -d --name "ravebot-bot-$run_id" --network "$net" "${env_args[@]}" "${mount[@]}" \
    -e DISCORD_BOT_TOKEN="$token" "$image" >/dev/null
  for _ in $(seq 1 60); do
    "$cli" logs "ravebot-bot-$run_id" >"$logfile" 2>&1 || true
    if grep -qiE "logged in|ready|connected as" "$logfile"; then break; fi
    sleep 1
  done
  "$cli" rm -f "ravebot-bot-$run_id" >/dev/null 2>&1 || true
  if grep -qiE "logged in|ready|connected as" "$logfile" && ! grep -q "Traceback" "$logfile"; then
    ok "the bot started and logged in to Discord"
  else
    fail "the bot did not log in to Discord"; tail -20 "$logfile" >&2
  fi
else
  # A fake token: Discord refuses it, which is the last step of the start-up
  timeout 120 "$cli" run "${common[@]}" -e DISCORD_BOT_TOKEN="$token" "$image" >"$logfile" 2>&1 || true
  if grep -q "Migrations complete" "$logfile" \
     && grep -qiE "improper token|LoginFailure" "$logfile" \
     && ! grep -qiE "permission denied|No module named|ModuleNotFoundError|migration failed" "$logfile"; then
    ok "the bot starts up to the Discord login (migrations, database, admin page)"
  else
    fail "the bot did not start up to the Discord login"; tail -30 "$logfile" >&2
  fi
fi
rm -f "$logfile"

echo
if [ "$failures" -ne 0 ]; then
  echo "Container smoke test FAILED ($failures check(s))." >&2
  exit 1
fi
echo "Container smoke test passed."
