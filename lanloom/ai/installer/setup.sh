#!/bin/sh
# Lanloom local AI setup for this computer (Linux and macOS).
# Installs a local model server, lets it listen in your network and pairs it with Lanloom.
# Usage: sh setup.sh [--yes] [--model <id>] [-h]
set -eu

LANLOOM_URL="__LANLOOM_URL__"
PAIR_CODE="__PAIR_CODE__"
PORT="__AI_PORT__"
ASSUME_YES=0
MODEL_OVERRIDE=""

usage() {
    cat <<USAGE
Lanloom local AI setup
  --yes           answer every question with yes
  --model <id>    use this model instead of the recommended one
  -h              show this help
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --yes|-y) ASSUME_YES=1 ;;
        --model) shift; [ $# -gt 0 ] || { echo "--model needs a value" >&2; exit 1; }; MODEL_OVERRIDE="$1" ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
    esac
    shift
done

TMPDIR_L="$(mktemp -d "${TMPDIR:-/tmp}/lanloom-ai.XXXXXX")"
trap 'rm -rf "$TMPDIR_L"' EXIT INT TERM
BODY="$TMPDIR_L/body"
HDRS="$TMPDIR_L/hdrs"

say() { printf '%s\n' "$*"; }
fail() { code="$1"; shift; printf '%s\n' "$*" >&2; exit "$code"; }

# ask "question" default(y|n) -> returns 0 for yes
ask() {
    q="$1"; def="$2"
    [ "$ASSUME_YES" = 1 ] && return 0
    if (: </dev/tty) 2>/dev/null; then
        if [ "$def" = y ]; then hint="[Y/n]"; else hint="[y/N]"; fi
        printf '%s %s ' "$q" "$hint" >/dev/tty
        ans=""
        read -r ans </dev/tty || ans=""
        case "$ans" in
            [Yy]*) return 0 ;;
            [Nn]*) return 1 ;;
        esac
    fi
    [ "$def" = y ]
}

# json_str key < body  -> string value, json_num key < body -> number
json_str() { sed -n 's/.*"'"$1"'" *: *"\([^"]*\)".*/\1/p' | head -n 1; }
json_num() { sed -n 's/.*"'"$1"'" *: *\([0-9][0-9.]*\).*/\1/p' | head -n 1; }

# http METHOD URL [JSON]  -> prints the status code, body in $BODY
http() {
    m="$1"; u="$2"; d="${3:-}"
    : >"$BODY"
    if command -v curl >/dev/null 2>&1; then
        if [ "$m" = POST ]; then
            curl -sS -m 30 -o "$BODY" -w '%{http_code}' -X POST -H 'Content-Type: application/json' -H 'X-Lanloom: 1' -d "$d" "$u" 2>/dev/null || echo 000
        else
            curl -sS -m 10 -o "$BODY" -w '%{http_code}' "$u" 2>/dev/null || echo 000
        fi
    elif command -v wget >/dev/null 2>&1; then
        if [ "$m" = POST ]; then
            wget -q -S --content-on-error -T 30 -O "$BODY" --header='Content-Type: application/json' --header='X-Lanloom: 1' --post-data="$d" "$u" 2>"$HDRS" || true
        else
            wget -q -S --content-on-error -T 10 -O "$BODY" "$u" 2>"$HDRS" || true
        fi
        c="$(sed -n 's/^ *HTTP\/[0-9.]* \([0-9][0-9]*\).*/\1/p' "$HDRS" | tail -n 1)"
        echo "${c:-000}"
    else
        echo 000
    fi
}

OS_NAME="$(uname -s)"
case "$OS_NAME" in
    Linux) OS=linux ;;
    Darwin) OS=macos ;;
    *) fail 1 "This script supports Linux and macOS. On Windows use the PowerShell script." ;;
esac

# ---- 1. hardware
ARCH="$(uname -m)"
GPU=""
if [ "$OS" = linux ]; then
    MEMINFO="${LANLOOM_AI_MEMINFO:-/proc/meminfo}"
    RAM_GB="$(awk '/^MemTotal:/ { printf "%.1f", $2 / 1048576 }' "$MEMINFO" 2>/dev/null || true)"
    CORES="$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)"
    if command -v nvidia-smi >/dev/null 2>&1; then GPU=nvidia
    elif [ -e /dev/kfd ]; then GPU=amd; fi
