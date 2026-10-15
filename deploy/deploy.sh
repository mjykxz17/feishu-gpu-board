#!/bin/bash
# deploy.sh — install, run, check and repair the GPU board on your boxes.
#
#   bash deploy/deploy.sh targets                       list the configured targets
#   bash deploy/deploy.sh secret                        type the app secret (hidden) into SECRET_FILE
#   bash deploy/deploy.sh setup [--via HOST] --gpu-table NAME [--gpu-table NAME ...] [--cpu-table NAME]
#                                                       create the Base tables, print their ids
#   bash deploy/deploy.sh <target|all> preflight        checks: ssh, nvidia-smi, python3, Feishu
#                                                       reachable, REMOTE_DIR writable (creates it)
#   bash deploy/deploy.sh <target|all>                  install/upgrade + (re)start the stack
#   bash deploy/deploy.sh <target|all> status           processes, snapshot ages, master log
#   bash deploy/deploy.sh <target|all> check            healthy | down | unreachable; for one
#                                                       target exit 0 / 1 / 2 (watchdog.sh uses it)
#   bash deploy/deploy.sh <target|all> print            what node_agent sees right now, per card
#   bash deploy/deploy.sh <target|all> stop             stop the stack, keep its files
#   bash deploy/deploy.sh <target|all> uninstall        stop the stack and delete its files
#
# Config: deploy/targets.conf (start from targets.example.conf), or CONFIG=/path.
#
# Each target box gets REMOTE_DIR with three Python files (no repo checkout),
# two env files and supervise.sh, which keeps node_agent.py, cpu_agent.py and
# master.py alive with one restart loop each — no cron or systemd needed, so it
# works inside Kubernetes pods. A pod restart still kills it: rerun this script,
# or let watchdog.sh do it for you.
#
# The app secret is read from SECRET_FILE and sent inside the script on ssh's
# stdin, never on a command line. On the box it lives only in master.env (0600);
# the two agents never see it.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd -P)"
BOARD="$(cd "$HERE/../board" && pwd -P)"
CONFIG=${CONFIG:-$HERE/targets.conf}
export CONFIG

# --- defaults (override any of them in targets.conf) -------------------------
FEISHU_APP_ID=
FEISHU_BASE_TOKEN=
FEISHU_BASE_URL=https://open.feishu.cn
FEISHU_CPU_TABLE_ID=
SECRET_FILE=~/.config/gpu-board/secret
REMOTE_DIR=gpu-board        # relative = under the remote user's $HOME
PYTHON=python3              # interpreter on the boxes (3.7+)
CYCLE_SECONDS=60            # agent + master loop period
CPU_PUSH_SECONDS=300        # CPU table update period
STALE_SECONDS=180           # snapshot older than this -> Stale
FREE_MEM_MB=2048            # Free = no compute process AND memory below this
SERVE_IP_PROBE=1.1.1.1      # pick the NIC whose address goes in 服务端点
SERVE_PATTERNS=             # empty = node_agent.py defaults
WORKLOAD_PATTERNS=          # empty = cpu_agent.py defaults
SSH_OPTS=                   # extra ssh options, e.g. "-i ~/.ssh/gpu_key"
BOARD_UTC_OFFSET=           # hours; time zone bookings end in. Empty = this machine's
# Optional master settings, passed through only when set:
FEISHU_PREFER_V3=; SWEEP_SECONDS=; DETAIL_SECONDS=; CPU_STALE_SECONDS=

