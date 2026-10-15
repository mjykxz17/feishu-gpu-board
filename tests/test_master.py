"""Offline tests for board/master.py (planning, status rules, API fallbacks; no network)."""
import importlib.util
import pathlib
import sys

spec = importlib.util.spec_from_file_location(
    "master", pathlib.Path(__file__).parent.parent / "board" / "master.py")
m = importlib.util.module_from_spec(spec)
sys.modules["master"] = m
spec.loader.exec_module(m)

NOW_MS = 1_800_000_000_000
FM_F = m.FMAP


def row(gpu, status="Free", util=0, proc=""):
    return {"gpu": gpu, "status": status, "mem_used_mb": 0,
            "mem_total_mb": 184320, "util": util, "user_proc": proc}


def card(rid, status="Free", by="", frm=None, until=None, updated=None):
    return {"record_id": rid, "reserved_by": by, "reserved_from": frm,
            "reserved_until": until, "cur_status": status, "cur_updated": updated}


def test_fresh_node_updates_all_cards_with_board_field_names():
    snaps = {"n1": {"fresh": True, "rows": [row(0), row(1, "Busy", 90, "python:1")]}}
    cards = {("n1", 0): card("r0"), ("n1", 1): card("r1")}
    updates, creates = m.plan_writes(snaps, cards, FM_F, NOW_MS)
    assert len(updates) == 2 and not creates
    by_rid = {u["record_id"]: u["fields"] for u in updates}
    assert by_rid["r0"]["状态"] == "Free"
    assert by_rid["r1"]["状态"] == "Busy"
    assert by_rid["r1"]["显存"] == "0.0 / 180 GB"


def test_active_reservation_shows_reserved_and_leaves_booking_untouched():
    cards = {("n1", 0): card("r0", by="alice", frm=NOW_MS - 1000, until=NOW_MS + 1000)}
    snaps = {"n1": {"fresh": True, "rows": [row(0)]}}
    updates, _ = m.plan_writes(snaps, cards, FM_F, NOW_MS)
    f = updates[0]["fields"]
    assert f["状态"] == "Reserved"
    assert "预定人" not in f  # booking columns are only written to clear them


def test_expired_idle_reservation_is_cleared():
    cards = {("n1", 0): card("r0", by="bob", frm=NOW_MS - 5000, until=NOW_MS - 1000)}
    snaps = {"n1": {"fresh": True, "rows": [row(0)]}}
    updates, _ = m.plan_writes(snaps, cards, FM_F, NOW_MS)
    f = updates[0]["fields"]
    assert f["状态"] == "Free"
    assert f["预定人"] == "" and f["预定开始"] is None and f["预定结束"] is None


def test_reservation_without_end_lasts_until_end_of_day():
    start, end = m.reservation_window("carol", None, None, NOW_MS)
    assert start == NOW_MS and end == m._end_of_day_ms(NOW_MS) and end > NOW_MS


def test_stale_snapshot_marks_stale_and_keeps_reservation():
    cards = {("n1", 0): card("r0", by="alice", frm=NOW_MS - 1000, until=NOW_MS + 1000)}
    snaps = {"n1": {"fresh": False, "rows": [row(0)]}}
    updates, _ = m.plan_writes(snaps, cards, FM_F, NOW_MS)
    f = updates[0]["fields"]
    assert f["状态"] == "Stale"
    assert "预定人" not in f


def test_active_reservation_beats_free_but_not_busy():
    r = card("r0", by="bob", frm=NOW_MS - 1000, until=NOW_MS + 1000)
    assert m.decide_status("Free", r, NOW_MS) == ("Reserved", False)
    assert m.decide_status("Busy", r, NOW_MS) == ("Busy", False)


def test_board_node_without_snapshot_goes_stale_once():
    snaps = {"n1": {"fresh": True, "rows": [row(0)]}}
    cards = {("n1", 0): card("r0"), ("gone", 0): card("rg", status="Free")}
    updates, _ = m.plan_writes(snaps, cards, FM_F, NOW_MS)
    by_rid = {u["record_id"]: u["fields"] for u in updates}
    assert by_rid["rg"]["状态"] == "Stale"
    updates2, _ = m.plan_writes({}, {("gone", 0): card("rg", status="Stale")}, FM_F, NOW_MS)
    assert updates2 == []


