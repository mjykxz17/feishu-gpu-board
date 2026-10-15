#!/bin/bash
# watchdog.sh — bring the board back after a box or pod restart.
#
# Run every 5 minutes by launchd (macOS) or cron (Linux); install_watchdog.sh
# sets that up. GPU boxes that are Kubernetes pods have no cron or systemd, so
# a pod restart silently kills the board stack and nothing on the box can
# restart it. For each deploy.sh target:
#   healthy      -> nothing
#   unreachable  -> nothing (this machine is offline, or the box is gone)
#   down         -> deploy.sh <target>, at most once per BACKOFF seconds
# Only state changes are logged.
set -uo pipefail
export PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="${STATE:-$HERE/../state}"
if [ "$(uname)" = Darwin ]; then LOG="${LOG:-$HOME/Library/Logs/gpu-board-watchdog.log}"
else LOG="${LOG:-$HOME/.local/state/gpu-board/watchdog.log}"; fi
BACKOFF=${BACKOFF:-900}
mkdir -p "$STATE" "$(dirname "$LOG")"

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }
notify() {
  [ "${NOTIFY:-1}" = 1 ] || return 0
  if command -v osascript >/dev/null; then
    osascript -e "display notification \"$1\" with title \"GPU board\"" >/dev/null 2>&1 || true
  elif command -v notify-send >/dev/null; then
    notify-send "GPU board" "$1" >/dev/null 2>&1 || true
  fi
}

if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 2000 ]; then
  tail -n 500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

# One run at a time: launchd never overlaps a job, but a manual run could.
LOCK="$STATE/lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  [ -n "$(find "$LOCK" -maxdepth 0 -mmin +30 2>/dev/null)" ] || exit 0  # live run
  rmdir "$LOCK" && mkdir "$LOCK" || exit 0                              # left by a killed run
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

now=$(date +%s)
for t in $(bash "$HERE/deploy.sh" targets); do
  line=$(bash "$HERE/deploy.sh" "$t" check 2>&1); rc=$?
  state=${line%% *}                       # healthy | down | unreachable
  [ "$state" = "$(cat "$STATE/$t.state" 2>/dev/null)" ] || log "$t: $line"
  echo "$state" > "$STATE/$t.state"
  [ "$rc" = 1 ] || continue
  last=$(cat "$STATE/$t.deployed" 2>/dev/null || echo 0)
  if [ $((now - last)) -lt "$BACKOFF" ]; then
    log "$t: still down $((now - last))s after the last redeploy, waiting"
    continue
  fi
  echo "$now" > "$STATE/$t.deployed"
  log "$t: redeploying"
  out=$(bash "$HERE/deploy.sh" "$t" 2>&1)
  if echo "$out" | grep -q "^\[$t\] OK"; then
    log "$t: $(echo "$out" | grep "^\[$t\] OK")"
    notify "$t: board was down (box restarted?) - redeployed"
  else
    log "$t: redeploy FAILED"; echo "$out" | tail -8 | sed 's/^/    /' >> "$LOG"
    notify "$t: automatic redeploy failed, see $LOG"
  fi
done
