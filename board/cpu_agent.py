#!/usr/bin/env python3
"""cpu_agent.py - per-host CPU / memory / disk / workload snapshot writer.

Runs next to node_agent.py on every box. Collects load (now and 1-hour peak),
busiest-disk I/O %, memory %, disk %, docker container count and the
processes that look like GPU workloads, and writes ONE local JSON file the
master turns into the host's row in the CPU table.

SAFETY (by design): reads /proc and `ps` / `docker ps` output only; NO network,
NO credentials. Standard library only.

Env:
  NODE_LABEL         default = hostname. The 主机 value on the board.
  SNAP_OUT_DIR       default ~/gpu-board/cpu_snaps
  WORKLOAD_PATTERNS  comma-separated `ps` substrings that count as a workload,
                     ADDED to the built-in list below
Modes:
  --print   collect and print, do not write the snapshot
  --loop N  repeat every N seconds
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time

SNAP_OUT_DIR = os.path.expanduser(os.environ.get("SNAP_OUT_DIR", "~/gpu-board/cpu_snaps"))

BUILTIN_WORKLOAD_PATTERNS = (
    "vllm", "VLLM::", "sglang", "torchrun", "deepspeed",
    "accelerate launch", "llamafactory", "train.py", "finetune",
    # NOT bare "verl": node_exporter's fs-exclude args contain "overlay",
    # which contains "verl", so every box would false-positive as RL training.
    "-m verl", "/verl/")
WORKLOAD_PATTERNS = BUILTIN_WORKLOAD_PATTERNS + tuple(
    p.strip() for p in os.environ.get("WORKLOAD_PATTERNS", "").split(",") if p.strip())

# Plain-language labels for the 任务详情 (workload detail) column.
PATTERN_LABELS = {
    "vllm": "vLLM 推理服务", "VLLM::": "vLLM worker", "sglang": "SGLang 推理服务",
    "torchrun": "分布式训练 (torchrun)", "deepspeed": "DeepSpeed 训练",
    "accelerate launch": "Accelerate 训练", "llamafactory": "LLaMA-Factory 训练",
    "train.py": "训练脚本", "finetune": "微调",
    "-m verl": "verl RL 训练", "/verl/": "verl RL 训练",
}


def container_count():
    """docker ps count; falls back to the rootless socket, where the default
    socket shows nothing. 0 when docker is absent (most GPU pods)."""
    def count(env=None):
        try:
            r = subprocess.run(["docker", "ps", "-q"], capture_output=True, text=True,
                               timeout=15, env=env)
        except (OSError, subprocess.SubprocessError):
            return None  # docker not installed or not responding
        if r.returncode != 0:
            return None
        return len([l for l in r.stdout.splitlines() if l.strip()])
    n = count()
    if not n:
        sock = f"/run/user/{os.getuid()}/docker.sock"
        if os.path.exists(sock):
            n2 = count(dict(os.environ, DOCKER_HOST=f"unix://{sock}"))
            if n2:
                return n2
    return n or 0


def summarize(procs, patterns=None):
    """One line per matched pattern label, e.g. "· vLLM 推理服务 ×2"."""
    patterns = patterns or WORKLOAD_PATTERNS
    counted = {}
    for _, a in procs:
        for p in patterns:
            if p in a:
                label = PATTERN_LABELS.get(p, p)
                counted[label] = counted.get(label, 0) + 1
                break
    lines = [f"· {k}" + (f" ×{v}" if v > 1 else "") for k, v in counted.items()]
    return "\n".join(lines)[:400]


def match_workloads(ps_text, patterns=None):
    """[(pid, args)] of `ps -eo pid,args` lines matching a workload pattern."""
    patterns = patterns or WORKLOAD_PATTERNS
    procs = []
    for line in ps_text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, args = parts
        if any(p in args for p in patterns) and "cpu_agent.py" not in args:
            procs.append((pid, args))
    return procs


def workload_processes():
    """(count, human summary) of workload-looking processes from ps."""
    r = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True, timeout=15)
    procs = match_workloads(r.stdout)
    return len(procs), summarize(procs)


# --- bottleneck metrics ---

_LOAD_HIST = []          # (ts, load1) samples, pruned to the last hour
_PREV_DISK = {}          # {"ts": epoch, "ticks": {dev: io_ticks_ms}}


def parse_diskstats(text):
    """/proc/diskstats -> {whole-device: io_ticks_ms} (nvme*n*/sd*/vd* only,
    partitions excluded so one busy partition isn't double counted)."""
    out = {}
    for line in text.splitlines():
        c = line.split()
        if len(c) < 14:
            continue
        dev = c[2]
        if re.fullmatch(r"nvme\d+n\d+|sd[a-z]+|vd[a-z]+", dev):
            out[dev] = int(c[12])  # ms spent doing I/O
    return out


def io_util_pct():
    """Busiest disk's busy-% since the previous call (first call primes and
    returns None)."""
    now = time.time()
    try:
        with open("/proc/diskstats") as fh:
            ticks = parse_diskstats(fh.read())
    except OSError:
        return None
    prev, _PREV_DISK["ts"], _PREV_DISK["ticks"] = dict(_PREV_DISK), now, ticks
    if not prev.get("ticks"):
        return None
    elapsed_ms = (now - prev["ts"]) * 1000
    if elapsed_ms <= 0:
        return None
    best = 0
    for dev, t in ticks.items():
        if dev in prev["ticks"]:
            best = max(best, (t - prev["ticks"][dev]) / elapsed_ms * 100)
    return round(min(best, 100), 1)


def load_peak_1h(load1, now):
    _LOAD_HIST.append((now, load1))
    _LOAD_HIST[:] = [(t, v) for t, v in _LOAD_HIST if now - t <= 3600]
    return round(max(v for _, v in _LOAD_HIST), 2)


def mem_pct():
    info = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            k, _, rest = line.partition(":")
            info[k] = int(rest.split()[0])
    total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
    return round((total - avail) / total * 100, 1) if total else None


def disk_pct():
    worst = 0
    for path in ("/", "/var/lib/docker", "/home"):
        try:
            st = os.statvfs(path)
        except OSError:
            continue
        if st.f_blocks:
            worst = max(worst, (1 - st.f_bavail / st.f_blocks) * 100)
    return round(worst, 1)


def collect():
    procs, detail = workload_processes()
    now = int(time.time())
    load1 = round(os.getloadavg()[0], 2)
    return {
        # NODE_LABEL wins over hostname: the board matches rows by this string.
        "host": os.environ.get("NODE_LABEL") or socket.gethostname(),
        "ts": now,
        "load1": load1,
        "load_peak_1h": load_peak_1h(load1, now),
        "io_util": io_util_pct(),
        "mem_pct": mem_pct(),
        "disk_pct": disk_pct(),
        "containers": container_count(),
        "procs": procs,
        "detail": detail,
    }


def write_snap(snap, out_dir=None):
    """Atomic tmp+rename so the master never reads a partial file."""
    out_dir = out_dir or SNAP_OUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    snap = dict(snap, recv_ts=int(time.time()))  # master ages rows off recv_ts
    tmp = os.path.join(out_dir, f".{snap['host']}.json.tmp")
    dst = os.path.join(out_dir, f"{snap['host']}.json")
    with open(tmp, "w") as fh:
        json.dump(snap, fh, ensure_ascii=False)
    os.replace(tmp, dst)
    return dst


def main(argv=None):
    p = argparse.ArgumentParser(description="per-host CPU/workload snapshot writer")
    p.add_argument("--print", dest="do_print", action="store_true")
    p.add_argument("--loop", type=int, default=0)
    args = p.parse_args(argv)

    def once():
        snap = collect()
        if args.do_print:
            print(json.dumps(snap, ensure_ascii=False))
        else:
            dst = write_snap(snap)
            print(f"{snap['host']}: wrote {dst} load={snap['load1']} "
                  f"containers={snap['containers']} procs={snap['procs']}", file=sys.stderr)

    if args.loop > 0:
        while True:
            try:
                once()
            except Exception as e:  # one bad cycle never stops the loop
                print(f"cycle error: {e}", file=sys.stderr)
            time.sleep(args.loop)
    else:
        once()


if __name__ == "__main__":
    main()