def test_missing_card_is_created():
    snaps = {"n1": {"fresh": True, "rows": [row(3)]}}
    updates, creates = m.plan_writes(snaps, {}, FM_F, NOW_MS)
    assert not updates and len(creates) == 1
    assert creates[0]["fields"]["卡号"] == 3


def test_backend_from_env(monkeypatch):
    for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_BASE_TOKEN", "FEISHU_TABLE_ID"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("FEISHU_BASE_URL", "https://open.larksuite.com/")
    b = m.backend_from_env()
    assert b["base_url"] == "https://open.larksuite.com" and b["table_id"] == "x"
    assert b["fmap"] is m.FMAP and not b["prefer_v3"]


def test_load_snapshots_marks_old_ones_not_fresh(tmp_path):
    import json
    (tmp_path / "a.json").write_text(json.dumps({"node": "a", "ts": 1000, "rows": []}))
    (tmp_path / "b.json").write_text(json.dumps({"node": "b", "ts": 1170, "rows": []}))
    (tmp_path / "bad.json").write_text("{not json")
    snaps = m.load_snapshots(str(tmp_path), 180, 1200)
    assert snaps["a"]["fresh"] is False and snaps["b"]["fresh"] is True
    assert "bad" not in snaps


def _grouped_backend():
    return {"label": "feishu", "fmap": FM_F, "base_url": "http://x",
            "base_token": "b", "table_id": "t"}


def test_v3_grouped_writes_only_changes_and_groups(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "_api", lambda *a, **k: (calls.append(k.get("body")), {"code": 0})[1])
    monkeypatch.setattr(m, "SWEEP_SECONDS", 1800)  # liveness-only sweep configured
    m._LAST_SWEEP["feishu"] = 10**9  # sweep just happened -> change-only mode
    cards = {("n1", 0): card("r0", status="Free"), ("n1", 1): card("r1", status="Free"),
             ("n1", 2): card("r2", status="Busy")}
    updates = [
        {"record_id": "r0", "fields": {FM_F["status"]: "Busy", FM_F["updated"]: 1}},   # changed
        {"record_id": "r1", "fields": {FM_F["status"]: "Free", FM_F["updated"]: 1}},   # unchanged
        {"record_id": "r2", "fields": {FM_F["status"]: "Busy", FM_F["updated"]: 1}},   # unchanged
    ]
    g, n = m.push_v3_grouped(_grouped_backend(), "tok", updates, cards, 10**9 + 60)
    assert (g, n) == (1, 1)
    assert calls[0]["record_id_list"] == ["r0"]
    assert calls[0]["patch"][FM_F["status"]] == "Busy"


def test_v3_grouped_sweep_writes_all_in_status_groups(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "_api", lambda *a, **k: (calls.append(k.get("body")), {"code": 0})[1])
    m._LAST_SWEEP.pop("feishu", None)  # never swept -> full sweep
    cards = {("n1", 0): card("r0", status="Free"), ("n1", 1): card("r1", status="Free"),
             ("n1", 2): card("r2", status="Busy")}
    updates = [
        {"record_id": "r0", "fields": {FM_F["status"]: "Free", FM_F["updated"]: 1}},
        {"record_id": "r1", "fields": {FM_F["status"]: "Free", FM_F["updated"]: 1}},
        {"record_id": "r2", "fields": {FM_F["status"]: "Busy", FM_F["updated"]: 1}},
    ]
    g, n = m.push_v3_grouped(_grouped_backend(), "tok", updates, cards, 5000)
    assert (g, n) == (2, 3)
    assert m._LAST_SWEEP["feishu"] == 5000


def test_v3_grouped_clear_group_wipes_reservation(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "_api", lambda *a, **k: (calls.append(k.get("body")), {"code": 0})[1])
    m._LAST_SWEEP["feishu"] = 10**9
    cards = {("n1", 0): card("r0", status="Free")}
    updates = [{"record_id": "r0", "fields": {
        FM_F["status"]: "Free", FM_F["updated"]: 1,
        FM_F["res_by"]: "", FM_F["res_from"]: None, FM_F["res_until"]: None}}]
    g, n = m.push_v3_grouped(_grouped_backend(), "tok", updates, cards, 10**9 + 60)
    assert (g, n) == (1, 1)
    assert calls[0]["patch"][FM_F["res_by"]] is None