NAMES=(); HOSTS=(); LABELS=(); TABLES=()
target() {
  [ $# -eq 4 ] || { echo "$CONFIG: 'target' needs 4 fields: name ssh-host node-label gpu-table-id (got: $*)" >&2; exit 1; }
  NAMES+=("$1"); HOSTS+=("$2"); LABELS+=("$3"); TABLES+=("$4")
}
if [ -f "$CONFIG" ]; then
  # shellcheck source=/dev/null
  . "$CONFIG"
elif [ "${1:-}" != secret ]; then
  echo "no config at $CONFIG — copy deploy/targets.example.conf to deploy/targets.conf and fill it in" >&2; exit 1
fi
if [ -z "$BOARD_UTC_OFFSET" ]; then     # this machine's offset, e.g. +0800 -> 8, +0530 -> 5.50
  z=$(date +%z); h=$((10#${z:1:2})); m=$((10#${z:3:2}))
  BOARD_UTC_OFFSET="${z:0:1}$h"; [ "$m" = 0 ] || BOARD_UTC_OFFSET="$BOARD_UTC_OFFSET.$((m * 100 / 60))"
fi
FRESH=${FRESH:-$((CYCLE_SECONDS * 2 + 180))}   # check: older snapshot/log than this = down

usage() { sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

find_target() {
  local i
  for i in ${NAMES[@]+"${!NAMES[@]}"}; do
    if [ "${NAMES[$i]}" = "$1" ]; then
      HOST=${HOSTS[$i]}; NODE_LABEL=${LABELS[$i]}; TABLE_ID=${TABLES[$i]}; return 0
    fi
  done
  return 1
}

read -r -a EXTRA_SSH <<< "$SSH_OPTS"
ssh_to() { ssh -o ConnectTimeout=20 -o BatchMode=yes ${EXTRA_SSH[@]+"${EXTRA_SSH[@]}"} "$1" 'bash -s'; }

read_secret() {
  [ -f "$SECRET_FILE" ] || { echo "missing $SECRET_FILE (one line: FEISHU_APP_SECRET=...)" >&2; exit 1; }
  SECRET=$(sed -n 's/^FEISHU_APP_SECRET=//p' "$SECRET_FILE" | head -1)
  [ -n "$SECRET" ] || { echo "no FEISHU_APP_SECRET= line in $SECRET_FILE" >&2; exit 1; }
  [ -n "$FEISHU_APP_ID" ] && [ -n "$FEISHU_BASE_TOKEN" ] \
    || { echo "set FEISHU_APP_ID and FEISHU_BASE_TOKEN in $CONFIG" >&2; exit 1; }
}

# Remote prelude: resolve REMOTE_DIR on the box (relative -> under $HOME).
prelude() {
  printf 'DIR=%q\ncase "$DIR" in /*) ;; *) DIR="$HOME/$DIR" ;; esac\n' "$REMOTE_DIR"
  printf 'LABEL=%q\n' "${NODE_LABEL:-}"
}

b64() { printf "base64 -d <<'B64' | gunzip > \"\$DIR/%s\"\n" "$2"; gzip -9c "$1" | base64; echo B64; }

case "${1:-}" in
  ""|-h|--help) usage ;;
  targets) echo ${NAMES[@]+"${NAMES[@]}"}; exit 0 ;;
  secret)
    [ -t 0 ] || { echo "run this in a terminal: it reads the secret from the keyboard" >&2; exit 1; }
    mkdir -p "$(dirname "$SECRET_FILE")"
    read -rsp "Feishu app secret (input hidden): " s; echo
    [ -n "$s" ] || { echo "empty, nothing written" >&2; exit 1; }
    (umask 077; printf 'FEISHU_APP_SECRET=%s\n' "$s" > "$SECRET_FILE")
    echo "wrote $SECRET_FILE (mode 600)"; exit 0
    ;;
  setup)
    shift
    VIA=""; ARGS=()
    while [ $# -gt 0 ]; do     # --via may appear anywhere
      if [ "$1" = --via ]; then VIA=${2:?--via needs an ssh host}; shift 2; else ARGS+=("$1"); shift; fi
    done
    [ ${#ARGS[@]} -gt 0 ] || { echo "setup needs --gpu-table NAME and/or --cpu-table NAME" >&2; exit 1; }
    set -- "${ARGS[@]}"
    # the interpreter on the box for --via, the laptop's own python3 otherwise
    if [ -n "$VIA" ]; then SETUP_PY=$PYTHON; else SETUP_PY=python3; fi
    read_secret
    {
      echo 'set -euo pipefail; f=$(mktemp); trap '"'"'rm -f "$f"'"'"' EXIT'
      printf "base64 -d <<'B64' | gunzip > \"\$f\"\n"; gzip -9c "$BOARD/setup_tables.py" | base64; echo B64
      printf 'export FEISHU_APP_ID=%q FEISHU_BASE_TOKEN=%q FEISHU_BASE_URL=%q\n' \
        "$FEISHU_APP_ID" "$FEISHU_BASE_TOKEN" "$FEISHU_BASE_URL"
      printf 'printf "%%s\\n" %q | %q "$f" --secret-stdin' "$SECRET" "$SETUP_PY"
      printf ' %q' "$@"; echo
    } | if [ -n "$VIA" ]; then ssh_to "$VIA"; else bash -s; fi
    exit $?
    ;;
  all)
    rc=0
    for t in ${NAMES[@]+"${NAMES[@]}"}; do
      if [ "${2:-}" = check ]; then printf '%s: ' "$t"; fi   # one unlabelled line per target otherwise
      bash "$0" "$t" "${2:-}" || rc=1
    done
    exit $rc
    ;;
esac

NAME=$1
ACTION=${2:-deploy}
find_target "$NAME" || { echo "unknown target '$NAME' (configured: ${NAMES[*]:-none})" >&2; exit 1; }

case "$ACTION" in
check)
  # Healthy = our supervisor is alive AND the node snapshot and the master log
  # were both written within FRESH seconds. Read-only on the box.
  out=$({ prelude; cat <<'CHECK'
now=$(date +%s)
age() { if [ -e "$1" ]; then echo $((now - $(stat -c %Y "$1"))); else echo 999999; fi; }
sup=0
pid=$(cat "$DIR/supervise.pid" 2>/dev/null)
[ -n "$pid" ] && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -qF "$DIR/supervise.sh" && sup=1
echo "sup=$sup snap=$(age "$DIR/nodes/$LABEL.json") mlog=$(age "$DIR/master.log")"
CHECK
  } | ssh_to "$HOST" 2>/dev/null)
  # Parse strictly, never eval: the line comes from the box.
  re='^sup=([01]) snap=([0-9]+) mlog=([0-9]+)$'
  if [[ $out =~ $re ]]; then
    sup=${BASH_REMATCH[1]}; snap=${BASH_REMATCH[2]}; mlog=${BASH_REMATCH[3]}
  else
    echo "unreachable"; exit 2
  fi
  if [ "$sup" = 1 ] && [ "$snap" -lt "$FRESH" ] && [ "$mlog" -lt "$FRESH" ]; then
    echo "healthy $out"; exit 0
  fi
  echo "down $out"; exit 1
  ;;

status)
  echo "=== $NAME ($NODE_LABEL @ $HOST)"
  { prelude; cat <<'STATUS'
echo "--- processes ($DIR)"
pgrep -af "$DIR/(supervise\.sh|node_agent\.py|cpu_agent\.py|master\.py)" || echo 'NOT RUNNING'
now=$(date +%s)
for f in "$DIR"/nodes/*.json "$DIR"/cpu_snaps/*.json; do
  [ -e "$f" ] && echo "$f  $((now - $(stat -c %Y "$f")))s ago"
done
echo '--- master.log'; tail -n 5 "$DIR/master.log" 2>/dev/null
STATUS
  } | ssh_to "$HOST"
  ;;

preflight)
  echo "=== $NAME ($NODE_LABEL @ $HOST)"
  [ -n "$TABLE_ID" ] && [ "${TABLE_ID#tbl}" != "$TABLE_ID" ] && echo "ok   table id $TABLE_ID" \
    || echo "FAIL table id '$TABLE_ID' does not look like tbl... (run: deploy.sh setup ...)"
  { [ -f "$SECRET_FILE" ] && grep -q '^FEISHU_APP_SECRET=.' "$SECRET_FILE"; } \
    && echo "ok   secret file $SECRET_FILE" || echo "FAIL secret file $SECRET_FILE (one line: FEISHU_APP_SECRET=...)"
  { prelude; printf 'PY=%q\nAPI=%q\n' "$PYTHON" "$FEISHU_BASE_URL"; cat <<'PRE'
say() { if [ "$1" = 0 ]; then echo "ok   $2"; else echo "FAIL $2"; fi; }
echo "ok   ssh as $(id -un)@$(hostname)"
n=$(nvidia-smi -L 2>/dev/null | grep -c '^GPU'); [ "$n" -gt 0 ]; say $? "nvidia-smi sees $n GPU(s)"
v=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 7))' 2>/dev/null; say $? "$PY ${v:-missing} (need 3.7+)"
"$PY" - "$API" <<'PY' 2>/dev/null; say $? "reach $API from this box"
import sys, urllib.request, urllib.error
try:
    urllib.request.urlopen(sys.argv[1] + "/open-apis/auth/v3/tenant_access_token/internal", timeout=10)
except urllib.error.HTTPError:
    pass  # any HTTP answer means the API is reachable
PY
mkdir -p "$DIR" 2>/dev/null && [ -w "$DIR" ]; say $? "$DIR writable"
pid=$(cat "$DIR/supervise.pid" 2>/dev/null)
if [ -n "$pid" ] && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -qF "$DIR/supervise.sh"; then
  echo "note stack already running (supervisor $pid) - a deploy restarts it"
fi
PRE
  } | ssh_to "$HOST" || echo "FAIL ssh $HOST"
  ;;

print)
  echo "=== $NAME ($NODE_LABEL @ $HOST)"
  { prelude; printf 'PY=%q\n' "$PYTHON"; cat <<'PRINT'
[ -f "$DIR/node_agent.py" ] || { echo "not deployed"; exit 1; }
cd "$DIR" && export DIR && set -a && . ./agent.env && set +a && "$PY" node_agent.py --print
PRINT
  } | ssh_to "$HOST"
  ;;

stop|uninstall)
  { prelude; printf 'ACTION=%q\nNAME=%q\n' "$ACTION" "$NAME"; cat <<'STOP'
pid=$(cat "$DIR/supervise.pid" 2>/dev/null)
if [ -n "$pid" ] && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -qF "$DIR/supervise.sh"; then
  kill -- "-$pid" 2>/dev/null || kill "$pid"; echo "[$NAME] stopped supervisor $pid"
else
  echo "[$NAME] not running"
fi
rm -f "$DIR/supervise.pid"
if [ "$ACTION" = uninstall ] && [ -f "$DIR/supervise.sh" ]; then
  # Only the files a deploy creates - never rm -rf a configurable path.
  rm -f "$DIR"/{node_agent.py,cpu_agent.py,master.py,supervise.sh,agent.env,master.env}
  rm -f "$DIR"/*.log "$DIR"/*.log.prev "$DIR"/nodes/*.json "$DIR"/cpu_snaps/*.json
  rmdir "$DIR/nodes" "$DIR/cpu_snaps" "$DIR" 2>/dev/null
  if [ -d "$DIR" ]; then echo "[$NAME] removed the board files; kept $DIR (it holds other files)"
  else echo "[$NAME] removed $DIR"; fi
fi
STOP
  } | ssh_to "$HOST"
  ;;

deploy)
  read_secret
  [ "$STALE_SECONDS" -ge $((CYCLE_SECONDS * 2 + 60)) ] || echo "warning: STALE_SECONDS=$STALE_SECONDS is short for CYCLE_SECONDS=$CYCLE_SECONDS; cards may flap to Stale (use >= $((CYCLE_SECONDS * 2 + 60)))" >&2
  [ "${TABLE_ID#tbl}" != "$TABLE_ID" ] \
    || { echo "target $NAME: '$TABLE_ID' is not a table id (tbl…); create tables with: deploy.sh setup" >&2; exit 1; }
  TMPOUT=$(mktemp); trap 'rm -f "$TMPOUT"' EXIT
  {
    echo 'set -euo pipefail'
    prelude
    printf 'NAME=%q\n' "$NAME"
    echo 'mkdir -p "$DIR/nodes" "$DIR/cpu_snaps"; umask 077'
    b64 "$BOARD/node_agent.py" node_agent.py
    b64 "$BOARD/cpu_agent.py" cpu_agent.py
    b64 "$BOARD/master.py" master.py
    # Env files: values quoted here; $DIR is expanded when supervise.sh sources them.
    echo "cat > \"\$DIR/agent.env\" <<'ENV'"
    printf 'NODE_LABEL=%q\nFREE_MEM_MB=%q\nSERVE_IP_PROBE=%q\n' "$NODE_LABEL" "$FREE_MEM_MB" "$SERVE_IP_PROBE"
    echo 'SNAP_DIR=$DIR/nodes'
    echo 'SNAP_OUT_DIR=$DIR/cpu_snaps'
    [ -n "$SERVE_PATTERNS" ] && printf 'SERVE_PATTERNS=%q\n' "$SERVE_PATTERNS"
    [ -n "$WORKLOAD_PATTERNS" ] && printf 'WORKLOAD_PATTERNS=%q\n' "$WORKLOAD_PATTERNS"
    echo ENV
    echo "cat > \"\$DIR/master.env\" <<'ENV'"
    printf 'FEISHU_APP_ID=%q\nFEISHU_APP_SECRET=%q\nFEISHU_BASE_TOKEN=%q\n' "$FEISHU_APP_ID" "$SECRET" "$FEISHU_BASE_TOKEN"
    printf 'FEISHU_TABLE_ID=%q\nFEISHU_BASE_URL=%q\nFEISHU_CPU_TABLE_ID=%q\n' "$TABLE_ID" "$FEISHU_BASE_URL" "$FEISHU_CPU_TABLE_ID"
    printf 'STALE_SECONDS=%q\nCPU_PUSH_SECONDS=%q\nBOARD_UTC_OFFSET=%q\n' \
      "$STALE_SECONDS" "$CPU_PUSH_SECONDS" "$BOARD_UTC_OFFSET"
    for v in FEISHU_PREFER_V3 SWEEP_SECONDS DETAIL_SECONDS CPU_STALE_SECONDS; do
      [ -n "${!v:-}" ] && printf '%s=%q\n' "$v" "${!v}"
    done
    echo 'SNAP_DIR=$DIR/nodes'
    echo 'CPU_SNAP_DIR=$DIR/cpu_snaps'
    echo ENV
    echo 'chmod 600 "$DIR/master.env" "$DIR/agent.env"'
    echo "cat > \"\$DIR/supervise.sh\" <<'SUP'"
    echo '#!/bin/bash'
    echo '# One restart loop per component: no cron/systemd needed. Written by deploy.sh.'
    echo 'DIR="$(cd "$(dirname "$0")" && pwd)"; export DIR'
    printf 'PY=%q\nCYCLE=%q\n' "$PYTHON" "$CYCLE_SECONDS"
    cat <<'SUP'
run() {
  local name=$1; shift
  while true; do
    "$@" >> "$DIR/$name.log" 2>&1
    echo "$(date '+%F %T') $name exited ($?), restarting in 10s" >> "$DIR/$name.log"
    sleep 10
  done
}
trim_logs() {   # keep each log under ~5 MB (the components append forever)
  while sleep 3600; do
    for f in "$DIR"/*.log; do
      [ "$(stat -c %s "$f" 2>/dev/null || echo 0)" -gt 5000000 ] && tail -n 2000 "$f" > "$f.prev" && : > "$f"
    done
  done
}
run node_agent bash -c 'set -a; . "$DIR/agent.env"; set +a; exec "$0" "$DIR/node_agent.py" --loop "$1"' "$PY" "$CYCLE" &
run cpu_agent  bash -c 'set -a; . "$DIR/agent.env"; set +a; exec "$0" "$DIR/cpu_agent.py" --loop "$1"' "$PY" "$CYCLE" &
run master     bash -c 'sleep 5; set -a; . "$DIR/master.env"; set +a; exec "$0" "$DIR/master.py" --loop "$1"' "$PY" "$CYCLE" &
trim_logs &
wait
SUP
    echo SUP
    cat <<'START'
chmod 700 "$DIR/supervise.sh"
# Stop a previous install: its whole process group, verified by cmdline first.
old=$(cat "$DIR/supervise.pid" 2>/dev/null || true)
if [ -n "$old" ] && tr '\0' ' ' < "/proc/$old/cmdline" 2>/dev/null | grep -qF "$DIR/supervise.sh"; then
  kill -- "-$old" 2>/dev/null || true; sleep 2
  echo "[$NAME] stopped previous supervisor $old"
fi
for f in node_agent cpu_agent master; do
  if [ -f "$DIR/$f.log" ]; then mv "$DIR/$f.log" "$DIR/$f.log.prev"; fi
done
setsid nohup "$DIR/supervise.sh" > /dev/null 2>&1 < /dev/null &
echo $! > "$DIR/supervise.pid"
# Wait for the first master cycle (it starts 5 s after the agents).
for i in $(seq 1 30); do
  sleep 2
  grep -qE 'pushed|push ok|push \(|failed|error' "$DIR/master.log" 2>/dev/null && break
done
sleep 3   # let the CPU-table push of the same cycle land in the log too
if [ -f "$DIR/nodes/$LABEL.json" ] && grep -q 'pushed\|push ok\|push (' "$DIR/master.log" 2>/dev/null \
   && ! grep -q 'failed\|error' "$DIR/master.log"; then
  echo "[$NAME] OK supervisor=$(cat "$DIR/supervise.pid")"
  tail -n 2 "$DIR/master.log" | sed 's/^/    /'
else
  echo "[$NAME] FAIL"
  tail -n 5 "$DIR/node_agent.log" "$DIR/master.log" 2>/dev/null | sed 's/^/    /'
fi
START
  } | ssh_to "$HOST" | tee "$TMPOUT"

  # Keep the watchdog's copy of the kit equal to what was just deployed, so
  # after a pod restart it brings back this code, not an older copy.
  if [ "$(uname)" = Darwin ]; then KIT_DIR="$HOME/Library/Application Support/gpu-board-watchdog"
  else KIT_DIR="$HOME/.local/share/gpu-board-watchdog"; fi
  # Only for the default config: a deploy with CONFIG=other.conf must not
  # swap the watchdog over to that config's targets.
  if grep -q "^\[$NAME\] OK" "$TMPOUT" && [ -d "$KIT_DIR/deploy" ] \
     && [ "$CONFIG" = "$HERE/targets.conf" ] \
     && [ "$HERE" != "$(cd "$KIT_DIR/deploy" && pwd -P)" ]; then
    cp "$HERE/deploy.sh" "$HERE/watchdog.sh" "$KIT_DIR/deploy/"
    cp "$CONFIG" "$KIT_DIR/deploy/targets.conf"
    cp "$BOARD"/*.py "$KIT_DIR/board/"
  fi
  grep -q "^\[$NAME\] OK" "$TMPOUT"
  ;;

*) usage ;;
esac
