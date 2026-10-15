#!/usr/bin/env python3
"""master.py - the only process that talks to the Feishu / Lark Base API.

Each cycle (default every 60 s) it:
  1. reads the GPU snapshot files node_agent.py wrote into SNAP_DIR
  2. reads the GPU table once (records/search) for record_ids + current values
  3. decides each card's status: Busy (real use) > Reserved (active booking) > Free;
     a node whose snapshot is older than STALE_SECONDS -> Stale
  4. pushes ONE batch_update (plus a batch_create for cards the table lacks)
  5. every CPU_PUSH_SECONDS, upserts this host's row in the CPU table (optional)

Several masters may share one table (typically one master per box and one table
per GPU type). Each writes only the nodes it has snapshots for, and marks
somebody else's rows Stale only once their 更新时间 is older than STALE_SECONDS,
i.e. once nobody is updating them any more. So a box never depends on another
box being up, and a box that dies is flagged by its siblings within minutes.

API cost: 1 read + 1-2 writes per cycle, ~90k calls/month at a 60 s cycle.
Feishu meters its monthly OpenAPI quota per method; if the bitable v1 write
quota is exhausted (error 99991403), the master degrades to the base v3 API
(push_v3_grouped / push_v3_details) instead of going dark.

Env:
  FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_BASE_TOKEN / FEISHU_TABLE_ID  (required)
  FEISHU_BASE_URL      default https://open.feishu.cn  (Lark: https://open.larksuite.com)
  FEISHU_CPU_TABLE_ID  optional - enables the CPU-load table
  SNAP_DIR             default ~/gpu-board/nodes
  CPU_SNAP_DIR         default ~/gpu-board/cpu_snaps
  STALE_SECONDS        default 180
  BOARD_UTC_OFFSET     hours, e.g. 8; the time zone bookings end in ("end of
                       day"). Default: the box's local time (often UTC in pods)
  FEISHU_PREFER_V3     default 0; 1 = v3 status writes every cycle and the v1
                       full-detail write only every DETAIL_SECONDS
  SWEEP_SECONDS, DETAIL_SECONDS, DETAIL_PATCH_LIMIT   v3 degraded-mode tuning
  CPU_PUSH_SECONDS (300), CPU_STALE_SECONDS (900)     CPU table cadence
Modes:
  --dry-run          read + plan only, print the planned writes
  --loop SECONDS     repeat every SECONDS (default one-shot)
"""
import argparse
import datetime
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request

# Logical field keys -> column names. Column names ARE the contract with the
# Base: rename a column here AND on the table together (setup_tables.py creates
# them from this map).
FMAP = {
    "node": "节点", "gpu": "卡号", "status": "状态",
    "mem": "显存", "util": "利用率", "proc": "占用进程",
    "updated": "更新时间", "res_by": "预定人",
    "res_from": "预定开始", "res_until": "预定结束",
    "model": "服务模型", "endpoint": "服务端点",
}
# 备注 (note) and 负责人 (owner) are people's columns - the master never writes them.

BATCH_LIMIT = 500  # bitable batch_create / batch_update per-call record limit


def backend_from_env():
    """The board this master writes, from the FEISHU_* environment."""
    return {
        "label": "feishu", "fmap": FMAP,
        # 0 = v1 full-fidelity batch every cycle, v3 only while the v1 quota
        # is dead. 1 = v3 is the primary channel, v1 a low-rate detail writer.
        "prefer_v3": os.environ.get("FEISHU_PREFER_V3", "0") == "1",
        "base_url": os.environ.get("FEISHU_BASE_URL", "https://open.feishu.cn").rstrip("/"),
        "app_id": os.environ["FEISHU_APP_ID"],
        "app_secret": os.environ.get("FEISHU_APP_SECRET", ""),
        "base_token": os.environ["FEISHU_BASE_TOKEN"],
        "table_id": os.environ["FEISHU_TABLE_ID"],
    }


