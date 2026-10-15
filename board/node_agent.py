#!/usr/bin/env python3
"""node_agent.py - per-node GPU snapshot writer.

Runs on every GPU node. Each cycle it asks nvidia-smi what every card is doing,
works out which inference server (if any) owns each card, and writes the result
to ONE local JSON file that the master picks up. It never talks to the board.

SAFETY (by design):
  * Runs ONLY read-only `nvidia-smi` queries and reads /proc/<pid>/{cmdline,status};
    never signals, kills, traces or attaches to any process.
  * NO network, NO credentials, NO API calls. Standard library only.
  * Writes exactly one file (atomic tmp+rename): SNAP_DIR/<NODE_LABEL>.json

Env:
  NODE_LABEL     default = hostname. The 节点 value on the board; must be unique.
  SNAP_DIR       default ~/gpu-board/nodes
  FREE_MEM_MB    default 2048  (Free = no compute process AND memory below this)
  SERVE_PATTERNS comma-separated launch-command signatures that mark an
                 inference server, ADDED to the built-in list below, so a new
                 engine can be recognised without shipping new code
  SERVE_IP_PROBE address used to pick which NIC's address to advertise in the
                 endpoint column (default 1.1.1.1 = the default-route NIC; set it
                 to a host on your cluster fabric on multi-homed machines)
  NODE_IP        hard override for the advertised address

Modes:
  --print / --json   gather + print, do not write the snapshot file
  --loop SECONDS     repeat every SECONDS (default one-shot)
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time

NVIDIA_GPU = ["nvidia-smi",
              "--query-gpu=index,uuid,utilization.gpu,memory.used,memory.total",
              "--format=csv,noheader,nounits"]
NVIDIA_PROC = ["nvidia-smi",
               "--query-compute-apps=gpu_uuid,pid,used_memory,process_name",
               "--format=csv,noheader,nounits"]


def _int(s):
    s = s.strip()
    return 0 if (not s or s == "N/A" or s.startswith("[")) else int(s)


# --- served-model detection -------------------------------------------------
# nvidia-smi reports the WORKER pid, whose cmdline is a proc-title vLLM sets
# ("VLLM::EngineCore", "VLLM::Worker_TP0_EP0") and which carries no model or
# port. The real `vllm serve` / api_server command is 1-3 hops up the parent
# chain, so every lookup starts at the worker and walks up.

# Every pattern must match a LAUNCH COMMAND, never a proc-title. A bare
# "sglang" would match "sglang::scheduler_DP0_TP0_EP0" at hop 0, and an RL
# rollout engine (e.g. Ray GRPO driving SGLang) would then be advertised as an
# endpoint that serves nobody. Keep the verb in the pattern.
BUILTIN_SERVE_PATTERNS = ("vllm serve", "vllm.entrypoints",
                          "sglang serve", "sglang.launch_server",
                          "lmdeploy serve", "trtllm-serve", "text-generation-launcher")
SERVE_PATTERNS = BUILTIN_SERVE_PATTERNS + tuple(
    p.strip() for p in os.environ.get("SERVE_PATTERNS", "").split(",") if p.strip())
MAX_PARENT_HOPS = 6
# Engine defaults, used only when the command line omits them:
# (signatures, default port, default bind host).
_ENGINE_DEFAULTS = ((("sglang serve", "sglang.launch_server"), 30000, ""),
                    (("vllm serve", "vllm.entrypoints"), 8000, ""),
                    (("lmdeploy serve",), 23333, ""),
                    (("trtllm-serve",), 8000, "localhost"),
                    (("text-generation-launcher",), 3000, ""))
# Console scripts that take the model as a positional argument, possibly after
# a subcommand: `vllm serve M`, `lmdeploy serve api_server M`, `trtllm-serve [serve] M`.
_SERVE_CLIS = ("vllm", "sglang", "lmdeploy", "trtllm-serve")
_SUBCOMMANDS = ("serve", "api_server")


def _flag(argv, *names):
    """Value of `--flag value` or `--flag=value`; None if absent or valueless."""
    for i, arg in enumerate(argv):
        for name in names:
            if arg == name:
                nxt = argv[i + 1] if i + 1 < len(argv) else ""
                return nxt if nxt and not nxt.startswith("-") else None
            if arg.startswith(name + "="):
                return arg.split("=", 1)[1] or None
    return None


def _pretty_model(value):
    """`/data/models/qwen3_8b` -> `qwen3_8b`, but `Qwen/Qwen3-8B` is a HF repo
    id, not a path, so it stays whole."""
    if not value:
        return ""
    if value.startswith(("/", "~", ".")):
        return os.path.basename(value.rstrip("/")) or value
    return value


def _positional_model(argv):
    """The model given as a positional argument after the engine's console
    script and its subcommands, or None."""
    for i, arg in enumerate(argv):
        if os.path.basename(arg).startswith(_SERVE_CLIS):
            rest = argv[i + 1:]
            while rest and rest[0] in _SUBCOMMANDS:
                rest = rest[1:]
            return rest[0] if rest and not rest[0].startswith("-") else None
    return None


def parse_server(argv):
    """argv of an inference server -> {model, host, port}; None if it isn't one.

    vLLM and SGLang are parsed fully; lmdeploy, TensorRT-LLM (trtllm-serve)
    and TGI (text-generation-launcher) on a best-effort basis."""
    if not argv:
        return None
    line = " ".join(argv)
    if not any(p in line for p in SERVE_PATTERNS):
        return None
    model = (_flag(argv, "--served-model-name", "--model-name")
             or _flag(argv, "--model", "--model-path", "--model-id")
             or _positional_model(argv))
    defaults = next(((port, host) for pats, port, host in _ENGINE_DEFAULTS
                     if any(p in line for p in pats)), (8000, ""))
    port = _flag(argv, "--port", "--server-port")
    port = int(port) if port and port.isdigit() else defaults[0]
    host = _flag(argv, "--host", "--server-name", "--hostname") or defaults[1]
    return {"model": _pretty_model(model), "host": host, "port": port}


def read_proc(pid, root="/proc"):
    """(argv, ppid) for a pid, or (None, None) if it is gone or unreadable.

    Both files are world-readable; nothing here signals, traces, or opens the
    process itself, so the agent stays as read-only as its nvidia-smi calls."""
    try:
        with open(f"{root}/{pid}/cmdline", "rb") as fh:
            argv = [a for a in fh.read().decode("utf-8", "replace").split("\0") if a]
        with open(f"{root}/{pid}/status") as fh:
            m = re.search(r"^PPid:\s*(\d+)", fh.read(), re.M)
        return argv, (int(m.group(1)) if m else None)
    except (OSError, ValueError):
        return None, None


def find_server(pid, reader=read_proc, max_hops=MAX_PARENT_HOPS):
    """Walk up from a GPU worker pid to the argv of the server that owns it."""
    seen = set()
    for _ in range(max_hops):
        if pid is None or pid <= 1 or pid in seen:
            return None
        seen.add(pid)
        argv, ppid = reader(pid)
        if argv is None:
            return None
        if parse_server(argv):
            return argv
        pid = ppid
    return None


def node_ip(probe=None):
    """The address on the NIC that routes towards `probe`. A UDP connect()
    picks a source address from the routing table without sending a packet."""
    if os.environ.get("NODE_IP"):
        return os.environ["NODE_IP"]
    probe = probe or os.environ.get("SERVE_IP_PROBE", "1.1.1.1")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe, 9))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return ""


def format_endpoint(server, ip):
    """A dial-able `ip:port`, or a flagged loopback one that isn't."""
    host, port = server.get("host", ""), server["port"]
    if host in ("127.0.0.1", "localhost", "::1"):
        return f"{host}:{port} (local only)"
    if host and host not in ("0.0.0.0", "::", ""):
        return f"{host}:{port}"
    return f"{ip}:{port}"