else
    MEM_B="$(sysctl -n hw.memsize 2>/dev/null || echo 0)"
    RAM_GB="$(awk -v b="$MEM_B" 'BEGIN { printf "%.1f", b / 1073741824 }')"
    CORES="$(sysctl -n hw.ncpu 2>/dev/null || echo 1)"
    [ "$ARCH" = arm64 ] && GPU=apple
fi
[ -n "${RAM_GB:-}" ] || RAM_GB=0
say "This computer: $RAM_GB GB memory, $CORES cores, GPU: ${GPU:-none}, $ARCH ($OS)"

# ---- 2. hello
HELLO="{\"code\":\"$PAIR_CODE\",\"hw\":{\"ram_gb\":$RAM_GB,\"cores\":$CORES,\"gpu\":\"$GPU\",\"arch\":\"$ARCH\",\"os\":\"$OS\"}}"
CODE="$(http POST "$LANLOOM_URL/api/ai/pair/hello" "$HELLO")"
case "$CODE" in
    200) ;;
    403) fail 2 "The pairing code is wrong or expired. Create a new one in Lanloom: Settings > AI > Another computer." ;;
    429) fail 2 "Too many attempts. Wait a minute and run the script again." ;;
    000) fail 2 "Lanloom is not reachable at $LANLOOM_URL. Check that this computer is in the same network and that Lanloom is running." ;;
    *) fail 2 "Lanloom answered with an unexpected status ($CODE) at $LANLOOM_URL." ;;
esac
MODEL="$(json_str model <"$BODY")"
SPEED="$(json_str speed <"$BODY")"
DL_GB="$(json_num download_gb <"$BODY")"
[ -n "$MODEL" ] || fail 2 "Lanloom did not recommend a model."
if [ -n "$MODEL_OVERRIDE" ]; then MODEL="$MODEL_OVERRIDE"; fi
case "$MODEL" in
    *[!A-Za-z0-9._:/-]*|"") fail 1 "The model name contains characters that are not allowed." ;;
esac

if [ "$SPEED" = unsuitable ] && [ -z "$MODEL_OVERRIDE" ]; then
    fail 3 "This computer does not have enough memory to run a useful local model. Use a public AI service in Lanloom instead (Settings > AI)."
fi
if [ "$SPEED" = slow ]; then
    say "Warning: this computer is slow for this model. Answers can take a minute."
    ask "Continue anyway?" y || fail 3 "Stopped. Use a public AI service in Lanloom instead (Settings > AI)."
fi
say "Model: $MODEL (download about ${DL_GB:-?} GB)"

# ---- 3. model server
if ! command -v ollama >/dev/null 2>&1; then
    if [ "$OS" = linux ]; then
        ask "The local model server is not installed. Install it now (downloads from ollama.com)?" y || fail 4 "The local model server is required. Install it from https://ollama.com/download and run this script again."
        if command -v curl >/dev/null 2>&1; then
            curl -fsSL https://ollama.com/install.sh | sh
        else
            fail 4 "curl is needed to install the model server. Install curl or get the server from https://ollama.com/download."
        fi
    else
        if command -v brew >/dev/null 2>&1; then
            ask "The local model server is not installed. Install it with Homebrew?" y || fail 4 "The local model server is required. Install it from https://ollama.com/download and run this script again."
            brew install ollama
        else
            fail 4 "The local model server is not installed. Download it from https://ollama.com/download, install it and run this script again."
        fi
    fi
    command -v ollama >/dev/null 2>&1 || fail 4 "The model server could not be installed. Get it from https://ollama.com/download."
fi

# ---- 4. listen in the network
say "The model server has no password: every device in your network can use it. Only continue in a network you trust."
ask "Let the model server listen in your network on port $PORT?" y || fail 3 "Stopped. The model server stays reachable only from this computer, so Lanloom cannot use it."

SUDO=""
if [ "$(id -u)" != 0 ]; then SUDO="sudo"; fi
OLLAMA_BIN="$(command -v ollama)"

