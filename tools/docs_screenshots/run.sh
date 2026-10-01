#!/usr/bin/env bash
# Take the documentation screenshots against a throwaway local Home Assistant.
#
#   tools/docs_screenshots/run.sh [--keep-ha] [shot-name ...]
#
# Sets up (or reuses) tools/docs_screenshots/.venv, starts a relay stub and Home Assistant on 127.0.0.1,
# runs the browser driver, and stops both again. The instance's configuration is rebuilt from nothing on
# every run, so the result does not depend on an earlier run. With shot names only those are written.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
VENV="$HERE/.venv"
CONFIG="$HERE/.config"
LOGS="$HERE/.logs"
HA_PORT=8129
STUB_PORT=8130
KEEP_HA=0
SHOTS=()
for arg in "$@"; do
  case "$arg" in
    --keep-ha) KEEP_HA=1 ;;
    -h|--help) sed -n '2,9p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) SHOTS+=("$arg") ;;
  esac
done

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1" >&2; exit 1; }; }
need node
need "${CHROMIUM:-chromium}"

# 1. The virtual environment: created once, repaired when the pinned requirements change.
PYTHON="${PYTHON:-python3}"
STAMP="$VENV/.requirements.sha"
WANT="$(sha256sum "$HERE/requirements.txt" | cut -d' ' -f1)"
if [ ! -x "$VENV/bin/python" ]; then
  echo "== creating the virtual environment"
  "$PYTHON" -m venv "$VENV"
fi
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$WANT" ]; then
  echo "== installing Home Assistant (first run only, needs network access)"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -r "$HERE/requirements.txt"
  echo "$WANT" > "$STAMP"
fi

# 2. Nothing else may be using the ports.
for port in "$HA_PORT" "$STUB_PORT"; do
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    echo "port $port is already in use; stop whatever listens there first" >&2
    exit 1
  fi
done

# 3. A fresh configuration directory: the demo integrations and the integration under test, linked in.
rm -rf "$CONFIG" "$LOGS"
mkdir -p "$CONFIG/custom_components" "$LOGS"
ln -s "$REPO/custom_components/spotnav" "$CONFIG/custom_components/spotnav"
# The demo integrations keep their manifest as manifest.demo.json in the repository, so the repo
# holds exactly one manifest.json (HACS's checks require that); the copy gets the real name.
for demo in "$HERE"/demo_components/*/; do
  target="$CONFIG/custom_components/$(basename "$demo")"
  cp -r "${demo%/}" "$target"
  mv "$target/manifest.demo.json" "$target/manifest.json"
done
cat > "$CONFIG/configuration.yaml" <<YAML
http:
  server_host: 127.0.0.1
  server_port: $HA_PORT
frontend:
config:
logger:
  default: warning
  logs:
    custom_components.spotnav: info
YAML

PIDS=()
cleanup() {
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  done
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] && wait "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

# 4. The relay stub, then Home Assistant pointed at it.
"$VENV/bin/python" "$HERE/relay_stub.py" "$STUB_PORT" >"$LOGS/relay_stub.log" 2>&1 &
PIDS+=($!)
RELAY_STUB_URL="http://127.0.0.1:$STUB_PORT" "$VENV/bin/python" "$HERE/ha_launch.py" -c "$CONFIG" >"$LOGS/home-assistant.log" 2>&1 &
PIDS+=($!)

# 5. The driver.
export HA_URL="http://127.0.0.1:$HA_PORT"
export REPO_ROOT="$REPO"
START=$SECONDS
status=0
node "$HERE/driver/shots.mjs" "${SHOTS[@]}" || status=$?
echo "== finished in $((SECONDS - START)) s (exit $status); logs in $LOGS"
if [ "$KEEP_HA" = 1 ]; then
  echo "== Home Assistant stays up on $HA_URL until you press Ctrl-C"
  wait "${PIDS[1]}" || true
fi
exit "$status"
