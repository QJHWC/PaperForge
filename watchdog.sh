#!/usr/bin/env bash
# PaperForge training watchdog — auto-restarts training when log goes stale.
#
# Usage:
#   bash watchdog.sh start   # start watchdog in background
#   bash watchdog.sh status  # show watchdog + train status
#   bash watchdog.sh logs    # tail watchdog log
#   bash watchdog.sh stop    # stop watchdog
#
# Environment variables (all optional):
#   PROJECT_DIR          project root (default: script directory)
#   TRAIN_CMD            full command to (re)start training
#   TRAIN_LOG            path to training log file to monitor
#   TRAIN_PROCESS_PATTERN  grep pattern to find train process (default: "python.*experiment")
#   CHECK_INTERVAL_SEC   seconds between checks (default: 60)
#   STALE_THRESHOLD_SEC  seconds before log is considered stale (default: 900)

set -Eeuo pipefail

ACTION="${1:-start}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$SCRIPT_DIR}"
TRAIN_CMD="${TRAIN_CMD:-echo 'TRAIN_CMD not set; exiting' && exit 1}"
TRAIN_PROCESS_PATTERN="${TRAIN_PROCESS_PATTERN:-python.*experiment}"
CHECK_INTERVAL_SEC="${CHECK_INTERVAL_SEC:-60}"
STALE_THRESHOLD_SEC="${STALE_THRESHOLD_SEC:-900}"

WATCHDOG_PID_FILE="${WATCHDOG_PID_FILE:-$PROJECT_DIR/.watchdog.pid}"
WATCHDOG_LOG="${WATCHDOG_LOG:-$PROJECT_DIR/watchdog.log}"
TRAIN_LOG="${TRAIN_LOG:-$PROJECT_DIR/train.log}"

_start_training() {
  cd "$PROJECT_DIR"
  bash -lc "$TRAIN_CMD" >> "$TRAIN_LOG" 2>&1 || true
}

_watchdog_loop() {
  cd "$PROJECT_DIR"
  while true; do
    now_ts="$(date +%s)"
    train_pid="$(pgrep -f "$TRAIN_PROCESS_PATTERN" | head -n 1 || true)"

    if [[ -z "$train_pid" ]]; then
      echo "[$(date '+%F %T')] [WARN] train process not found, restarting..."
      _start_training &
      sleep "$CHECK_INTERVAL_SEC"
      continue
    fi

    if [[ ! -f "$TRAIN_LOG" ]]; then
      echo "[$(date '+%F %T')] [WARN] train log missing: $TRAIN_LOG, restarting..."
      pkill -f "$TRAIN_PROCESS_PATTERN" || true
      sleep 2
      _start_training &
      sleep "$CHECK_INTERVAL_SEC"
      continue
    fi

    if [[ "$(uname)" == "Darwin" ]]; then
      log_mtime="$(stat -f %m "$TRAIN_LOG" 2>/dev/null || echo 0)"
    else
      log_mtime="$(stat -c %Y "$TRAIN_LOG" 2>/dev/null || echo 0)"
    fi
    stale_sec=$((now_ts - log_mtime))

    if [[ "$stale_sec" -ge "$STALE_THRESHOLD_SEC" ]]; then
      echo "[$(date '+%F %T')] [WARN] stale log ${stale_sec}s >= ${STALE_THRESHOLD_SEC}s, restarting..."
      pkill -f "$TRAIN_PROCESS_PATTERN" || true
      sleep 3
      _start_training &
      sleep "$CHECK_INTERVAL_SEC"
      continue
    fi

    echo "[$(date '+%F %T')] [OK] healthy: pid=${train_pid}, stale=${stale_sec}s"
    sleep "$CHECK_INTERVAL_SEC"
  done
}

_start_watchdog() {
  if [[ -f "$WATCHDOG_PID_FILE" ]] && kill -0 "$(cat "$WATCHDOG_PID_FILE")" 2>/dev/null; then
    echo "[WARN] watchdog already running: pid=$(cat "$WATCHDOG_PID_FILE")"
    exit 0
  fi
  nohup env \
    PROJECT_DIR="$PROJECT_DIR" \
    TRAIN_CMD="$TRAIN_CMD" \
    TRAIN_PROCESS_PATTERN="$TRAIN_PROCESS_PATTERN" \
    CHECK_INTERVAL_SEC="$CHECK_INTERVAL_SEC" \
    STALE_THRESHOLD_SEC="$STALE_THRESHOLD_SEC" \
    WATCHDOG_PID_FILE="$WATCHDOG_PID_FILE" \
    WATCHDOG_LOG="$WATCHDOG_LOG" \
    TRAIN_LOG="$TRAIN_LOG" \
    bash "$0" _loop >> "$WATCHDOG_LOG" 2>&1 &
  echo "$!" > "$WATCHDOG_PID_FILE"
  echo "[OK] watchdog started: pid=$(cat "$WATCHDOG_PID_FILE")"
  echo "[OK] watchdog log: $WATCHDOG_LOG"
}

_show_status() {
  if [[ -f "$WATCHDOG_PID_FILE" ]]; then
    wpid="$(cat "$WATCHDOG_PID_FILE")"
    if kill -0 "$wpid" 2>/dev/null; then
      echo "[OK] watchdog running: pid=$wpid"
    else
      echo "[WARN] watchdog pid file exists but process dead: $wpid"
    fi
  else
    echo "[INFO] no watchdog running"
  fi
  tpid="$(pgrep -f "$TRAIN_PROCESS_PATTERN" | head -n1 || true)"
  if [[ -n "$tpid" ]]; then
    echo "[OK] train running: pid=$tpid"
  else
    echo "[WARN] train not running"
  fi
  if [[ -f "$TRAIN_LOG" ]]; then
    echo "[INFO] train log:"
    ls -la "$TRAIN_LOG"
  fi
}

_stop_watchdog() {
  if [[ -f "$WATCHDOG_PID_FILE" ]]; then
    wpid="$(cat "$WATCHDOG_PID_FILE")"
    if kill -0 "$wpid" 2>/dev/null; then
      kill "$wpid"
      echo "[OK] watchdog stopped: pid=$wpid"
    else
      echo "[INFO] watchdog not running: pid=$wpid"
    fi
    rm -f "$WATCHDOG_PID_FILE"
  else
    echo "[INFO] no watchdog pid file"
  fi
}

case "$ACTION" in
  _loop)  _watchdog_loop ;;
  start)  _start_watchdog ;;
  status) _show_status ;;
  logs)   tail -f "$WATCHDOG_LOG" ;;
  stop)   _stop_watchdog ;;
  *)
    echo "Usage: bash watchdog.sh [start|status|logs|stop]"
    echo ""
    echo "Environment variables:"
    echo "  TRAIN_CMD              command to start/resume training"
    echo "  TRAIN_LOG              log file to monitor for staleness"
    echo "  TRAIN_PROCESS_PATTERN  pgrep pattern to find train process"
    echo "  CHECK_INTERVAL_SEC     check interval (default: 60)"
    echo "  STALE_THRESHOLD_SEC    stale threshold (default: 900)"
    exit 1
    ;;
esac