def _api(method, url, token=None, body=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


_TOKEN = {}


def get_token(base_url, app_id, app_secret):
    now = time.time()
    c = _TOKEN.get(app_id)
    if c and now < c["exp"]:
        return c["val"]
    d = _api("POST", base_url + "/open-apis/auth/v3/tenant_access_token/internal",
             body={"app_id": app_id, "app_secret": app_secret})
    if d.get("code") != 0:
        raise RuntimeError(f"token error: code={d.get('code')} msg={d.get('msg')}")
    tok = d["tenant_access_token"]
    _TOKEN[app_id] = {"val": tok, "exp": now + min(int(d.get("expire", 7200)), 7200) - 300}
    return tok


def _text(v):
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return "".join(s.get("text", "") if isinstance(s, dict) else str(s) for s in v)
    if isinstance(v, dict):
        return v.get("text", "")
    return "" if v is None else str(v)


def _num(v):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, list) and v:
        return _num(v[0])
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0


def _ts(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, list) and v:
        return _ts(v[0])
    if isinstance(v, str):  # base v3 endpoints return datetimes as strings
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
            try:
                return int(datetime.datetime.strptime(v, fmt).timestamp() * 1000)
            except ValueError:
                continue
    return None


def _board_tz():
    """Fixed UTC offset from BOARD_UTC_OFFSET (hours), or None = box local time."""
    off = os.environ.get("BOARD_UTC_OFFSET", "").strip()
    return datetime.timezone(datetime.timedelta(hours=float(off))) if off else None


BOARD_TZ = _board_tz()


def _end_of_day_ms(ms, tz=None):
    """23:59:59 of the day `ms` falls on, in BOARD_TZ (or box local time)."""
    d = datetime.datetime.fromtimestamp(ms / 1000, tz if tz is not None else BOARD_TZ)
    return int(d.replace(hour=23, minute=59, second=59, microsecond=0).timestamp() * 1000)


def reservation_window(reserved_by, reserved_from, reserved_until, now_ms):
    if not reserved_by or not reserved_by.strip():
        return None
    start = reserved_from if reserved_from is not None else now_ms
    end = reserved_until if reserved_until is not None else _end_of_day_ms(start)
    return (start, end)


def decide_status(base_status, res, now_ms):
    """(status, clear). Precedence: Busy > Reserved (in window) > Free.
    Stale passes through untouched and never clears a reservation."""
    if base_status == "Stale":
        return ("Stale", False)
    if base_status == "Busy":
        return ("Busy", False)
    win = reservation_window(res.get("reserved_by", ""), res.get("reserved_from"),
                             res.get("reserved_until"), now_ms)
    if win is None:
        return ("Free", False)
    start, end = win
    if start <= now_ms <= end:
        return ("Reserved", False)
    if now_ms > end:
        return ("Free", True)   # expired + idle -> release
    return ("Free", False)


def load_snapshots(snap_dir, stale_seconds, now_s):
    out = {}
    for path in sorted(glob.glob(os.path.join(snap_dir, "*.json"))):
        try:
            with open(path) as fh:
                snap = json.load(fh)
        except (OSError, ValueError) as e:
            print(f"snapshot unreadable, skipping: {path}: {e}", file=sys.stderr)
            continue
        node = snap.get("node") or os.path.splitext(os.path.basename(path))[0]
        out[node] = {"fresh": (now_s - int(snap.get("ts", 0))) <= stale_seconds,
                     "rows": snap.get("rows", [])}
    return out


def _blank(v):
    if v is None or v is False:
        return True
    if isinstance(v, (int, float)):
        return False
    return not _text(v).strip()


def _card_from_fields(fm, f, record_id, out, junk=None):
    """Index one board row by (node, gpu).

    A row with no 节点 or no 卡号 can never match a card. If it is also
    completely empty - what a stray row added by hand in the Feishu UI looks
    like - collect it in `junk` for the caller to delete. A row that holds
    anything at all is left alone: deleting it could destroy someone's notes."""
    node = _text(f.get(fm["node"]))
    if not node or f.get(fm["gpu"]) is None:
        if junk is not None and record_id and all(_blank(v) for v in f.values()):
            junk.append(record_id)
        return
    out[(node, int(_num(f.get(fm["gpu"]))))] = {
        "record_id": record_id,
        "reserved_by": _text(f.get(fm["res_by"])),
        "reserved_from": _ts(f.get(fm["res_from"])),
        "reserved_until": _ts(f.get(fm["res_until"])),
        "cur_status": _text(f.get(fm["status"])),
        "cur_proc": _text(f.get(fm["proc"])),
        "cur_model": _text(f.get(fm["model"])),
        "cur_endpoint": _text(f.get(fm["endpoint"])),
        "cur_updated": _ts(f.get(fm["updated"])),
    }