def test_ts_parses_v3_datetime_strings():
    assert m._ts("2026-07-14 15:20:47") is not None
    assert m._ts("2026/07/14 15:20") is not None
    assert m._ts("not a date") is None


def test_v3_details_writes_only_proc_changes_with_cap(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "_api", lambda *a, **k: (calls.append(k.get("body")), {"code": 0})[1])
    cards = {
        ("n1", 0): dict(card("r0"), cur_proc="python:1"),        # unchanged
        ("n1", 1): dict(card("r1"), cur_proc="old:9"),           # changed
        ("n1", 2): dict(card("r2"), cur_proc="b:2, a:1"),        # same set, other order
        ("n1", 3): dict(card("r3"), cur_proc=""),                # newly busy
    }
    def upd(rid, proc):
        return {"record_id": rid, "fields": {
            FM_F["proc"]: proc, FM_F["mem"]: "1.0 / 278 GB",
            FM_F["util"]: 5, FM_F["status"]: "Busy", FM_F["updated"]: 1}}
    updates = [upd("r0", "python:1"), upd("r1", "new:7"),
               upd("r2", "a:1, b:2"), upd("r3", "x:3")]
    w, left = m.push_v3_details(_grouped_backend(), "tok", updates, cards, limit=1)
    assert (w, left) == (1, 1)          # cap honored, one queued
    assert calls[0]["record_id_list"] == ["r1"]
    w, left = m.push_v3_details(_grouped_backend(), "tok", updates, cards, limit=15)
    assert (w, left) == (2, 0)          # r1 + r3; r2's reordered set not a change
    assert calls[-1]["patch"][FM_F["proc"]] == "x:3"


# --- served-model columns ---

def srow(gpu, model="", endpoint="", **kw):
    r = row(gpu, **kw)
    r["model"] = model
    r["endpoint"] = endpoint
    return r


def test_served_model_and_endpoint_are_written_to_the_board():
    snaps = {"n1": {"fresh": True, "rows": [
        srow(0, "deepseek-v3", "10.0.0.64:8002", status="Busy", proc="p:1")]}}
    updates, _ = m.plan_writes(snaps, {("n1", 0): card("r0")}, FM_F, NOW_MS)
    f = updates[0]["fields"]
    assert f["服务模型"] == "deepseek-v3"
    assert f["服务端点"] == "10.0.0.64:8002"


def test_cards_without_a_served_model_write_empty_not_missing():
    """Blank, not absent: a card that stops serving must clear the old value."""
    snaps = {"n1": {"fresh": True, "rows": [srow(0, status="Busy", proc="train:1")]}}
    updates, _ = m.plan_writes(snaps, {("n1", 0): card("r0")}, FM_F, NOW_MS)
    f = updates[0]["fields"]
    assert f["服务模型"] == "" and f["服务端点"] == ""


def test_older_agent_snapshot_without_the_keys_does_not_crash():
    """Upgrade window: master upgraded first, agents still on the old build."""
    snaps = {"n1": {"fresh": True, "rows": [row(0)]}}  # no model/endpoint keys
    updates, _ = m.plan_writes(snaps, {("n1", 0): card("r0")}, FM_F, NOW_MS)
    assert updates[0]["fields"]["服务模型"] == ""


def test_payload_is_filtered_to_columns_the_board_actually_has():
    """A board missing 服务模型 must keep working, not fail the whole batch."""
    known = {"节点", "卡号", "状态", "显存", "利用率", "占用进程", "更新时间"}
    snaps = {"n1": {"fresh": True, "rows": [srow(0, "m", "1.2.3.4:8000")]}}
    updates, _ = m.plan_writes(snaps, {("n1", 0): card("r0")}, FM_F, NOW_MS,
                               known_fields=known)
    f = updates[0]["fields"]
    assert "服务模型" not in f and "服务端点" not in f
    assert f["状态"] == "Free"  # everything else still written


def test_no_filtering_when_the_board_fields_are_unknown():
    snaps = {"n1": {"fresh": True, "rows": [srow(0, "m", "1.2.3.4:8000")]}}
    updates, _ = m.plan_writes(snaps, {("n1", 0): card("r0")}, FM_F, NOW_MS,
                               known_fields=None)
    assert updates[0]["fields"]["服务模型"] == "m"


