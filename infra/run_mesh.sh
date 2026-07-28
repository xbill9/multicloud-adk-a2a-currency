#!/usr/bin/env bash
# Bring the three cloud agents up locally, one per port, for matrix and mesh runs.
#
#   ./infra/run_mesh.sh start     # start all three (direct mode, no credentials)
#   ./infra/run_mesh.sh stop
#   ./infra/run_mesh.sh status
#
# CURRENCY_MODEL_MODE=llm starts them on their native models instead, which
# needs each cloud's credentials in the environment.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$REPO/.venv/bin/python}"
RUN_DIR="${RUN_DIR:-$REPO/.run}"
AGENTS=("gcp:10001" "aws:10002" "azure:10003")

mkdir -p "$RUN_DIR"

start() {
  for entry in "${AGENTS[@]}"; do
    local name="${entry%%:*}" port="${entry##*:}"
    local pidfile="$RUN_DIR/$name.pid"
    if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
      echo "$name already running (pid $(cat "$pidfile"))"
      continue
    fi
    PORT="$port" nohup "$PYTHON" -m "agents.$name.server" \
      >"$RUN_DIR/$name.log" 2>&1 &
    echo $! >"$pidfile"
    echo "$name starting on :$port (pid $!)"
  done
  echo "waiting for health..."
  for entry in "${AGENTS[@]}"; do
    local name="${entry%%:*}" port="${entry##*:}"
    for _ in $(seq 1 40); do
      if curl -sf -m 2 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
        echo "  $name ready"
        break
      fi
      sleep 0.5
    done
  done
}

stop() {
  for entry in "${AGENTS[@]}"; do
    local name="${entry%%:*}"
    local pidfile="$RUN_DIR/$name.pid"
    if [[ -f "$pidfile" ]]; then
      local pid
      pid="$(cat "$pidfile")"
      if kill -0 "$pid" 2>/dev/null; then
        kill "$pid" && echo "$name stopped (pid $pid)"
      fi
      rm -f "$pidfile"
    fi
  done
}

status() {
  for entry in "${AGENTS[@]}"; do
    local name="${entry%%:*}" port="${entry##*:}"
    if curl -sf -m 2 "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
      echo "$name  :$port  up"
    else
      echo "$name  :$port  DOWN   (see $RUN_DIR/$name.log)"
    fi
  done
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  restart) stop; sleep 1; start ;;
  status) status ;;
  *) echo "usage: $0 {start|stop|restart|status}" >&2; exit 2 ;;
esac
