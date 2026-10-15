---
name: feishu-gpu-board
description: Set up, extend, check and repair a live Feishu / Lark Base (多维表格) board that shows which NVIDIA GPU cards are free or busy on a set of SSH-reachable boxes, with memory, utilization, processes and the served model name + ip:port per card. Use when the user wants a GPU availability board, a free-cards table or a "who is using which GPU" view in Feishu or Lark; wants to add or remove a GPU box from such a board; or says the board is stale, frozen or wrong.
license: MIT
---

# Feishu GPU board

The kit puts one row per GPU card into a Feishu Base table and refreshes it
every 60 s. Each GPU box runs three stdlib-only Python processes from
`~/gpu-board/`, kept alive by a generated `supervise.sh`:

- `node_agent.py` and `cpu_agent.py` are read-only, have no network access
  and hold no secret.
- `master.py` is the only process that calls the Feishu API.

Everything is driven from the user's machine over SSH by `deploy/deploy.sh`.
Design details are in `docs/architecture.md` in the kit.

## 0. Find the kit

Every command below runs from the kit root, the directory that contains
`deploy/deploy.sh` and `board/master.py`.

1. If the current directory or one of its parents is the kit, use it.
2. Otherwise clone it: `git clone https://github.com/mjykxz17/feishu-gpu-board ~/feishu-gpu-board`.
   Ask before cloning anywhere else.
   - In Claude Code with this plugin installed, `${CLAUDE_PLUGIN_ROOT}` is a
     copy of the kit. Treat it as read-only: plugin updates replace it, so
     never put `targets.conf` there. Clone a working copy instead.

## Ground rules

- **The app secret.** Never print, `cat`, `echo` or log it. Never put it on
  a command line or in `targets.conf`. It lives only in `SECRET_FILE`
  (default `~/.config/gpu-board/secret`, one line `FEISHU_APP_SECRET=…`,
  mode 600).
  - Ask the user to run `bash deploy/deploy.sh secret` from the kit root in
    **their own terminal**. It prompts with hidden input and writes the file
    with mode 600.
  - Don't suggest running it through the agent (for example, Claude Code's
    `!` prefix): that puts the command and its I/O into the conversation.
  - If the user pastes the secret into chat anyway, write the file once with
    `umask 077`, never repeat the secret, and suggest rotating it in the
    developer console later.
- **Before the first deploy to a box**, tell the user it starts three
  long-running processes under `~/gpu-board` on that box, and show the
  command.
- **Touch only the kit's own processes and files.** `deploy.sh stop` /
  `uninstall` check the command line before killing anything and remove only
  the files a deploy created. Never `kill` other processes. Never `rm -rf` the
  install directory yourself.
- **GPU boxes are often shared.** Don't install packages or change system
  config on them. The kit needs nothing beyond `python3` ≥ 3.7 and
  `nvidia-smi`.
- **TLS and host keys.** Never disable TLS verification to get past a proxy;
  run the API step through a GPU box with `--via` instead. Never accept a
  *changed* SSH host key without the user confirming the box was rebuilt.

## 1. New board

**a. Gather what's missing.** Ask in one message, and only for what you don't
already have:

- **GPU boxes.** An SSH alias or `user@host` for each box, plus any key or
  port (these go in `SSH_OPTS` or `~/.ssh/config`). Also a short unique
  **node label** per box, which appears in the `节点` column.
- **Feishu or Lark.** Lark uses `FEISHU_BASE_URL=https://open.larksuite.com`.
  You also need the **App ID** (`cli_…`) and the **Base URL**. The Base token
  is the path segment after `/base/`.
- **Has the app been set up?** Confirm that the `bitable:app` scope is added
  and a version published, and that the app has been added to the Base with
  edit rights (Base `…` → 添加文档应用 / Add document app). If not, walk the
  user through README section "1. Feishu app + Base"; only they can click
  through the developer console.
- **Table layout.** The default is one GPU table per GPU type (e.g. `H200`,
  `B200`) plus a `CPU 负载` table. Bookings columns are on by default.

**b. Configure.**

```bash
cp deploy/targets.example.conf deploy/targets.conf   # gitignored; bash syntax
```

Set `FEISHU_APP_ID`, `FEISHU_BASE_TOKEN` and `FEISHU_BASE_URL`, plus one
line per box: `target <name> <ssh-host> <node-label> <table-id>`. Use
`PENDING` as the table id until step d. Create the secret file as described
in the ground rules.

**c. Preflight** (read-only, except that it creates `~/gpu-board` on each box).

```bash
bash deploy/deploy.sh all preflight
```

Every line should say `ok`. `FAIL table id` is expected before step d. Fix
anything else before going on:

| FAIL | Fix |
|---|---|
| `ssh <host>` | Fix SSH first: alias, key, port. Check with `ssh -o BatchMode=yes <host> true`. |
| `nvidia-smi sees 0 GPU(s)` | Not a GPU box, or the driver isn't visible to this user. |
| `python3 … (need 3.7+)` | Set `PYTHON=/path/to/python3.x` in `targets.conf` (the interpreter **on the boxes**; local `setup` always uses the laptop's own `python3`). |
| `reach https://open.feishu.cn` | The box has no outbound HTTPS, so its master can't write to Feishu. The kit can't fix this; leave the box out, or get its egress opened. |
| `… writable` | Set `REMOTE_DIR` to a writable path. |

**d. Create the tables.**

```bash
bash deploy/deploy.sh setup --gpu-table H200 --gpu-table B200 --cpu-table "CPU 负载"
```

- The output ends with lines like `TABLE gpu H200 tbl…`. Write each GPU table
  id into the matching `target` lines, and the CPU id into
  `FEISHU_CPU_TABLE_ID`.
- If the laptop can't reach the Feishu API (SSL errors or a corporate
  proxy), rerun with `--via <ssh-host-of-any-target>` added anywhere in the
  command.