def _v1_read_all(b, token, table_id):
    """All rows of a table via bitable v1, following page_token. Returns the
    item list, or None when the v1 read quota is exhausted (99991403) so the
    caller can fall back to v3. Tries records/search first, then the list GET
    (Feishu meters each method's monthly quota separately)."""
    base = (f"{b['base_url']}/open-apis/bitable/v1/apps/{b['base_token']}"
            f"/tables/{table_id}/records")
    for method, url in (("POST", base + "/search"), ("GET", base)):
        items, page = [], ""
        while True:
            q = "?page_size=500" + (f"&page_token={page}" if page else "")
            d = _api(method, url + q, token=token, body={} if method == "POST" else None)
            if d.get("code") != 0:
                break
            data = d.get("data") or {}
            items += data.get("items") or []
            if not data.get("has_more"):
                return items
            page = data.get("page_token", "")
        if "99991403" not in json.dumps(d):
            raise RuntimeError(f"{b['label']} read error: " + json.dumps(d))
    return None


def board_cards(b, token, junk=None):
    """(node, gpu) -> card dict for every row on board b.

    junk: optional list, filled with the record_ids of rows that carry neither
    a node nor a gpu (see _card_from_fields).

    Feishu meters its monthly OpenAPI quota PER METHOD, so when one bitable v1
    bucket is exhausted the others may still work: v1 search first
    (full-featured), then the v1 list GET, then the base v3 columnar list."""
    fm = b["fmap"]
    out = {}
    if not b.get("prefer_v3"):
        items = _v1_read_all(b, token, b["table_id"])
        if items is not None:
            for item in items:
                _card_from_fields(fm, item.get("fields", {}), item.get("record_id"),
                                  out, junk)
            return out
    # v3 fallback: columnar response {fields: [...], data: [[...]], record_id_list: [...]}
    # v3 paginates with limit/offset (limit max 200; the v1-style page_size is
    # IGNORED and silently returns 20 rows). Paginate fully: a partial read here
    # would misplan rows as batch_create duplicates once the v1 quota returns.
    offset = 0
    while True:
        v3 = (f"{b['base_url']}/open-apis/base/v3/bases/{b['base_token']}"
              f"/tables/{b['table_id']}/records?limit=200&offset={offset}")
        d = _api("GET", v3, token=token)
        if d.get("code") != 0:
            raise RuntimeError(f"{b['label']} v3 read error: " + json.dumps(d))
        dd = d.get("data", {})
        cols = dd.get("fields", [])
        rows = dd.get("data", [])
        for rid, row in zip(dd.get("record_id_list", []), rows):
            _card_from_fields(fm, dict(zip(cols, row)), rid, out, junk)
        if not dd.get("has_more") or not rows:
            return out
        offset += len(rows)


_FIELDS = {}  # (base_token, table_id) -> frozenset of column names, or None


def board_field_names(b, token):
    """The column names a board has, read once per process and cached.

    One extra call per master start, which is why it is safe to keep: it lets
    plan_writes drop columns a board does not have (e.g. no reservation
    columns) instead of the whole write failing. Returns None if the read
    fails - callers then write every column."""
    key = (b["base_token"], b["table_id"])
    if key in _FIELDS:
        return _FIELDS[key]
    url = (f"{b['base_url']}/open-apis/bitable/v1/apps/{b['base_token']}"
           f"/tables/{b['table_id']}/fields?page_size=200")
    names = None
    try:
        d = _api("GET", url, token=token)
        if d.get("code") == 0:
            names = frozenset(it.get("field_name", "")
                              for it in d.get("data", {}).get("items", []))
    except Exception as e:
        print(f"{b['label']}: field list unreadable ({e}); writing all columns",
              file=sys.stderr)
    _FIELDS[key] = names
    return names


def delete_records(b, token, record_ids):
    """Remove rows from a board. Used only for rows the master itself proved
    are unusable (no node, no gpu) - never for anything a person might be
    using. Returns the number deleted."""
    if not record_ids:
        return 0
    url = (f"{b['base_url']}/open-apis/bitable/v1/apps/{b['base_token']}"
           f"/tables/{b['table_id']}/records/batch_delete")
    done = 0
    for i in range(0, len(record_ids), BATCH_LIMIT):
        chunk = record_ids[i:i + BATCH_LIMIT]
        d = _api("POST", url, token=token, body={"records": chunk})
        if d.get("code") != 0:
            raise RuntimeError(f"{b['label']} batch_delete error: " + json.dumps(d))
        done += len(chunk)
    return done


