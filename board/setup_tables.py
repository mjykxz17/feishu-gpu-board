#!/usr/bin/env python3
"""setup_tables.py - create (or complete) the board tables in a Feishu / Lark Base.

    python3 setup_tables.py --gpu-table H200 --gpu-table B200 --cpu-table "CPU 负载"

For every table name given: create it with the columns master.py writes (plus
the people columns 负责人 / 备注 and, unless --no-reservations, the booking
columns), or, if a table of that name already exists, add whichever of those
columns it lacks. GPU tables also get three filtered views: free cards, cards
held but idle, and offline cards. Idempotent - safe to re-run.

Prints one machine-readable line per table at the end:
    TABLE gpu H200 tblXXXXXXXXXXXXXX
    TABLE cpu CPU 负载 tblYYYYYYYYYYYYYY

Env: FEISHU_APP_ID, FEISHU_BASE_TOKEN, FEISHU_BASE_URL (default
https://open.feishu.cn), and the app secret either in FEISHU_APP_SECRET or as the
first line of stdin with --secret-stdin (keeps it out of argv and the env).
Standard library only; imports nothing from the rest of the kit, so it can be
shipped alone to any machine that can reach the Feishu API.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

TEXT, NUMBER, SINGLE_SELECT, DATETIME = 1, 2, 3, 5


def _num(fmt="0"):
    return {"type": NUMBER, "property": {"formatter": fmt}}


def _dt(fmt="yyyy/MM/dd HH:mm"):
    return {"type": DATETIME, "property": {"date_formatter": fmt, "auto_fill": False}}


def _select(*options):
    # Feishu palette indices: 7 green, 5 orange, 3 blue, 10 grey
    return {"type": SINGLE_SELECT,
            "property": {"options": [{"name": n, "color": c} for n, c in options]}}


_TEXT = {"type": TEXT}


def gpu_fields(reservations=True):
    """Column specs for a GPU table. The FIRST one becomes the primary column.
    Names must match master.FMAP exactly."""
    cols = [
        ("节点", _TEXT), ("卡号", _num()),
        ("状态", _select(("Free", 7), ("Busy", 5), ("Reserved", 3), ("Stale", 10))),
        ("显存", _TEXT), ("利用率", _num()), ("占用进程", _TEXT),
        ("服务模型", _TEXT), ("服务端点", _TEXT), ("更新时间", _dt()),
        ("负责人", _TEXT), ("备注", _TEXT),
    ]
    if reservations:
        cols += [("预定人", _TEXT), ("预定开始", _dt()), ("预定结束", _dt())]
    return [dict(spec, field_name=name) for name, spec in cols]


def cpu_fields():
    """Column specs for the CPU table. Names must match master.CPU_FMAP."""
    cols = [
        ("主机", _TEXT),
        ("状态", _select(("Active", 7), ("Idle", 10), ("Stale", 3))),
        ("负载", _num("0.00")), ("负载峰值1h", _num("0.00")),
        ("IO占用%", _num()), ("内存占用%", _num()), ("磁盘占用%", _num()),
        ("容器数", _num()), ("任务进程数", _num()), ("任务详情", _TEXT),
        ("更新时间", _dt()), ("负责人", _TEXT), ("备注", _TEXT),
    ]
    return [dict(spec, field_name=name) for name, spec in cols]


# view name -> [(column, operator, value)]; value is matched against option
# NAMES for single-select columns and resolved to option ids at run time.
GPU_VIEWS = {
    "空闲 Free now": [("状态", "is", "Free")],
    "空转占用 Held but idle": [("状态", "is", "Busy"), ("利用率", "is", 0)],
    "离线 Offline": [("状态", "is", "Stale")],
}


class Api:
    def __init__(self, base_url, app_id, secret, base_token):
        self.base = base_url.rstrip("/")
        self.app = f"{self.base}/open-apis/bitable/v1/apps/{base_token}"
        d = self.call("POST", f"{self.base}/open-apis/auth/v3/tenant_access_token/internal",
                      {"app_id": app_id, "app_secret": secret}, auth=False, raw=True)
        self.token = d["tenant_access_token"]

    def call(self, method, url, body=None, auth=True, raw=False):
        req = urllib.request.Request(url, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Content-Type", "application/json; charset=utf-8")
        if auth:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            d = json.loads(e.read().decode() or "{}")
        if d.get("code") != 0:
            hint = ""
            if d.get("code") in (91402, 91403, 1254302, 1254040):
                hint = ("\n  hint: add the app to the Base as a collaborator with edit "
                        "rights (Base ... menu -> 添加文档应用 / Add document app), and "
                        "make sure it has the bitable:app scope")
            raise SystemExit(f"Feishu API error on {method} {url.split('/open-apis')[-1]}: "
                             f"code={d.get('code')} msg={d.get('msg')}{hint}")
        return d if raw else (d.get("data") or {})

    def list_all(self, path, key="items"):
        out, page = [], ""
        while True:
            sep = "&" if "?" in path else "?"
            d = self.call("GET", f"{self.app}{path}{sep}page_size=100"
                                 + (f"&page_token={page}" if page else ""))
            out += d.get(key) or []
            if not d.get("has_more"):
                return out
            page = d.get("page_token", "")


def ensure_table(api, name, fields, log):
    """table_id of the table called `name`, created or completed with `fields`."""
    tables = {t["name"]: t["table_id"] for t in api.list_all("/tables")}
    if name not in tables:
        d = api.call("POST", f"{api.app}/tables",
                     {"table": {"name": name, "default_view_name": "全部 All",
                                "fields": fields}})
        log(f"created table {name!r} ({d['table_id']}) with {len(fields)} columns")
        return d["table_id"]
    tid = tables[name]
    have = {f["field_name"] for f in api.list_all(f"/tables/{tid}/fields")}
    missing = [f for f in fields if f["field_name"] not in have]
    for f in missing:
        api.call("POST", f"{api.app}/tables/{tid}/fields", f)
    log(f"table {name!r} ({tid}) exists; added {len(missing)} missing column(s)"
        + (f": {', '.join(f['field_name'] for f in missing)}" if missing else ""))
    return tid


def _condition(fields, col, op, value):
    f = fields[col]
    # The API wants a JSON-encoded list: option ids for a single select,
    # integer strings for a number (e.g. '["0"]').
    if f["type"] == SINGLE_SELECT:
        value = [next(o["id"] for o in f["property"]["options"] if o["name"] == value)]
    else:
        value = [str(value)]
    return {"field_id": f["field_id"], "operator": op, "value": json.dumps(value)}


def ensure_views(api, tid, views, log):
    fields = {f["field_name"]: f for f in api.list_all(f"/tables/{tid}/fields")}
    have = {v["view_name"] for v in api.list_all(f"/tables/{tid}/views")}
    for name, conds in views.items():
        if name in have:
            continue
        vid = api.call("POST", f"{api.app}/tables/{tid}/views",
                       {"view_name": name, "view_type": "grid"})["view"]["view_id"]
        try:
            api.call("PATCH", f"{api.app}/tables/{tid}/views/{vid}", {"property": {
                "filter_info": {"conjunction": "and",
                                "conditions": [_condition(fields, *c) for c in conds]}}})
            log(f"  view {name!r} created")
        except SystemExit as e:  # the view still exists; only the filter is missing
            log(f"  view {name!r} created WITHOUT its filter ({e}); set it in the UI")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--gpu-table", action="append", default=[], metavar="NAME",
                   help="GPU table to create/complete (repeatable, e.g. one per GPU type)")
    p.add_argument("--cpu-table", metavar="NAME", help="CPU-load table to create/complete")
    p.add_argument("--no-reservations", action="store_true",
                   help="leave out the 预定人/预定开始/预定结束 booking columns")
    p.add_argument("--no-views", action="store_true", help="do not create the filtered views")
    p.add_argument("--secret-stdin", action="store_true",
                   help="read the app secret from the first line of stdin")
    p.add_argument("--dry-run", action="store_true", help="print the schema, call nothing")
    args = p.parse_args(argv)
    if not args.gpu_table and not args.cpu_table:
        p.error("give at least one --gpu-table or --cpu-table")

    plan = [("gpu", n, gpu_fields(not args.no_reservations)) for n in args.gpu_table]
    if args.cpu_table:
        plan.append(("cpu", args.cpu_table, cpu_fields()))
    if args.dry_run:
        for kind, name, fields in plan:
            print(json.dumps({"kind": kind, "name": name, "fields": fields},
                             ensure_ascii=False))
        return

    secret = sys.stdin.readline().strip() if args.secret_stdin else os.environ.get(
        "FEISHU_APP_SECRET", "")
    missing = [k for k, v in (("FEISHU_APP_ID", os.environ.get("FEISHU_APP_ID")),
                              ("FEISHU_BASE_TOKEN", os.environ.get("FEISHU_BASE_TOKEN")),
                              ("app secret", secret)) if not v]
    if missing:
        raise SystemExit("missing: " + ", ".join(missing))
    api = Api(os.environ.get("FEISHU_BASE_URL", "https://open.feishu.cn"),
              os.environ["FEISHU_APP_ID"], secret, os.environ["FEISHU_BASE_TOKEN"])

    def log(msg):
        print(msg, file=sys.stderr)
    results = []
    for kind, name, fields in plan:
        tid = ensure_table(api, name, fields, log)
        if kind == "gpu" and not args.no_views:
            ensure_views(api, tid, GPU_VIEWS, log)
        results.append((kind, name, tid))
    for kind, name, tid in results:
        print(f"TABLE {kind} {name} {tid}")


if __name__ == "__main__":
    main()
