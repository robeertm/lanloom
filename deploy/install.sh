#!/usr/bin/env sh
# Lanloom installer: one command, safe to run again (a second run updates, it never deletes data).
#
#   curl -fsSL https://raw.githubusercontent.com/robeertm/lanloom/main/deploy/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/robeertm/lanloom/main/deploy/install.sh | sh -s -- --no-updater
#
# Options: --dir <path> (default ~/lanloom)  --port <n> (default 7380)  --no-updater  --no-update-check
#          --with-ai  also run a local AI service next to Lanloom (downloads several GB later, when you pick a model)
set -eu

DIR="${HOME}/lanloom"
PORT=7380
UPDATER=1
UPDATE_CHECK=1
WITH_AI=0
IMAGE="ghcr.io/robeertm/lanloom:latest"

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --no-updater) UPDATER=0; shift ;;
    --no-update-check) UPDATE_CHECK=0; shift ;;
    --with-ai) WITH_AI=1; shift ;;
    -h|--help) sed -n '2,10p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "a container runtime with the 'docker' command is required (see docs/INSTALL.md)"
docker compose version >/dev/null 2>&1 || die "'docker compose' (v2) is required"
docker info >/dev/null 2>&1 || die "the container runtime is not running or you lack permission (try: sudo sh, or add your user to the docker group)"
case "$PORT" in ''|*[!0-9]*) die "--port must be a number" ;; esac

mkdir -p "$DIR"
cd "$DIR"

if [ -f compose.yaml ]; then
  say "Existing installation in $DIR - updating (your data and settings stay)."
else
  say "Installing Lanloom into $DIR"
  cat > compose.yaml <<EOF
# Lanloom - created by the installer. Safe to edit; the installer never overwrites this file.
name: lanloom
services:
  lanloom:
    image: ${IMAGE}
    container_name: lanloom
    restart: unless-stopped
    network_mode: host
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    environment:
      LANLOOM_PORT: "${PORT}"
      LANLOOM_UPDATE_CHECK: "${UPDATE_CHECK}"
    volumes:
      - lanloom-data:/data
    labels:
      org.lanloom.role: app
  updater:
    image: ${IMAGE}
    container_name: lanloom-updater
    profiles: ["updater"]
    restart: unless-stopped
    command: ["python", "-m", "updater"]
    user: "0:0"
    network_mode: host
    read_only: true
    tmpfs: [/tmp]
    security_opt: ["no-new-privileges:true"]
    environment:
      LANLOOM_TARGET: lanloom
    volumes:
      - lanloom-data:/data
      - /var/run/docker.sock:/var/run/docker.sock
volumes:
  lanloom-data:
EOF
fi

PROFILE=""
[ "$UPDATER" = 1 ] && PROFILE="--profile updater"
say "Pulling the image ..."
# shellcheck disable=SC2086
docker compose $PROFILE pull
# shellcheck disable=SC2086
docker compose $PROFILE up -d

say "Waiting for Lanloom to answer ..."
PORT_IN_FILE=$(sed -n 's/.*LANLOOM_PORT: "\([0-9]*\)".*/\1/p' compose.yaml | head -n 1)
[ -n "$PORT_IN_FILE" ] && PORT="$PORT_IN_FILE"
ok=0
i=0
while [ $i -lt 60 ]; do
  if docker exec lanloom python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:${PORT}/api/health', timeout=2).status == 200 else 1)" >/dev/null 2>&1; then
    ok=1; break
  fi
  state=$(docker inspect -f '{{.State.Status}} {{.RestartCount}}' lanloom 2>/dev/null || echo "missing 0")
  case "$state" in restarting*|exited*|dead*) break ;; esac
  i=$((i + 1)); sleep 1
done

if [ "$ok" != 1 ]; then
  echo "Lanloom did not come up. Last log lines:" >&2
  docker logs --tail 40 lanloom >&2 || true
  exit 1
fi

AI_NOTE="Local AI: add it later in Settings > AI (one click with the updater sidecar) or re-run with --with-ai."
if [ "$WITH_AI" = 1 ]; then
  if [ -f compose.override.yaml ]; then
    if grep -q 'lanloom-ai' compose.override.yaml; then
      say "compose.override.yaml already defines the local AI service - leaving it as it is."
    else
      echo "Note: compose.override.yaml exists without the local AI service; not touching it (see docs/integrations/ai.md)." >&2
    fi
  else
    say "Adding the local AI service ..."
    AI_TMP=$(mktemp "$DIR/.compose.override.XXXXXX") || die "cannot create a temporary file in $DIR"
    if docker exec lanloom python -m lanloom.ai.runtime compose > "$AI_TMP" && [ -s "$AI_TMP" ]; then
      mv "$AI_TMP" compose.override.yaml
    else
      rm -f "$AI_TMP"
      die "could not get the local AI service definition from the Lanloom image (see docs/integrations/ai.md)"
    fi
  fi
  if grep -q 'lanloom-ai' compose.override.yaml; then
    # shellcheck disable=SC2086
    docker compose $PROFILE up -d
    AI_NOTE="Local AI service: running - open Settings > AI in Lanloom to pick a model."
  fi
fi

HOSTIP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -n "$HOSTIP" ] || HOSTIP="<this-host>"
CODE=$(docker logs lanloom 2>&1 | sed -n 's/.*setup code \([A-Z0-9-]*\).*/\1/p' | tail -n 1)
say "Lanloom is running: http://${HOSTIP}:${PORT}"
if [ -n "$CODE" ]; then
  say "Setup code for the first account: ${CODE}"
else
  echo "Already set up - sign in with your account."
fi
[ "$UPDATER" = 1 ] && echo "One-click updates: enabled (sidecar lanloom-updater)." || echo "One-click updates: off (update with: cd $DIR && docker compose pull && docker compose up -d)."
echo "$AI_NOTE"