def plan_writes(snaps, cards, fm, now_ms, known_fields=None, stale_after_ms=None):
    """Planned (updates, creates) for ONE board, in bitable batch_* body shape.

    cards: (node, gpu) -> card dict read from the board; its reservation
    columns drive decide_status.
    known_fields: the column names the board actually has. Writing a column a
    board lacks fails the WHOLE batch (FieldNameNotFound), so a board without
    the optional columns keeps working, just without them. None = the board's
    columns are unknown, so write everything.
    stale_after_ms: a row whose node has no snapshot here may belong to another
    master sharing this table. Leave it alone while its 更新时间 is younger than
    this; None = mark it Stale straight away."""
    updates, creates = [], []
    seen = set()

    def keep(f):
        if known_fields is None:
            return f
        return {k: v for k, v in f.items() if k in known_fields}
    for node, snap in snaps.items():
        for row in snap["rows"]:
            key = (node, row["gpu"])
            seen.add(key)
            card = cards.get(key) or {}
            base = row["status"] if snap["fresh"] else "Stale"
            status, clear = decide_status(base, card, now_ms)
            f = {
                fm["node"]: node,
                fm["gpu"]: row["gpu"],
                fm["status"]: status,
                fm["mem"]: f"{row['mem_used_mb'] / 1024:.1f} / {row['mem_total_mb'] / 1024:.0f} GB",
                fm["util"]: row["util"],
                fm["proc"]: row["user_proc"],
                # Written even when empty: a card that stops serving has to
                # clear the old model/endpoint, not keep showing it. .get()
                # because an older agent's snapshot may lack these keys.
                fm["model"]: row.get("model", ""),
                fm["endpoint"]: row.get("endpoint", ""),
            }
            if snap["fresh"]:
                # 更新时间 = when the card was last actually seen, so a stale
                # snapshot leaves it alone.
                f[fm["updated"]] = now_ms
            if clear:
                f[fm["res_by"]] = ""
                f[fm["res_from"]] = None
                f[fm["res_until"]] = None
            elif ((card.get("reserved_by") or "").strip() and card.get("reserved_from") is None
                  and base != "Stale"):
                # A name-only booking: pin its start to now, so it ends tonight
                # instead of re-starting (and never ending) every cycle.
                f[fm["res_from"]] = now_ms
            rid = card.get("record_id")
            if rid:
                updates.append({"record_id": rid, "fields": keep(f)})
            else:
                creates.append({"fields": keep(f)})
    # Board rows whose node has no snapshot here -> Stale once nobody else is
    # updating them. Status only: 更新时间 keeps saying when the node was last
    # actually seen, instead of looking freshly updated while it is dark.
    for key, card in cards.items():
        if key in seen or card["cur_status"] == "Stale":
            continue
        last = card.get("cur_updated")
        if stale_after_ms is not None and last is not None and now_ms - last < stale_after_ms:
            continue
        updates.append({"record_id": card["record_id"],
                        "fields": keep({fm["status"]: "Stale"})})
    return updates, creates


def push(b, token, updates, creates):
    """Full-fidelity v1 push. Raises QuotaDead on 99991403 so the caller can
    degrade to the v3 grouped mode instead."""
    recs = (f"{b['base_url']}/open-apis/bitable/v1/apps/{b['base_token']}"
            f"/tables/{b['table_id']}/records")
    for i in range(0, len(updates), BATCH_LIMIT):
        d = _api("POST", recs + "/batch_update", token=token,
                 body={"records": updates[i:i + BATCH_LIMIT]})
        if d.get("code") != 0:
            if "99991403" in json.dumps(d):
                raise QuotaDead()
            raise RuntimeError(f"{b['label']} batch_update error: " + json.dumps(d))
    for i in range(0, len(creates), BATCH_LIMIT):
        d = _api("POST", recs + "/batch_create", token=token,
                 body={"records": creates[i:i + BATCH_LIMIT]})
        if d.get("code") != 0:
            raise RuntimeError(f"{b['label']} batch_create error: " + json.dumps(d))


