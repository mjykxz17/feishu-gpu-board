# feishu-gpu-board

**English** | [简体中文](README.zh-CN.md)

**A live "which GPUs are free?" board for your team, inside Feishu / Lark.**

Every card on every GPU box becomes one row in a Feishu Base (多维表格) table.
The table shows whether the card is free or busy, its memory and utilization,
and which processes are on it. If the card is serving a model, it also shows
the model name and the `ip:port` to call it at. The board refreshes every
minute. Your team already lives in Feishu, so nobody has to SSH into a box and
run `nvidia-smi`, or ask in the group chat, to find a free card.

![Architecture](docs/architecture.svg)

- **Nothing to host.** No server, database or web app. Each GPU box runs three
  small standard-library Python processes, and the board is a Base table you
  already have.
- **Works where cron and systemd don't.** It was built for Kubernetes GPU pods.
  A tiny supervisor keeps the processes alive, and an optional watchdog on
  your laptop redeploys after a pod restart.
- **Read-only on the GPU boxes.** The agents only run `nvidia-smi`, `ps` and
  `docker ps`, and read `/proc`. They never signal, kill or attach to a
  process, and they hold no credentials.
- **Knows what is served where.** It walks from each GPU worker process up to
  the command that launched it: `vllm serve` or SGLang (parsed fully), or
  lmdeploy, `trtllm-serve` or TGI (best effort). From that command it reads
  the model name and the endpoint people should call.
- **Offline detection.** When a box goes silent, any other box writing the
  same table marks its cards **Stale** within about 4 minutes. A box alone on
  its table can't flag itself; there, `更新时间` shows how old its rows are,
  and the watchdog brings the box back.
- **Bookings.** Type your name in `预定人` and the card shows **Reserved**
  until the window ends, unless a job is actually running on it.

> 中文文档：[README.zh-CN.md](README.zh-CN.md) · [架构说明](docs/architecture.zh-CN.md)

---

## Contents

- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [Let an AI agent set it up](#let-an-ai-agent-set-it-up)
- [The board](#the-board)
- [Configuration](#configuration)
- [Day-2 operations](#day-2-operations)
- [Security model](#security-model)
- [Limitations](#limitations)
- [Development](#development)

## How it works

Every GPU box runs the same three processes, kept alive by `supervise.sh`:

| Process | What it does | Network | Credentials |
|---|---|---|---|
| `board/node_agent.py` | every 60 s: `nvidia-smi` + a `/proc` parent walk → `nodes/<label>.json` | none | none |
| `board/cpu_agent.py` | every 60 s: load, memory, disk, I/O, workload processes → `cpu_snaps/<label>.json` | none | none |
| `board/master.py` | every 60 s: read snapshots + the table, decide each card's status, write one batch | Feishu API (HTTPS) | app secret |

Boxes of the same GPU type usually share one table (`H200`, `B200`, …). Each
box's master writes only its own rows. When another box's rows go
`STALE_SECONDS` (3 min) without an update, it marks them **Stale** on its next
cycle. So no box depends on any other box, and a dead box is flagged by its
siblings.

Deployment happens from your laptop over SSH (`deploy/deploy.sh`). It ships
the three `.py` files and two env files, never a checkout of this repo. The
optional `deploy/watchdog.sh` checks every box every 5 minutes and redeploys
any whose stack has died.

The full design, including status rules, served-model detection, the Feishu
API quota strategy and the reasons behind each choice, is in
**[docs/architecture.md](docs/architecture.md)**.

## Quick start

You need:

- **GPU boxes** (bare metal, VMs or pods) that you can `ssh` into
  non-interactively, with `nvidia-smi`, `python3` ≥ 3.7, and outbound HTTPS to
  `open.feishu.cn` (or `open.larksuite.com`). Any user works; root is not
  required.
- **A Feishu custom app** (企业自建应用) with the `bitable:app` scope, and a
  **Base** (多维表格) the app may edit. See [Feishu setup](#1-feishu-app--base).
- **A laptop** (macOS or Linux) with `bash`, `ssh`, `gzip`, and `python3` for
  the one-time table setup.

### 1. Feishu app + Base

1. In the [Feishu developer console](https://open.feishu.cn/app) (Lark:
   [open.larksuite.com/app](https://open.larksuite.com/app)), create a
   **custom app**. Under **Permissions & Scopes**, add `bitable:app`. Then
   create and publish a version so the scope takes effect.
2. From **Credentials & Basic Info**, copy the **App ID** (`cli_…`) and the
   **App Secret**.
3. Create a Base (or pick an existing one). Then, in the Base's `…` menu, use
   **Add document app** (添加文档应用) to add your app with **edit** permission.
4. Copy the Base token from the Base URL: `https://<tenant>.feishu.cn/base/`**`<BASE_TOKEN>`**`?table=…`

### 2. Configure

```bash
git clone https://github.com/mjykxz17/feishu-gpu-board && cd feishu-gpu-board
cp deploy/targets.example.conf deploy/targets.conf      # gitignored
bash deploy/deploy.sh secret       # prompts for the app secret (hidden) -> ~/.config/gpu-board/secret, mode 600
```

Edit `deploy/targets.conf`. Set `FEISHU_APP_ID` and `FEISHU_BASE_TOKEN`, then
**replace** the example `target` lines with one line per box:

```bash
#      name    ssh host           node label   GPU table id (PENDING until step 3)
target h200-a  gpu-h200-a         h200-a       PENDING
target b200-a  root@10.0.0.12     b200-a       PENDING
```

### 3. Create the tables

```bash
bash deploy/deploy.sh setup --gpu-table H200 --gpu-table B200 --cpu-table "CPU 负载"
# -> TABLE gpu H200 tblXXXX…   TABLE gpu B200 tblYYYY…   TABLE cpu CPU 负载 tblZZZZ…
```

Put the GPU table ids in the `target` lines, and put the CPU table id in
`FEISHU_CPU_TABLE_ID`. Re-running `setup` is safe: an existing table only
gets the columns it is missing.

> If your laptop can't reach the Feishu API (for example, a corporate proxy
> breaks TLS), run the setup through one of the GPU boxes:
> `deploy.sh setup --via <ssh-host> …`. That sends `setup_tables.py` to the
> box as a temp file, runs it there and deletes it.

### 4. Check, deploy, verify

```bash
bash deploy/deploy.sh all preflight   # ssh, GPUs, python, Feishu reachable (only creates ~/gpu-board)
bash deploy/deploy.sh all             # install + start on every box
bash deploy/deploy.sh all status      # processes, snapshot ages, last master log lines
```

A good deploy prints `[h200-a] OK supervisor=…` and then
`feishu: pushed nodes=1 (fresh 1) updated=0 created=8`. Open the Base; one row
per card is already there.

### 5. (Optional) keep it alive across pod restarts

```bash
bash deploy/install_watchdog.sh       # macOS: LaunchAgent, every 5 min; Linux: prints a crontab line
```

The watchdog only runs while the machine it lives on is awake. For true 24/7
coverage, install it on an always-on box that can SSH to the GPU boxes.

## Let an AI agent set it up

This repo ships an [Agent Skill](https://agentskills.io) at
[`skills/feishu-gpu-board/SKILL.md`](skills/feishu-gpu-board/SKILL.md). It
teaches a coding agent the whole job: what to ask you for, preflight checks,
table creation, deploy, verification, troubleshooting, and the safety rules
(secret handling, never touching other people's processes).

**Claude Code** (the repo is also a plugin marketplace):

```text
/plugin marketplace add mjykxz17/feishu-gpu-board
/plugin install feishu-gpu-board@feishu-gpu-board
```

Then ask: *"set up a GPU board in Feishu for my boxes gpu-1 and gpu-2"*.

**Without the plugin system**, copy the skill folder into
`~/.claude/skills/feishu-gpu-board/` (or `.claude/skills/` in a project). For
other agents (Codex, Cursor, …), point them at the SKILL.md. The skill
clones this repo itself when it isn't already present.

## The board

**GPU table**: one row per card. The master writes these columns:

| Column | Meaning |
|---|---|
| `节点` Node | node label from `targets.conf` (primary column) |
| `卡号` GPU | card index 0…N |
| `状态` Status | **Free** · **Busy** · **Reserved** · **Stale** |
| `显存` Memory | `used / total GB` |
| `利用率` Util | GPU utilization % |
| `占用进程` Processes | `process_name:pid` of every compute process on the card |
| `服务模型` Served model | model name if the card belongs to an inference server, else blank |
| `服务端点` Endpoint | `ip:port` to call it at; `127.0.0.1:port (local only)` if it listens on loopback |
| `更新时间` Updated | when the card was last actually seen. It stops moving when a box goes Stale. |

People own `负责人` (owner), `备注` (note) and the booking columns `预定人` /
`预定开始` / `预定结束`. The master never overwrites them; it only clears an
expired booking.

**Status rules** (precedence top to bottom):

1. **Stale**: the card's snapshot is older than `STALE_SECONDS` (180 s), or
   its box has stopped reporting.
2. **Busy**: any compute process is on the card, or memory used ≥
   `FREE_MEM_MB` (2048 MB).
3. **Reserved**: `预定人` is filled and now is inside the booking window.
   With no start, the master fills in `预定开始` = the moment it first saw the
   booking. With no end, the booking ends at 23:59:59 that day, in
   `BOARD_UTC_OFFSET` (default: your laptop's time zone at deploy time).
4. **Free**: otherwise. An expired booking on an idle card is cleared.

**Views** created by `setup`: `空闲 Free now`, `空转占用 Held but idle`
(Busy at 0 % util, i.e. reserved by a process but not computing) and
`离线 Offline`. Add a dashboard (仪表盘) on top if you want charts; that part is
manual.

**CPU table** (optional): one row per box with `状态` (Active / Idle /
Stale), load and 1-hour peak, memory %, disk %, I/O %, container count,
workload-process count and a plain-language summary (`· vLLM 推理服务 ×2`).

## Configuration

Everything lives in `deploy/targets.conf` (bash syntax, gitignored):

| Variable | Default | Meaning |
|---|---|---|
| `FEISHU_APP_ID` | — | app id `cli_…` |
| `FEISHU_BASE_TOKEN` | — | Base token from the URL |
| `FEISHU_BASE_URL` | `https://open.feishu.cn` | `https://open.larksuite.com` for Lark |
| `FEISHU_CPU_TABLE_ID` | empty | CPU table; empty = no CPU rows |
| `SECRET_FILE` | `~/.config/gpu-board/secret` | one line `FEISHU_APP_SECRET=…`, mode 600 |
| `REMOTE_DIR` | `gpu-board` | install dir on each box (relative = under `$HOME`) |
| `PYTHON` | `python3` | interpreter on the boxes |
| `CYCLE_SECONDS` | `60` | agent + master period |
| `CPU_PUSH_SECONDS` | `300` | CPU table period |
| `STALE_SECONDS` | `180` | snapshot age that means Stale. Keep it ≥ 2 × `CYCLE_SECONDS` + 60 |
| `FRESH` | 2 × `CYCLE_SECONDS` + 180 | `check` calls a box `down` when its snapshot or log is older than this |
| `BOARD_UTC_OFFSET` | this machine's | hours (e.g. `8`); the time zone bookings end in |
| `FREE_MEM_MB` | `2048` | below this (and no process) = Free |
| `SERVE_IP_PROBE` | `1.1.1.1` | the endpoint IP is the address of the NIC that routes here; use a fabric host on multi-homed boxes |
| `SERVE_PATTERNS` | empty | comma-separated launch-command signatures **added** to the built-in list (include the launch verb, e.g. `mycorp-serve`) |
| `WORKLOAD_PATTERNS` | empty | `ps` substrings **added** to the built-in workload list for the CPU table |
| `FEISHU_PREFER_V3`, `SWEEP_SECONDS`, `DETAIL_SECONDS`, `CPU_STALE_SECONDS` | unset | advanced master tuning, passed through when set; see the [architecture doc](docs/architecture.md#feishu-api-and-quota) |
| `SSH_OPTS` | empty | extra ssh options, e.g. `-i ~/.ssh/gpu_key -p 2222` |
| `target NAME HOST LABEL TABLE_ID` | — | one line per box |

**API budget**: each box's master makes about 2 calls per cycle: roughly 90k
calls per month at 60 s, plus about 17k for the CPU table at 300 s. That is
**per box**, so N boxes use N times as much. If your tenant's Feishu plan has a
low monthly OpenAPI quota, raise `CYCLE_SECONDS` and raise `STALE_SECONDS`
with it. If the quota still runs out, the master switches to the base v3 API
and keeps status current instead of going dark (see the
[architecture doc](docs/architecture.md#feishu-api-and-quota)).

## Day-2 operations

```bash
deploy/deploy.sh targets              # list targets
deploy/deploy.sh <t|all> status       # what's running, snapshot ages, master log
deploy/deploy.sh <t|all> check        # healthy / down / unreachable (one target: exit 0/1/2)
deploy/deploy.sh <t|all> print        # what the node agent sees right now, card by card
deploy/deploy.sh <t|all>              # redeploy (also how you upgrade)
deploy/deploy.sh <t|all> stop         # stop, keep files
deploy/deploy.sh <t|all> uninstall    # stop and remove the files it created
```

On a box, everything lives in `~/gpu-board/`: `node_agent.log`,
`cpu_agent.log`, `master.log` (trimmed automatically), `nodes/`, `cpu_snaps/`,
`agent.env`, `master.env` (0600) and `supervise.pid`. `deploy.sh <t> print`
runs the node agent once with the box's own settings and prints each card.

| Symptom | Likely cause → fix |
|---|---|
| All cards of a box show **Stale** | the box restarted (pod) or the stack died → `deploy.sh <t>` or install the watchdog |
| A box's `更新时间` is old but its cards aren't Stale | it is the only box on its table and it is down, so nobody is left to mark it → same fix |
| `token error` in `master.log` | wrong app id or secret |
| a forbidden / permission error from the API | the app isn't a collaborator on the Base with edit rights, or its scope version isn't published |
| a column missing or renamed in the UI | run `deploy.sh setup …` again to re-add it, then redeploy (the master reads the column list once at start). If `节点` or `卡号` is missing, the master stops writing and says so instead of guessing. |
| `quota dead` in the log | monthly API quota exhausted; the master is already on the v3 fallback. Raise `CYCLE_SECONDS` and `STALE_SECONDS`. |
| Endpoint shows the wrong IP | multi-homed box: set `SERVE_IP_PROBE` to an address on the network people call from |
| Model/endpoint blank on a serving card | engine launched in a form we don't recognise; add its launch command to `SERVE_PATTERNS` (see [Limitations](#limitations)) |

## Security model

- **One secret, one place per box.** The app secret is read from your local
  `SECRET_FILE` and sent inside the deploy script over SSH stdin. It never
  appears on a command line. On the box it is written only to `master.env`
  (mode 600) and read only by `master.py`. The two agents run with
  `agent.env`, which holds no secret.
- **Least privilege on the GPU boxes.** The agents are read-only: they run
  `nvidia-smi` queries, read `/proc/<pid>/cmdline` and `/proc/<pid>/status`,
  run `ps`, and run `docker ps -q`. They signal and kill nothing. The only
  processes the tooling ever stops are its own, and it checks their command
  line before stopping them.
- **No full command lines on the board.** `占用进程` shows only
  `process_name:pid`, and the served model comes from `--served-model-name` /
  `--model`. API keys passed on a command line (for example `--api-key`)
  never reach the table.
- **Scoped app.** The app only needs `bitable:app`, and it only sees Bases it
  has been explicitly added to.

## Limitations

- **NVIDIA only**: it depends on `nvidia-smi`.
- **Config-file launches**: an engine started as
  `sglang serve --config server.yaml` keeps its port and model in the YAML.
  The model stays blank, and the endpoint shows the engine's default port,
  which may be wrong. Custom Python servers that wrap vLLM aren't recognised
  at all, so both columns stay blank.
- **Other engines are best effort**: lmdeploy, `trtllm-serve` and TGI
  (`text-generation-launcher`) are recognised, and their usual model, host
  and port flags are read. vLLM and SGLang are the ones tested against real
  deployments.
- **Tables up to a few thousand rows**: reads are paginated, but every cycle
  reads the whole table, so keep one table per GPU type rather than one giant
  table.
- **Process names across containers**: on a box that hosts other containers,
  `nvidia-smi` may report PIDs from another PID namespace. The card is still
  correctly Busy, but the parent walk may not find the server.
- **The watchdog is client-side**: it only revives boxes while the machine
  running it is awake and can SSH in. For self-healing without it, deploy
  once with `REMOTE_DIR` on a persistent volume, then add this to the pod's
  startup command. It must write `supervise.pid` and use the absolute path,
  or `check`/`stop` won't recognise the stack:
  `setsid nohup "$HOME/gpu-board/supervise.sh" >/dev/null 2>&1 </dev/null & echo $! > "$HOME/gpu-board/supervise.pid"`
- **Column names are Chinese** because the board was built for Feishu teams.
  They are the contract between `master.py` and the table. Rename them in
  `FMAP` / `CPU_FMAP` (master) and in `setup_tables.py` together.

## Development

```bash
python3 -m pip install pytest
python3 -m pytest -q            # fully offline: no GPUs, no ssh, no Feishu
```

Layout:

```
board/       node_agent.py · cpu_agent.py · master.py · setup_tables.py   (stdlib only)
deploy/      deploy.sh · watchdog.sh · install_watchdog.sh · targets.example.conf
skills/      feishu-gpu-board/SKILL.md      the agent skill
docs/        architecture.md · architecture.svg  (+ .zh-CN versions in Chinese)
tests/       pytest suite
```

## License

[MIT](LICENSE)