if [ "$OS" = linux ]; then
    if command -v systemctl >/dev/null 2>&1 && systemctl cat ollama.service >/dev/null 2>&1; then
        DROPIN_DIR="${LANLOOM_AI_SYSTEMD_DIR:-/etc/systemd/system}/ollama.service.d"
        $SUDO mkdir -p "$DROPIN_DIR"
        printf '[Service]\nEnvironment="OLLAMA_HOST=0.0.0.0:%s"\n' "$PORT" | $SUDO tee "$DROPIN_DIR/lanloom.conf" >/dev/null
        $SUDO systemctl daemon-reload
        $SUDO systemctl restart ollama
        say "The model server now listens on port $PORT and starts with this computer."
    else
        say "No systemd service found. Starting the model server now; it will not start again after a restart."
        OLLAMA_HOST="0.0.0.0:$PORT" nohup "$OLLAMA_BIN" serve >/dev/null 2>&1 &
    fi
else
    PLIST="$HOME/Library/LaunchAgents/org.lanloom.ai.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    cat >"$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>org.lanloom.ai</string>
    <key>ProgramArguments</key>
    <array><string>$OLLAMA_BIN</string><string>serve</string></array>
    <key>EnvironmentVariables</key>
    <dict><key>OLLAMA_HOST</key><string>0.0.0.0:$PORT</string></dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
</dict>
</plist>
PLIST
    say "Stopping a model server that is already running (the menu bar app), so it can restart with network access."
    pkill -x ollama >/dev/null 2>&1 || true
    pkill -x Ollama >/dev/null 2>&1 || true
    launchctl unload "$PLIST" >/dev/null 2>&1 || true
    launchctl load -w "$PLIST"
    say "The model server now listens on port $PORT and starts when you log in."
fi

# ---- 5. wait for the server
say "Waiting for the model server ..."
i=0
while :; do
    c="$(http GET "http://127.0.0.1:$PORT/api/version")"
    [ "$c" = 200 ] && break
    i=$((i + 1))
    [ "$i" -lt 60 ] || fail 5 "The model server did not start within 60 seconds."
    sleep 1
done

# ---- 6. model
if ollama list 2>/dev/null | awk 'NR > 1 { print $1 }' | grep -qx "$MODEL"; then
    say "Model $MODEL is already installed."
else
    say "Downloading $MODEL ..."
    ollama pull "$MODEL"
fi

# ---- 7. pair
c="$(http POST "$LANLOOM_URL/api/ai/pair" "{\"code\":\"$PAIR_CODE\",\"port\":$PORT,\"model\":\"$MODEL\"}")"
if [ "$c" != 200 ]; then
    err="$(json_str error <"$BODY")"
    fail 5 "Lanloom could not pair with this computer (${err:-status $c})."
fi
JOB="$(json_str id <"$BODY")"
[ -n "$JOB" ] || fail 5 "Lanloom did not return a pairing job."
say "Waiting for Lanloom to reach this computer ..."
n=0
while :; do
    c="$(http POST "$LANLOOM_URL/api/ai/pair/status" "{\"code\":\"$PAIR_CODE\",\"job\":\"$JOB\"}")"
    if [ "$c" = 200 ]; then
        st="$(json_str state <"$BODY")"
        case "$st" in
            done)
                ms="$(json_num ms <"$BODY")"
                secs="$(awk -v m="${ms:-0}" 'BEGIN { printf "%.1f", m / 1000 }')"
                say "Lanloom received an answer from the model in $secs s. Local AI is ready."
                exit 0 ;;
            error)
                err="$(json_str error <"$BODY")"
                if [ "$err" = ai.err.lan_unreachable ]; then
                    say "Lanloom could not reach this computer on port $PORT. A firewall is probably blocking it. Examples:"
                    say "  sudo ufw allow $PORT/tcp"
                    say "  sudo firewall-cmd --permanent --add-port=$PORT/tcp && sudo firewall-cmd --reload"
                    exit 5
                fi
                fail 5 "Pairing failed: ${err:-unknown}" ;;
        esac
    elif [ "$c" = 404 ]; then
        fail 5 "Lanloom does not know this pairing job any more. Run the script again."
    fi
    n=$((n + 1))
    [ "$n" -lt 160 ] || fail 5 "Pairing did not finish within 8 minutes."
    sleep 3
done