class QuotaDead(Exception):
    """The v1 monthly method quota is exhausted (resets on the 1st)."""


V3_BATCH_LIMIT = 200          # v3 batch_update record_id_list per-call limit
# Degraded mode: full status sweep this often. 60 = every cycle (board timestamps
# always fresh, ~170k v3 calls/month); 1800 = liveness-only (~6k/month).
# Every cycle by default: masters sharing a table judge each other by
# 更新时间, so it must stay fresh in degraded mode too.
SWEEP_SECONDS = int(os.environ.get("SWEEP_SECONDS", "60"))
_LAST_SWEEP = {}              # backend label -> epoch seconds of last full sweep


def push_v3_grouped(b, token, updates, cards, now_s):
    """Degraded push while the v1 quota is dead: the base v3 batch_update only
    supports ONE patch applied to many record_ids, so per-card columns
    (显存/利用率/占用进程) cannot be written - but 状态 + 更新时间 are identical
    for every card in the same state, so they group into a handful of calls.
    To keep even that volume tiny, only status changes / reservation clears are
    written each cycle, plus a full sweep every SWEEP_SECONDS as liveness proof.
    Returns (groups_written, records_written)."""
    fm = b["fmap"]
    sweep = (now_s - _LAST_SWEEP.get(b["label"], 0)) >= SWEEP_SECONDS
    cur_by_rid = {c["record_id"]: c["cur_status"] for c in cards.values()}
    now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_s))
    groups = {}  # (status, clear, stamp) -> [record_ids]
    for u in updates:
        status = u["fields"][fm["status"]]
        clear = fm["res_by"] in u["fields"] and u["fields"][fm["res_by"]] == ""
        stamp = fm["updated"] in u["fields"]   # Stale marks keep the old 更新时间
        if not (sweep or clear or cur_by_rid.get(u["record_id"]) != status):
            continue
        groups.setdefault((status, clear, stamp), []).append(u["record_id"])
    url = (f"{b['base_url']}/open-apis/base/v3/bases/{b['base_token']}"
           f"/tables/{b['table_id']}/records/batch_update")
    n = 0
    for (status, clear, stamp), rids in groups.items():
        patch = {fm["status"]: status}
        if stamp:
            patch[fm["updated"]] = now_str
        if clear:
            patch[fm["res_by"]] = None
            patch[fm["res_from"]] = None
            patch[fm["res_until"]] = None
        for i in range(0, len(rids), V3_BATCH_LIMIT):
            body = {"record_id_list": rids[i:i + V3_BATCH_LIMIT], "patch": patch}
            d = _api("POST", url, token=token, body=body)
            if d.get("code") != 0 and "limited" in json.dumps(d):
                time.sleep(2)  # transient QPS limit -> one retry
                d = _api("POST", url, token=token, body=body)
            if d.get("code") != 0:
                raise RuntimeError(f"{b['label']} v3 batch_update error: " + json.dumps(d))
            time.sleep(0.5)  # soften bursts between group calls
        n += len(rids)
    if sweep:
        _LAST_SWEEP[b["label"]] = now_s
    return len(groups), n


# v3-primary mode: how often to attempt the v1 full-fidelity detail write
# (占用进程/显存/利用率 per card). 300s -> ~8.6k v1 calls/month, so the v1
# monthly quota can never be exhausted by this channel.
DETAIL_SECONDS = int(os.environ.get("DETAIL_SECONDS", "300"))
_LAST_DETAIL = {}  # backend label -> epoch seconds of last v1 detail attempt

# v3 per-card detail patches: a single-record "batch" carries any per-card
# values, so proc/mem/util CAN go through v3 - just not for the whole board
# every cycle. Written only when a card's process set actually changes (job
# start/stop), capped per cycle; leftovers carry over to the next cycle.
DETAIL_PATCH_LIMIT = int(os.environ.get("DETAIL_PATCH_LIMIT", "15"))


def _norm_proc(s):
    """Order-insensitive process-list key so nvidia-smi enumeration order
    changes never look like a job change (would flap the detail channel)."""
    return ", ".join(sorted(p.strip() for p in (s or "").split(",") if p.strip()))