def parse(gpu_text, proc_text, free_mem_mb, resolver=None, node_ip=""):
    """Pure parse of nvidia-smi output -> list of per-GPU dicts. Read-only.

    resolver: optional (pid) -> (argv, ppid) reader. Without it the model and
    endpoint columns come back blank."""
    uuid_to_index = {}
    gpus = {}
    for line in gpu_text.strip().splitlines():
        if not line.strip():
            continue
        idx_s, uuid, util, used, total = [c.strip() for c in line.split(",", 4)]
        idx = int(idx_s)
        uuid_to_index[uuid] = idx
        gpus[idx] = {"util": _int(util), "used": _int(used),
                     "total": _int(total), "procs": [], "pids": []}
    for line in proc_text.strip().splitlines():
        if not line.strip():
            continue
        parts = [c.strip() for c in line.split(",", 3)]
        if len(parts) < 4:
            continue
        uuid, pid, _pmem, pname = parts
        idx = uuid_to_index.get(uuid)
        if idx is not None:
            gpus[idx]["procs"].append(f"{pname}:{pid}")
            gpus[idx]["pids"].append(int(pid))
    # One server owns many workers, so the same parent is walked repeatedly.
    cache = {}
    rows = []
    for idx in sorted(gpus):
        g = gpus[idx]
        models, endpoints = [], []
        for pid in (g["pids"] if resolver else []):
            if pid not in cache:
                argv = find_server(pid, resolver)
                cache[pid] = parse_server(argv) if argv else None
            server = cache[pid]
            if not server:
                continue
            endpoint = format_endpoint(server, node_ip)
            if endpoint not in endpoints:       # TP workers all share one server
                models.append(server["model"])
                endpoints.append(endpoint)
        busy = bool(g["procs"]) or g["used"] >= free_mem_mb
        rows.append({"gpu": idx, "status": "Busy" if busy else "Free",
                     "mem_used_mb": g["used"], "mem_total_mb": g["total"],
                     "util": g["util"], "user_proc": ", ".join(g["procs"]),
                     "model": ", ".join(models), "endpoint": ", ".join(endpoints)})
    return rows


