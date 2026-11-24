#!/usr/bin/env bash
# Take the documentation screenshots against a throwaway local Home Assistant.
#
#   tools/docs_screenshots/run.sh [--keep-ha] [--fresh|--reuse] [shot-name ...]
#
# Sets up (or reuses) tools/docs_screenshots/.venv, starts a rates stub and Home Assistant on 127.0.0.1,
# runs the browser driver, and stops both again. With DRIVER=v3shots.mjs the finished configuration (the
# site, weeks of history) is kept between runs and reused while it was built today (--reuse: whenever it
# exists, --fresh: rebuild from nothing); the 2.x driver always starts from nothing. With shot names only
# those are written. Every phase's start and end is printed to stderr as "[timing] ...".
set -euo pipefail
T0=$SECONDS
PHASE=""
PHASE_AT=0
phase() {
  local now=$((SECONDS - T0))
  if [ -n "$PHASE" ]; then echo "[timing] end   $PHASE: $((now - PHASE_AT)) s" >&2; fi
  PHASE="${1:-}"
  PHASE_AT=$now
  if [ -n "$PHASE" ]; then echo "[timing] start $PHASE (t=${now} s)" >&2; fi
}

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
VENV="$HERE/.venv"
CONFIG="${DOCS_CONFIG:-$HERE/.config}"
LOGS="${DOCS_LOGS:-$HERE/.logs}"
HA_PORT="${HA_PORT:-8129}"
STUB_PORT="${STUB_PORT:-8130}"
KEEP_HA=0
MODE=auto
SHOTS=()
for arg in "$@"; do
  case "$arg" in
    --keep-ha) KEEP_HA=1 ;;
    --fresh) MODE=fresh ;;
    --reuse) MODE=reuse ;;
    -h|--help) sed -n '2,9p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) SHOTS+=("$arg") ;;
  esac
done

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1" >&2; exit 1; }; }
need node
need "${CHROMIUM:-chromium}"

phase "python environment"
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

phase "ports"
# 2. Nothing else may be using the ports.
for port in "$HA_PORT" "$STUB_PORT"; do
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    echo "port $port is already in use; stop whatever listens there first" >&2
    exit 1
  fi
done

phase "configuration directory"
# 3. The configuration directory: the demo integrations and the integration under test, linked in. A finished
#    one (the stamp the v3 driver writes once the site is set up and its history seeded) is kept when it was
#    built today (the demo house's weeks end at the day it was built), unless --fresh.
STAMP_FILE="$CONFIG/.docs_built"
REUSING=0
if [ "$MODE" != fresh ] && [ "${DRIVER:-shots.mjs}" = v3shots.mjs ] && [ -f "$STAMP_FILE" ]; then
  if [ "$MODE" = reuse ] || [ "$(cat "$STAMP_FILE")" = "$(date +%F)" ]; then REUSING=1; fi
fi
if [ "$REUSING" = 1 ]; then
  echo "== reusing the finished configuration in $CONFIG (--fresh rebuilds it)"
  rm -rf "$LOGS" "$CONFIG/custom_components"
else
  rm -rf "$CONFIG" "$LOGS"
fi
mkdir -p "$CONFIG/custom_components" "$LOGS"
ln -s "$REPO/custom_components/spotnav" "$CONFIG/custom_components/spotnav"
# The demo integrations keep their manifest as manifest.demo.json in the repository, so the repo
# holds exactly one manifest.json (HACS's checks require that); the copy gets the real name.
# The fake spa and sauna (tools/fake_devices/, core Balboa's and Saunum's shapes) come in the same way.
for demo in "$HERE"/demo_components/*/ "$REPO"/tools/fake_devices/balboa/ "$REPO"/tools/fake_devices/saunum/; do
  target="$CONFIG/custom_components/$(basename "$demo")"
  cp -r "${demo%/}" "$target"
  mv "$target/manifest.demo.json" "$target/manifest.json"
done
[ -f "$CONFIG/automations.yaml" ] || echo "[]" > "$CONFIG/automations.yaml"
cat > "$CONFIG/configuration.yaml" <<YAML
http:
  server_host: 127.0.0.1
  server_port: $HA_PORT
frontend:
config:
recorder:
history:
energy:
mobile_app:
automation: !include automations.yaml
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

phase "start Home Assistant"
# 4. The ECB rates stub, then Home Assistant pointed at it (prices come from the demo Nord Pool integration).
"$VENV/bin/python" "$HERE/rates_stub.py" "$STUB_PORT" >"$LOGS/rates_stub.log" 2>&1 &
PIDS+=($!)
DOCS_SEED_SESSIONS=1 DOCS_SEED_HOUSE=1 DOCS_FAST_CHECK=1 DOCS_PRICE_FIXTURE="$REPO/tests/fixtures/relay/day_SE4_2026-09-22_96.json" \
  RATES_STUB_URL="http://127.0.0.1:$STUB_PORT" "$VENV/bin/python" "$HERE/ha_launch.py" -c "$CONFIG" >"$LOGS/home-assistant.log" 2>&1 &
PIDS+=($!)

phase "driver"
# 5. The driver.
export HA_URL="http://127.0.0.1:$HA_PORT"
export REPO_ROOT="$REPO"
export DOCS_CONFIG="$CONFIG" HA_LOG="$LOGS/home-assistant.log"
if [ "$REUSING" = 1 ]; then export REUSE=1 HA_ONBOARDED=1; fi
START=$SECONDS
status=0
node "$HERE/driver/${DRIVER:-shots.mjs}" "${SHOTS[@]}" || status=$?
phase ""
echo "== finished in $((SECONDS - START)) s (exit $status); logs in $LOGS"
if [ "$KEEP_HA" = 1 ]; then
  echo "== Home Assistant stays up on $HA_URL until you press Ctrl-C"
  wait "${PIDS[1]}" || true
fi
exit "$status"