def _detail_sig(fm, fields):
    """What the detail channel considers "changed" for one card: the process
    set plus the served model and endpoint. A server restarting on a different
    port has the same pids on the same cards, so without the endpoint here the
    board would keep advertising the old address."""
    return (_norm_proc(fields.get(fm["proc"], "")),
            _text(fields.get(fm["model"], "")),
            _text(fields.get(fm["endpoint"], "")))


def _card_detail_sig(fm, card):
    """The same signature, taken from what the board currently shows."""
    return _detail_sig(fm, {fm["proc"]: card.get("cur_proc", ""),
                            fm["model"]: card.get("cur_model", ""),
                            fm["endpoint"]: card.get("cur_endpoint", "")})


def push_v3_details(b, token, updates, cards, limit=DETAIL_PATCH_LIMIT):
    """Per-card v3 patches for cards whose process set changed vs the board.
    Returns (written, backlog_remaining)."""
    fm = b["fmap"]
    cur_by_rid = {c["record_id"]: _card_detail_sig(fm, c) for c in cards.values()}
    pending = [u for u in updates
               if fm["proc"] in u["fields"]
               and _detail_sig(fm, u["fields"]) != cur_by_rid.get(u["record_id"])]
    url = (f"{b['base_url']}/open-apis/base/v3/bases/{b['base_token']}"
           f"/tables/{b['table_id']}/records/batch_update")
    written = 0
    for u in pending[:limit]:
        patch = {fm["proc"]: u["fields"][fm["proc"]],
                 fm["mem"]: u["fields"][fm["mem"]],
                 fm["util"]: u["fields"][fm["util"]]}
        for k in ("model", "endpoint"):   # absent when the board lacks the column
            if fm[k] in u["fields"]:
                patch[fm[k]] = u["fields"][fm[k]]
        body = {"record_id_list": [u["record_id"]], "patch": patch}
        d = _api("POST", url, token=token, body=body)
        if d.get("code") != 0 and "limited" in json.dumps(d):
            time.sleep(2)
            d = _api("POST", url, token=token, body=body)
        if d.get("code") != 0:
            raise RuntimeError(f"{b['label']} v3 detail error: " + json.dumps(d))
        written += 1
        time.sleep(0.3)
    return written, len(pending) - written


# --- CPU-load rows (one per host, written by cpu_agent.py) ---
CPU_FMAP = {"host": "主机", "status": "状态", "load": "负载",
            "containers": "容器数", "procs": "任务进程数",
            "detail": "任务详情", "updated": "更新时间",
            "peak": "负载峰值1h", "io": "IO占用%",
            "mem": "内存占用%", "disk": "磁盘占用%"}
CPU_SNAP_DIR = os.path.expanduser(os.environ.get("CPU_SNAP_DIR", "~/gpu-board/cpu_snaps"))
CPU_PUSH_SECONDS = int(os.environ.get("CPU_PUSH_SECONDS", "300"))
CPU_STALE_SECONDS = int(os.environ.get("CPU_STALE_SECONDS", "900"))
_LAST_CPU_PUSH = [0]


def cpu_status(snap, now_s):
    if now_s - int(snap.get("recv_ts", snap.get("ts", 0))) > CPU_STALE_SECONDS:
        return "Stale"
    return "Active" if (snap.get("procs") or snap.get("containers")) else "Idle"


def plan_cpu_writes(snaps, rows, now_s, fm=CPU_FMAP, stale_seconds=None):
    """(updates, creates) for the CPU table.

    rows: host -> {"record_id", "status", "updated"} read from the table.
    A host with a snapshot here gets its row updated (created if missing). A
    row nobody has updated for stale_seconds is marked Stale - status only,
    the same rule the GPU table uses when several masters share it."""
    stale_seconds = CPU_STALE_SECONDS if stale_seconds is None else stale_seconds
    updates, creates = [], []
    for host, s in snaps.items():
        fields = {fm["host"]: host, fm["status"]: cpu_status(s, now_s),
                  fm["load"]: s.get("load1", 0), fm["containers"]: s.get("containers", 0),
                  fm["procs"]: s.get("procs", 0), fm["detail"]: s.get("detail", ""),
                  fm["updated"]: now_s * 1000}
        for key, snap_key in (("peak", "load_peak_1h"), ("io", "io_util"),
                              ("mem", "mem_pct"), ("disk", "disk_pct")):
            if s.get(snap_key) is not None:
                fields[fm[key]] = s[snap_key]
        row = rows.get(host)
        if row:
            updates.append({"record_id": row["record_id"], "fields": fields})
        else:
            creates.append({"fields": fields})
    for host, row in rows.items():
        if host in snaps or row.get("status") == "Stale":
            continue
        last = row.get("updated")
        if last is not None and now_s * 1000 - last < stale_seconds * 1000:
            continue
        updates.append({"record_id": row["record_id"], "fields": {fm["status"]: "Stale"}})
    return updates, creates