- The command is idempotent, so it also repairs a table that lost columns.

**e. Deploy and verify.**

```bash
bash deploy/deploy.sh all          # expect: [name] OK supervisor=…  and  feishu: pushed … created=8
bash deploy/deploy.sh all status
```

If a target prints `FAIL`, read the log tail it prints and use the error
table in section 4. Then ask the user to open the Base; there should be one
row per card.

**f. Keep it alive (offer this).** If the boxes are Kubernetes pods, or
anything that restarts and loses processes, offer the watchdog:

```bash
bash deploy/install_watchdog.sh
```

On macOS this installs a LaunchAgent that runs every 5 min. On Linux it
prints a crontab line. Tell the user it only works while that machine is
awake. For true self-healing, `~/gpu-board/supervise.sh` has to start with
the pod.

**g. Report.** Tell the user the tables and ids, the number of cards per
table, how to check (`deploy.sh all status`), and how to stop
(`deploy.sh all stop`).

## 2. Add or remove a box

- **Add**: append a `target` line, choosing the existing table for its GPU
  type or running `setup --gpu-table <NewType>` first. Then run
  `deploy.sh <name> preflight` and `deploy.sh <name>`.
- **Remove**: run `deploy.sh <name> uninstall`, then delete the line. If
  another box shares its table, its rows turn Stale within about 4 min;
  otherwise they just stop updating. The master never deletes real rows, so
  the user deletes those rows in the UI (or you do it through the API if they
  ask).

## 3. "The board is stale / frozen / wrong"

1. `bash deploy/deploy.sh all check` gives one line per box,
   `<name>: <state> …`:
   - `healthy`: the stack is fine; look at the table or column issues below.
   - `down`: SSH works but the stack is dead. Usually the pod or box
     restarted. Run `deploy.sh <name> status` to confirm, then
     `deploy.sh <name>` to redeploy.
   - `unreachable`: SSH failed. Check the network or VPN, and whether the box
     still exists. A changed host key means it was rebuilt; confirm with the
     user before updating `known_hosts`.
2. If it's still wrong after a healthy push, read the last lines of
   `master.log` (shown by `status`) and match them against the error table
   below.
3. To see exactly what the agent sees on a box, with that box's own
   settings, run `bash deploy/deploy.sh <name> print`.

## 4. Errors → fixes

| In `master.log` / output | Cause → fix |
|---|---|
| `token error` | Wrong app id or secret. Check `targets.conf` and have the user re-write the secret file. |
| forbidden / no permission | The app isn't on the Base with edit rights, or its scope version isn't published. |
| `table has no 节点/卡号 column` / `FieldNameNotFound` / a column missing | A column was renamed or deleted in the UI. Rerun `deploy.sh setup …` with the same table names, then redeploy so the master re-reads the column list. |
| `quota dead` | Monthly API quota exhausted. The master is already on the v3 fallback. Raise `CYCLE_SECONDS` and `STALE_SECONDS` (≥ 2 × cycle + 60), then redeploy. |
| `no snapshots in …` | The node agent isn't writing. Check `node_agent.log` (`nvidia-smi` failing?). |
| Card **Busy** with no process listed | Memory ≥ `FREE_MEM_MB` held by a process in another container or PID namespace. This is expected and correct. |
| Model/endpoint blank on a serving card | A custom wrapper isn't recognised: add its launch command (with the verb) to `SERVE_PATTERNS`, which extends the built-ins. `sglang serve --config x.yaml` is recognised, but its model stays blank and its endpoint shows the default port, because both live in the YAML. |
| Endpoint has the wrong IP | The box is multi-homed. Set `SERVE_IP_PROBE` to an address on the network people call from. |
| A box's `更新时间` is old but its cards aren't Stale | It is the only box on its table and it is down, so nobody is left to mark it. Run `check`, then redeploy. |
| Old Stale rows next to fresh ones for the same box | The node label changed. Labels must be unique and stable. Delete the old label's rows in the UI. |

## Command reference

```text
deploy.sh targets
deploy.sh secret                                             # user runs it: hidden prompt -> SECRET_FILE
deploy.sh setup [--via HOST] --gpu-table NAME [...] [--cpu-table NAME] [--no-reservations] [--no-views]
deploy.sh <target|all> [preflight|status|print|stop|uninstall]   # no action = deploy
deploy.sh <target|all> check                                 # healthy|down|unreachable (one target: exit 0|1|2)
install_watchdog.sh [uninstall]
CONFIG=/path/to/other.conf deploy.sh …                       # alternate config (the watchdog keeps the default one)
```

On each box, `~/gpu-board/` (or `REMOTE_DIR`) contains:

- `node_agent.py`, `cpu_agent.py`, `master.py`
- `agent.env`, and `master.env` (0600, holds the secret)
- `supervise.sh`, `supervise.pid`
- `*.log`, `nodes/<label>.json`, `cpu_snaps/<label>.json`