def test_detail_signature_tracks_model_and_endpoint():
    """The v3 detail channel repushes a card only when this signature changes,
    so a server moving to a new port must not look unchanged."""
    fm = FM_F

    def sig(proc, model, endpoint):
        return m._detail_sig(fm, {fm["proc"]: proc, fm["model"]: model,
                                  fm["endpoint"]: endpoint})

    base = sig("p:1", "deepseek-v3", "10.0.0.64:8002")
    assert base == sig("p:1", "deepseek-v3", "10.0.0.64:8002")
    assert base != sig("p:1", "deepseek-v3", "10.0.0.64:8003")
    assert base != sig("p:1", "qwen3_32b", "10.0.0.64:8002")


def test_detail_signature_ignores_process_enumeration_order():
    fm = FM_F
    a_ = m._detail_sig(fm, {fm["proc"]: "b:2, a:1", fm["model"]: "x", fm["endpoint"]: "y"})
    b_ = m._detail_sig(fm, {fm["proc"]: "a:1, b:2", fm["model"]: "x", fm["endpoint"]: "y"})
    assert a_ == b_


def test_detail_signature_of_a_board_card_matches_an_unchanged_update():
    """Board side and snapshot side must produce the same signature shape."""
    fm = FM_F
    c = dict(card("r0"), cur_proc="a:1", cur_model="m", cur_endpoint="1.2.3.4:8000")
    assert m._card_detail_sig(fm, c) == m._detail_sig(
        fm, {fm["proc"]: "a:1", fm["model"]: "m", fm["endpoint"]: "1.2.3.4:8000"})


# --- blank-row cleanup ---

def test_blank_rows_are_collected_for_deletion():
    fm = FM_F
    out, junk = {}, []
    m._card_from_fields(fm, {"节点": None, "卡号": None}, "recBlank", out, junk)
    assert out == {} and junk == ["recBlank"]


def test_rows_holding_anything_are_never_deleted():
    """A row missing 节点/卡号 but holding a note (or anything) is not ours to
    delete - e.g. after someone renamed the 节点 column, every row looks like
    this, and deleting them would wipe people's notes and bookings."""
    fm = FM_F
    out, junk = {}, []
    m._card_from_fields(fm, {"节点": "n1", "卡号": None}, "recHalf", out, junk)
    m._card_from_fields(fm, {"卡号": 3, "备注": "keep me"}, "recNote", out, junk)
    m._card_from_fields(fm, {"节点 renamed": "n1", "卡号": 0}, "recRenamed", out, junk)
    assert out == {} and junk == []


def test_real_rows_are_never_junk():
    fm = FM_F
    out, junk = {}, []
    m._card_from_fields(fm, {"节点": "n1", "卡号": 0, "状态": "Free"}, "recReal", out, junk)
    assert junk == [] and ("n1", 0) in out


def test_junk_collection_is_optional_for_callers_that_do_not_want_it():
    out = {}
    m._card_from_fields(FM_F, {"节点": None, "卡号": None}, "recBlank", out)
    assert out == {}


# --- delete_records / board_field_names (offline, _api stubbed) ---

BACK = {"label": "feishu", "base_url": "https://open.feishu.cn",
        "base_token": "bt", "table_id": "tbl"}


def test_delete_records_posts_batch_delete_and_counts(monkeypatch):
    seen = {}

    def fake_api(method, url, token=None, body=None, timeout=20):
        seen["method"], seen["url"], seen["body"] = method, url, body
        return {"code": 0}

    monkeypatch.setattr(m, "_api", fake_api)
    assert m.delete_records(BACK, "tok", ["r1", "r2"]) == 2
    assert seen["method"] == "POST" and seen["url"].endswith("/records/batch_delete")
    assert seen["body"] == {"records": ["r1", "r2"]}