def _cpu_row(fm, fields):
    return {"status": _text(fields.get(fm["status"])), "updated": _ts(fields.get(fm["updated"]))}


def push_cpu_rows(b, token, table_id, now_s):
    """Upsert the CPU table from the snapshots in CPU_SNAP_DIR.
    v1 first (one search read + one batch_update/create, same shape as the GPU
    push); v3 per-row fallback while a v1 monthly method bucket is dead."""
    fm = CPU_FMAP
    snaps = {}
    for path in glob.glob(os.path.join(CPU_SNAP_DIR, "*.json")):
        try:
            with open(path) as fh:
                s = json.load(fh)
            snaps[s["host"]] = s
        except (OSError, ValueError, KeyError):
            continue
    if not snaps:
        return 0
    rows = {}  # host -> {"record_id", "status", "updated"}
    v3 = (f"{b['base_url']}/open-apis/base/v3/bases/{b['base_token']}"
          f"/tables/{table_id}/records")
    items = _v1_read_all(b, token, table_id)
    if items is not None:
        for item in items:
            f = item.get("fields", {})
            host = _text(f.get(fm["host"]))
            if host:
                rows[host] = dict(_cpu_row(fm, f), record_id=item.get("record_id"))
    else:
        d = _api("GET", v3 + "?limit=200", token=token)
        if d.get("code") != 0:
            raise RuntimeError("cpu v3 read error: " + json.dumps(d))
        cols = d["data"].get("fields", [])
        for rid, r in zip(d["data"].get("record_id_list", []), d["data"].get("data", [])):
            f = dict(zip(cols, r))
            host = _text(f.get(fm["host"]))
            if host:
                rows[host] = dict(_cpu_row(fm, f), record_id=rid)
    updates, creates = plan_cpu_writes(snaps, rows, now_s, fm)
    try:
        push(dict(b, table_id=table_id, label=b["label"] + "-cpu"), token, updates, creates)
        return len(updates) + len(creates)
    except QuotaDead:
        pass
    # v3 fallback: per-row patches (v3 batch_update applies ONE patch to many ids,
    # so per-row values need one call each); v3 datetime cells take
    # "YYYY-MM-DD HH:mm:ss" strings, not epoch-ms. Rows are not created here.
    now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_s))
    for u in updates:
        patch = dict(u["fields"])
        if fm["updated"] in patch:
            patch[fm["updated"]] = now_str
        d = _api("POST", v3 + "/batch_update", token=token,
                 body={"record_id_list": [u["record_id"]], "patch": patch})
        if d.get("code") != 0:
            raise RuntimeError("cpu v3 write error: " + json.dumps(d))
        time.sleep(0.3)
    return len(updates)


