#!/usr/bin/env sh
# Lanloom installer: one command, safe to run again (a second run updates, it never deletes data).
#
#   curl -fsSL https://raw.githubusercontent.com/robeertm/lanloom/main/deploy/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/robeertm/lanloom/main/deploy/install.sh | sh -s -- --no-updater
#
# Options: --dir <path> (default ~/lanloom)  --port <n> (default 7380)  --no-updater  --no-update-check
set -eu

DIR="${HOME}/lanloom"
PORT=7380
UPDATER=1
UPDATE_CHECK=1
IMAGE="ghcr.io/robeertm/lanloom:latest"

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --no-updater) UPDATER=0; shift ;;
    --no-update-check) UPDATE_CHECK=0; shift ;;
    -h|--help) sed -n '2,9p' "$0" 2>/dev/null || true; exit 0 ;;
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