def test_delete_records_is_a_no_op_for_an_empty_list(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not call the API with nothing to delete")

    monkeypatch.setattr(m, "_api", boom)
    assert m.delete_records(BACK, "tok", []) == 0


def test_delete_records_raises_on_api_error(monkeypatch):
    monkeypatch.setattr(m, "_api", lambda *a, **k: {"code": 1254043, "msg": "nope"})
    try:
        m.delete_records(BACK, "tok", ["r1"])
    except RuntimeError as e:
        assert "batch_delete" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_board_field_names_reads_once_and_caches(monkeypatch):
    calls = []

    def fake_api(method, url, token=None, body=None, timeout=20):
        calls.append(url)
        return {"code": 0, "data": {"items": [{"field_name": "节点"},
                                              {"field_name": "服务模型"}]}}

    monkeypatch.setattr(m, "_api", fake_api)
    m._FIELDS.clear()
    b = dict(BACK, table_id="tblcache1")
    assert m.board_field_names(b, "tok") == frozenset({"节点", "服务模型"})
    assert m.board_field_names(b, "tok") == frozenset({"节点", "服务模型"})
    assert len(calls) == 1


def test_board_field_names_returns_none_when_unreadable(monkeypatch):
    monkeypatch.setattr(m, "_api", lambda *a, **k: {"code": 99991403})
    m._FIELDS.clear()
    assert m.board_field_names(dict(BACK, table_id="tblcache2"), "tok") is None


# --- several masters on one table (one master per box) ---
# Each box's master only has its own node's snapshot. Rows of the other node
# are someone else's: leave them alone while they are being kept fresh, mark
# them Stale once nobody has touched them for the stale window.

def test_rows_another_master_keeps_fresh_are_left_alone():
    snaps = {"box-a": {"fresh": True, "rows": [row(0)]}}
    cards = {("box-a", 0): card("mine"),
             ("box-b", 0): card("peer", updated=NOW_MS - 60_000)}
    updates, creates = m.plan_writes(snaps, cards, FM_F, NOW_MS,
                                     stale_after_ms=180_000)
    assert [u["record_id"] for u in updates] == ["mine"]
    assert creates == []


def test_rows_nobody_updates_go_stale_but_keep_their_last_update_time():
    cards = {("box-b", 0): card("peer", updated=NOW_MS - 600_000)}
    updates, _ = m.plan_writes({}, cards, FM_F, NOW_MS, stale_after_ms=180_000)
    assert updates == [{"record_id": "peer", "fields": {"状态": "Stale"}}]


def test_rows_with_no_update_time_go_stale_as_before():
    cards = {("gone", 0): card("rg")}
    updates, _ = m.plan_writes({}, cards, FM_F, NOW_MS, stale_after_ms=180_000)
    assert updates[0]["fields"]["状态"] == "Stale"


def test_board_cards_carry_their_last_update_time():
    out = {}
    m._card_from_fields(FM_F, {"节点": "n1", "卡号": 0, "更新时间": NOW_MS}, "r0", out)
    assert out[("n1", 0)]["cur_updated"] == NOW_MS


# --- CPU table planning ---

def cpu_snap(host, ts, procs=0, containers=0):
    return {"host": host, "ts": ts, "recv_ts": ts, "load1": 1.5, "procs": procs,
            "containers": containers, "detail": "", "load_peak_1h": 2.0,
            "io_util": None, "mem_pct": 40.0, "disk_pct": 70.0}


def test_cpu_row_is_updated_or_created_for_hosts_with_a_snapshot():
    now = 10_000
    snaps = {"box-a": cpu_snap("box-a", now, procs=3), "box-b": cpu_snap("box-b", now)}
    rows = {"box-a": {"record_id": "ra", "status": "Idle", "updated": now * 1000}}
    updates, creates = m.plan_cpu_writes(snaps, rows, now)
    assert updates[0]["record_id"] == "ra" and updates[0]["fields"]["状态"] == "Active"
    assert creates[0]["fields"]["主机"] == "box-b"
    assert creates[0]["fields"]["状态"] == "Idle"
    assert "IO占用%" not in creates[0]["fields"]   # None metrics are not written


def test_cpu_rows_nobody_updates_go_stale_status_only():
    now = 10_000
    rows = {"peer": {"record_id": "rp", "status": "Active", "updated": (now - 3600) * 1000},
            "fresh-peer": {"record_id": "rf", "status": "Idle", "updated": (now - 60) * 1000},
            "dead": {"record_id": "rd", "status": "Stale", "updated": 0}}
    updates, creates = m.plan_cpu_writes({}, rows, now, stale_seconds=900)
    assert updates == [{"record_id": "rp", "fields": {"状态": "Stale"}}]
    assert creates == []


def test_cpu_status_rules():
    assert m.cpu_status(cpu_snap("h", 1000, procs=1), 1000) == "Active"
    assert m.cpu_status(cpu_snap("h", 1000), 1000) == "Idle"
    assert m.cpu_status(cpu_snap("h", 1000, procs=1), 1000 + 10_000) == "Stale"


# --- bookings, own-stale timestamps, degraded-mode stamps ---

def test_name_only_booking_gets_its_start_pinned_and_then_expires():
    cards = {("n1", 0): card("r0", by="dave")}
    snaps = {"n1": {"fresh": True, "rows": [row(0)]}}
    updates, _ = m.plan_writes(snaps, cards, FM_F, NOW_MS)
    f = updates[0]["fields"]
    assert f["状态"] == "Reserved" and f["预定开始"] == NOW_MS
    # next day, with the pinned start read back from the board: expired + cleared
    later = m._end_of_day_ms(NOW_MS) + 60_000
    cards = {("n1", 0): card("r0", by="dave", frm=NOW_MS)}
    updates, _ = m.plan_writes(snaps, cards, FM_F, later)
    f = updates[0]["fields"]
    assert f["状态"] == "Free" and f["预定人"] == ""


def test_pinned_start_is_written_only_once():
    cards = {("n1", 0): card("r0", by="dave", frm=NOW_MS - 1000)}
    snaps = {"n1": {"fresh": True, "rows": [row(0)]}}
    updates, _ = m.plan_writes(snaps, cards, FM_F, NOW_MS)
    assert "预定开始" not in updates[0]["fields"]


def test_end_of_day_honours_the_configured_utc_offset():
    import datetime
    tz8 = datetime.timezone(datetime.timedelta(hours=8))
    ms = int(datetime.datetime(2026, 5, 1, 20, 0, tzinfo=datetime.timezone.utc).timestamp() * 1000)
    # 20:00 UTC is already 04:00 on 2 May in UTC+8, so the day ends on 2 May
    end = datetime.datetime.fromtimestamp(m._end_of_day_ms(ms, tz8) / 1000, tz8)
    assert (end.month, end.day, end.hour, end.minute) == (5, 2, 23, 59)


def test_own_stale_snapshot_keeps_the_last_seen_time():
    snaps = {"n1": {"fresh": False, "rows": [row(0)]}}
    updates, _ = m.plan_writes(snaps, {("n1", 0): card("r0")}, FM_F, NOW_MS)
    f = updates[0]["fields"]
    assert f["状态"] == "Stale" and "更新时间" not in f


def test_v3_grouped_stale_marks_do_not_refresh_the_update_time(monkeypatch):
    calls = []
    monkeypatch.setattr(m, "_api", lambda *a, **k: (calls.append(k.get("body")), {"code": 0})[1])
    monkeypatch.setattr(m, "time", type("T", (), {"strftime": staticmethod(lambda *a: "now"),
                                                   "localtime": staticmethod(lambda *a: None),
                                                   "sleep": staticmethod(lambda *a: None)}))
    m._LAST_SWEEP.pop("feishu", None)
    cards = {("n1", 0): card("r0", status="Free"), ("peer", 0): card("rp", status="Free")}
    updates = [{"record_id": "r0", "fields": {FM_F["status"]: "Free", FM_F["updated"]: 1}},
               {"record_id": "rp", "fields": {FM_F["status"]: "Stale"}}]
    m.push_v3_grouped(_grouped_backend(), "tok", updates, cards, 5000)
    patches = {tuple(c["record_id_list"]): c["patch"] for c in calls}
    assert FM_F["updated"] in patches[("r0",)]
    assert FM_F["updated"] not in patches[("rp",)]


def test_v1_read_follows_page_tokens(monkeypatch):
    pages = {"": {"items": [{"record_id": "a"}], "has_more": True, "page_token": "p2"},
             "p2": {"items": [{"record_id": "b"}], "has_more": False}}

    def fake_api(method, url, token=None, body=None, timeout=20):
        tok = url.split("page_token=")[1] if "page_token=" in url else ""
        return {"code": 0, "data": pages[tok]}
    monkeypatch.setattr(m, "_api", fake_api)
    items = m._v1_read_all(BACK, "tok", "tbl")
    assert [i["record_id"] for i in items] == ["a", "b"]


def test_v1_read_returns_none_when_quota_is_dead(monkeypatch):
    monkeypatch.setattr(m, "_api", lambda *a, **k: {"code": 99991403, "msg": "quota"})
    assert m._v1_read_all(BACK, "tok", "tbl") is None
