#!/usr/bin/env bash
# One command for a winhand relay on your own Linux server (Docker + Caddy, automatic HTTPS):
#
#   curl -fsSL https://raw.githubusercontent.com/konbakuyomu/winhand/main/relay/selfhost/install.sh | sudo bash -s -- mcp.example.com
#
# Point the domain's DNS at this server and open ports 80 and 443 first (Caddy gets the
# certificate). Running it again upgrades the relay to the newest code and keeps its state, secrets
# and sign-ins; if the new relay does not come up healthy, it goes back to the previous one.
# Options: --dir <path> (default /opt/winhand-relay), --ref <git ref> (default main),
# --source <local checkout> (use these files instead of downloading), --no-build (use the image
# winhand-relay:local that is already there), --cimd on|off (accept OAuth clients identified by a
# metadata document URL; a relay whose Claude sign-ins were made that way must keep it on; kept
# from the previous run when not given), --hook on|off (serve the SessionStart hooks of cloud
# sessions: on makes secrets/hook-token when it is missing, off removes it; the token is never
# printed, read it from that file; kept from the previous run when not given).
set -euo pipefail

REPO="konbakuyomu/winhand"
DIR="/opt/winhand-relay"
REF="main"
SOURCE=""
HOST=""
BUILD=1
CIMD=""
HOOK=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --source) SOURCE="$2"; shift 2 ;;
    --no-build) BUILD=""; shift ;;
    --cimd) CIMD="$2"; shift 2 ;;
    --hook) HOOK="$2"; shift 2 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) HOST="$1"; shift ;;
  esac
done

say() { printf 'winhand: %s\n' "$*"; }
die() { printf 'winhand: %s\n' "$*" >&2; exit 1; }

[ -n "$HOST" ] || die "give the relay's domain, e.g.: bash install.sh mcp.example.com"
printf '%s' "$HOST" | grep -Eq '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$' || die "not a domain name: $HOST"
case "$CIMD" in on|off|"") ;; *) die "--cimd takes on or off" ;; esac
case "$HOOK" in on|off|"") ;; *) die "--hook takes on or off" ;; esac
command -v docker >/dev/null || die "Docker is needed: https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 || die "the Docker Compose plugin is needed (docker compose)"
docker info >/dev/null 2>&1 || die "Docker is installed but not usable here: run as root or start the Docker service"

mkdir -p "$DIR"
cd "$DIR"

# The relay as this server answers it (Caddy on 443), whatever the DNS says.
status() {
  curl -fsS --max-time 5 --resolve "$HOST:443:127.0.0.1" "https://$HOST/status.json" 2>/dev/null \
    || curl -fsSk --max-time 5 --resolve "$HOST:443:127.0.0.1" "https://$HOST/status.json" 2>/dev/null
}
UPGRADE=""; ONLINE_BEFORE=""
if [ -f compose.yaml ]; then
  UPGRADE=1
  if status | grep -q '"online":true'; then ONLINE_BEFORE=1; fi
fi

# 1. The relay's code (only relay/ of the repository).
say "getting the relay code ($([ -n "$SOURCE" ] && echo "$SOURCE" || echo "$REPO@$REF")) ..."
rm -rf code.new && mkdir code.new
if [ -n "$SOURCE" ]; then
  cp -R "$SOURCE/relay/." code.new/
else
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$REF" | tar -xz -C code.new --strip-components=2 --wildcards '*/relay/*'
fi
[ -f code.new/selfhost/Dockerfile ] || die "the download has no relay/selfhost/Dockerfile"
rm -rf code.old && { [ ! -d code ] || mv code code.old; } && mv code.new code && rm -rf code.old

# 2. Secrets, made once and kept: the owner passphrase and the Caddy <-> relay token.
umask 077
mkdir -p secrets state/bridge state/kv generated caddy-data caddy-config
NEW_PASSPHRASE=""
if [ ! -s secrets/owner-passphrase ]; then
  NEW_PASSPHRASE="$(head -c 24 /dev/urandom | base64 | tr -d '/+=\n' | head -c 32)"
  printf '%s' "$NEW_PASSPHRASE" > secrets/owner-passphrase
fi
[ -s secrets/proxy-token ] || head -c 32 /dev/urandom | base64 | tr -d '/+=\n' | head -c 43 > secrets/proxy-token
# The hook token of the cloud sessions' SessionStart hooks: made on request, never printed.
NEW_HOOK=""
if [ "$HOOK" = on ] && [ ! -s secrets/hook-token ]; then
  head -c 32 /dev/urandom | base64 | tr -d '/+=\n' | head -c 43 > secrets/hook-token
  NEW_HOOK=1
