#!/usr/bin/env bash
# One command for a winhand relay on your own Linux server (Docker + Caddy, automatic HTTPS):
#
#   curl -fsSL https://raw.githubusercontent.com/konbakuyomu/winhand/main/relay/selfhost/install.sh | sudo bash -s -- mcp.example.com
#
# Point the domain's DNS at this server and open ports 80 and 443 first (Caddy gets the
# certificate). Running it again upgrades the relay to the newest code and keeps its state, secrets
# and sign-ins. Options: --dir <path> (default /opt/winhand-relay), --ref <git ref> (default main),
# --source <local checkout> (use these files instead of downloading), --no-build (use the image
# winhand-relay:local that is already there).
set -euo pipefail

REPO="konbakuyomu/winhand"
DIR="/opt/winhand-relay"
REF="main"
SOURCE=""
HOST=""
BUILD=1
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --source) SOURCE="$2"; shift 2 ;;
    --no-build) BUILD=""; shift ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) HOST="$1"; shift ;;
  esac
done

say() { printf 'winhand: %s\n' "$*"; }
die() { printf 'winhand: %s\n' "$*" >&2; exit 1; }

[ -n "$HOST" ] || die "give the relay's domain, e.g.: bash install.sh mcp.example.com"
printf '%s' "$HOST" | grep -Eq '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$' || die "not a domain name: $HOST"
command -v docker >/dev/null || die "Docker is needed: https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 || die "the Docker Compose plugin is needed (docker compose)"
docker info >/dev/null 2>&1 || die "Docker is installed but not usable here: run as root or start the Docker service"

mkdir -p "$DIR"
cd "$DIR"

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
printf 'WINHAND_HOST=%s\nPROXY_TOKEN=%s\nWINHAND_IMAGE=winhand-relay:local\n' "$HOST" "$(cat secrets/proxy-token)" > .env
cp code/selfhost/compose.yaml code/selfhost/Caddyfile .

# 3. Build and start (Caddy gets the certificate on the first request).
if [ -n "$BUILD" ]; then
  say "building the relay image ..."
  docker build -q -t winhand-relay:local -f code/selfhost/Dockerfile code >/dev/null
fi
docker compose up -d --remove-orphans >/dev/null
say "waiting for https://$HOST/ ..."
ok=""
for _ in $(seq 1 60); do
  if curl -fsS --max-time 5 --resolve "$HOST:443:127.0.0.1" "https://$HOST/status.json" >/dev/null 2>&1 \
    || curl -fsSk --max-time 5 --resolve "$HOST:443:127.0.0.1" "https://$HOST/status.json" >/dev/null 2>&1; then
    ok=1; break
  fi
  sleep 2
done
[ -n "$ok" ] || die "the relay did not answer; see: docker compose -f $DIR/compose.yaml logs"

say "the relay is running: https://$HOST/"
if [ -n "$NEW_PASSPHRASE" ]; then
  say "owner passphrase (shown only now; also in $DIR/secrets/owner-passphrase):"
  printf '\n    %s\n\n' "$NEW_PASSPHRASE"
fi
say "next: open https://$HOST/ , pair a computer and run the command it gives in that computer's PowerShell."