def gather(free_mem_mb):
    gpu = subprocess.run(NVIDIA_GPU, capture_output=True, text=True, timeout=15)
    if gpu.returncode != 0:
        raise RuntimeError("nvidia-smi failed: " + gpu.stderr.strip())
    proc = subprocess.run(NVIDIA_PROC, capture_output=True, text=True, timeout=15)
    return parse(gpu.stdout, proc.stdout if proc.returncode == 0 else "", free_mem_mb,
                 resolver=read_proc, node_ip=node_ip())


def write_snapshot(snap_dir, node_label, rows):
    payload = {"node": node_label, "ts": int(time.time()), "rows": rows}
    os.makedirs(snap_dir, exist_ok=True)
    tmp = os.path.join(snap_dir, f".{node_label}.json.tmp")
    dst = os.path.join(snap_dir, f"{node_label}.json")
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, dst)  # atomic on the same filesystem; readers never see a partial file
    return dst


def main(argv=None):
    p = argparse.ArgumentParser(description="per-node GPU snapshot writer (no network)")
    p.add_argument("--print", dest="do_print", action="store_true")
    p.add_argument("--json", dest="do_json", action="store_true")
    p.add_argument("--loop", type=int, default=0, help="repeat every N seconds")
    args = p.parse_args(argv)

    free_mem_mb = int(os.environ.get("FREE_MEM_MB", "2048"))
    node_label = os.environ.get("NODE_LABEL") or socket.gethostname()
    snap_dir = os.path.expanduser(os.environ.get("SNAP_DIR", "~/gpu-board/nodes"))

    def once():
        rows = gather(free_mem_mb)
        if args.do_json:
            print(json.dumps({"node": node_label, "rows": rows}))
        elif args.do_print:
            for r in rows:
                served = f"  [{r['model']} @ {r['endpoint']}]" if r["model"] else ""
                print(f"GPU{r['gpu']} {r['status']} {r['mem_used_mb']}MB "
                      f"util={r['util']}% {r['user_proc']}{served}")
        else:
            dst = write_snapshot(snap_dir, node_label, rows)
            print(f"{node_label}: wrote {dst}", file=sys.stderr)

    if args.loop > 0:
        while True:
            try:
                once()
            except Exception as e:  # one bad cycle never stops the loop
                print(f"{node_label}: cycle error: {e}", file=sys.stderr)
            time.sleep(args.loop)
    else:
        once()


if __name__ == "__main__":
    main()