fi
[ "$HOOK" != off ] || rm -f secrets/hook-token

# 3. Build while the running relay keeps serving. The image it runs stays as winhand-relay:previous.
PREVIOUS=""
if [ -n "$BUILD" ]; then
  if docker image inspect winhand-relay:local >/dev/null 2>&1; then docker tag winhand-relay:local winhand-relay:previous; PREVIOUS=1; fi
  say "building the relay image ..."
  docker build -q -t winhand-relay:local -f code/selfhost/Dockerfile code >/dev/null
fi

# 4. The new configuration; what ran so far is kept in backup-<time>/ for going back.
BACKUP=""
if [ -n "$UPGRADE" ]; then
  BACKUP="backup-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$BACKUP"
  for f in compose.yaml Caddyfile .env; do [ ! -f "$f" ] || cp -a "$f" "$BACKUP/"; done
  # the backup's own image: winhand-relay:local is the new build now
  if [ -n "$PREVIOUS" ] && [ -f "$BACKUP/.env" ] && grep -q '^WINHAND_IMAGE=winhand-relay:local$' "$BACKUP/.env"; then
    sed -i 's/^WINHAND_IMAGE=winhand-relay:local$/WINHAND_IMAGE=winhand-relay:previous/' "$BACKUP/.env"
  fi
fi
[ -n "$CIMD" ] || CIMD="$( { grep -E '^CIMD=' .env 2>/dev/null || true; } | tail -1 | cut -d= -f2)"
printf 'WINHAND_HOST=%s\nPROXY_TOKEN=%s\nWINHAND_IMAGE=winhand-relay:local\n' "$HOST" "$(cat secrets/proxy-token)" > .env
[ "$CIMD" != "on" ] || printf 'CIMD=on\n' >> .env
# A relay from the single-computer days keeps serving its computer at the old addresses.
[ ! -s secrets/agent-token ] || printf 'AGENT_TOKEN_FILE=/run/secrets/agent-token\n' >> .env
# The hooks of cloud sessions (winhand bridge init) are served only while secrets/hook-token exists.
[ ! -s secrets/hook-token ] || printf 'HOOK_TOKEN_FILE=/run/secrets/hook-token\n' >> .env
cp code/selfhost/compose.yaml code/selfhost/Caddyfile .

# 5. Start, and check: the relay answers, and a computer that was online is back (its reconnect
#    attempts back off to 30 s, so give it a few minutes).
healthy() {
  local i
  for i in $(seq 1 60); do status >/dev/null && break; sleep 2; done
  status >/dev/null || return 1
  [ -n "$ONLINE_BEFORE" ] || return 0
  for i in $(seq 1 120); do
    if status | grep -q '"online":true'; then return 0; fi
    sleep 2
  done
  return 1
}
say "starting the relay ..."
docker compose up -d --remove-orphans >/dev/null 2>&1 || true
if ! healthy; then
  if [ -n "$BACKUP" ]; then
    say "the new relay is not healthy: going back to the previous one (configuration in $DIR/$BACKUP)"
    cp -a "$BACKUP/." .
    docker compose up -d --remove-orphans >/dev/null 2>&1 || true
    if healthy; then
      # winhand-relay:local is the image that failed: point it back at the one that runs, so the
      # next run keeps that one as winhand-relay:previous
      if [ -n "$PREVIOUS" ]; then docker tag winhand-relay:previous winhand-relay:local; fi
      die "went back to the previous relay, which works; the new one did not (see: docker compose -f $DIR/compose.yaml logs)"
    fi
    die "went back to the previous relay, but it is not healthy either; see: docker compose -f $DIR/compose.yaml logs"
  fi
  die "the relay did not answer; see: docker compose -f $DIR/compose.yaml logs"
fi

say "the relay is running: https://$HOST/"
if [ -n "$NEW_PASSPHRASE" ]; then
  say "owner passphrase (shown only now; also in $DIR/secrets/owner-passphrase):"
  printf '\n    %s\n\n' "$NEW_PASSPHRASE"
fi
if [ -n "$NEW_HOOK" ]; then
  say "hook token made in $DIR/secrets/hook-token (not printed: read it there and put the same value in the cloud environment's Network secret, see docs/rules-and-guards.md)"
fi
say "next: open https://$HOST/ , pair a computer and run the command it gives in that computer's PowerShell."