def main(argv=None):
    p = argparse.ArgumentParser(description="GPU board master: snapshots -> Feishu Base")
    p.add_argument("--loop", type=int, default=0, help="repeat every N seconds")
    p.add_argument("--dry-run", action="store_true",
                   help="read + plan only; print instead of writing")
    args = p.parse_args(argv)

    snap_dir = os.path.expanduser(os.environ.get("SNAP_DIR", "~/gpu-board/nodes"))
    stale_seconds = int(os.environ.get("STALE_SECONDS", "180"))
    b = backend_from_env()
    cpu_table = os.environ.get("FEISHU_CPU_TABLE_ID")

    def once():
        now_s = int(time.time())
        now_ms = now_s * 1000
        snaps = load_snapshots(snap_dir, stale_seconds, now_s)
        if not snaps:
            print(f"no snapshots in {snap_dir}; nothing to push", file=sys.stderr)
            return
        try:
            token = get_token(b["base_url"], b["app_id"], b["app_secret"])
            junk = []
            cards = board_cards(b, token, junk)
        except Exception as e:
            print(f"{b['label']}: read failed, skipping this cycle: {e}", file=sys.stderr)
            return
        known = board_field_names(b, token)
        if known is not None and not {b["fmap"]["node"], b["fmap"]["gpu"]} <= known:
            # Without the key columns every row looks unmatched: writing would
            # create duplicates every cycle. Stop and say why instead.
            print(f"{b['label']}: table has no {b['fmap']['node']}/{b['fmap']['gpu']} column "
                  f"(renamed in the UI?); not writing. Run setup_tables.py and restart.",
                  file=sys.stderr)
            return
        fresh = sum(1 for s in snaps.values() if s["fresh"])
        updates, creates = plan_writes(snaps, cards, b["fmap"], now_ms,
                                       known_fields=known,
                                       stale_after_ms=stale_seconds * 1000)
        if args.dry_run:
            print(f"[dry-run] {b['label']}: nodes={len(snaps)} (fresh {fresh}) "
                  f"updates={len(updates)} creates={len(creates)} "
                  f"blank-rows-to-delete={len(junk)}")
            for u in (updates + creates)[:3]:
                print("   ", json.dumps(u, ensure_ascii=False)[:200])
            return
        # Rows a person added by hand in the UI and left empty: they can
        # never match a card, so they would linger forever. Deleted before
        # the push so a create in this same cycle doesn't race them.
        if junk and known is not None:
            try:
                n = delete_records(b, token, junk)
                print(f"{b['label']}: removed {n} blank row(s): "
                      f"{', '.join(junk[:5])}", file=sys.stderr)
            except Exception as e:
                print(f"{b['label']}: blank-row cleanup failed: {e}", file=sys.stderr)
        if b.get("prefer_v3"):
            # v3 is THE scheduling mode: status + timestamp every cycle.
            try:
                g, n = push_v3_grouped(b, token, updates, cards, now_s)
                print(f"{b['label']}: v3 status push ({n} records in {g} groups)",
                      file=sys.stderr)
            except Exception as e:
                print(f"{b['label']}: v3 status push failed: {e}", file=sys.stderr)
            # v1 is only the low-rate detail channel (per-card mem/util/proc).
            if (now_s - _LAST_DETAIL.get(b["label"], 0)) >= DETAIL_SECONDS:
                _LAST_DETAIL[b["label"]] = now_s
                try:
                    push(b, token, updates, creates)
                    print(f"{b['label']}: v1 detail push ok "
                          f"({len(updates)} cards, full fields)", file=sys.stderr)
                except QuotaDead:
                    print(f"{b['label']}: v1 detail quota still dead "
                          f"(mem/util/proc frozen until reset)", file=sys.stderr)
                except Exception as e:
                    print(f"{b['label']}: v1 detail push failed: {e}", file=sys.stderr)
        else:
            try:
                push(b, token, updates, creates)
                print(f"{b['label']}: pushed nodes={len(snaps)} (fresh {fresh}) "
                      f"updated={len(updates)} created={len(creates)}", file=sys.stderr)
            except QuotaDead:
                try:
                    g, n = push_v3_grouped(b, token, updates, cards, now_s)
                    w, left = push_v3_details(b, token, updates, cards)
                    print(f"{b['label']}: v1 quota dead -> v3 push "
                          f"(status {n} in {g} groups; details {w} cards"
                          f"{f', {left} queued' if left else ''})", file=sys.stderr)
                except Exception as e:
                    print(f"{b['label']}: degraded push failed: {e}", file=sys.stderr)
            except Exception as e:
                print(f"{b['label']}: push failed: {e}", file=sys.stderr)
        # CPU-load rows, on their own slower cadence
        if cpu_table and (now_s - _LAST_CPU_PUSH[0]) >= CPU_PUSH_SECONDS:
            _LAST_CPU_PUSH[0] = now_s
            try:
                n = push_cpu_rows(b, token, cpu_table, now_s)
                print(f"{b['label']}: cpu rows pushed ({n})", file=sys.stderr)
            except Exception as e:
                print(f"{b['label']}: cpu push failed: {e}", file=sys.stderr)

    if args.loop > 0:
        while True:
            try:
                once()
            except Exception as e:  # one bad cycle never stops the loop
                print(f"master cycle error: {e}", file=sys.stderr)
            time.sleep(args.loop)
    else:
        once()


if __name__ == "__main__":
    main()
