"""Offline tests for board/setup_tables.py: the schema it creates must be
exactly what master.py writes."""
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "board" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


s = load("setup_tables")
m = load("master")


def names(fields):
    return [f["field_name"] for f in fields]


def test_gpu_table_has_every_column_the_master_writes():
    assert set(m.FMAP.values()) <= set(names(s.gpu_fields()))


def test_gpu_table_primary_column_is_the_node():
    assert names(s.gpu_fields())[0] == m.FMAP["node"]


def test_reservation_columns_are_optional():
    without = set(names(s.gpu_fields(reservations=False)))
    assert not {"预定人", "预定开始", "预定结束"} & without
    # the master copes: it filters its writes to the columns a table has
    assert set(m.FMAP.values()) - without == {"预定人", "预定开始", "预定结束"}


def test_cpu_table_has_every_column_the_master_writes():
    assert set(m.CPU_FMAP.values()) <= set(names(s.cpu_fields()))
    assert names(s.cpu_fields())[0] == m.CPU_FMAP["host"]


def test_status_options_cover_every_status_the_master_sets():
    status = next(f for f in s.gpu_fields() if f["field_name"] == "状态")
    assert {o["name"] for o in status["property"]["options"]} == {"Free", "Busy", "Reserved", "Stale"}
    cpu = next(f for f in s.cpu_fields() if f["field_name"] == "状态")
    assert {o["name"] for o in cpu["property"]["options"]} == {"Active", "Idle", "Stale"}


def test_view_filters_reference_real_columns_and_options():
    cols = {f["field_name"]: f for f in s.gpu_fields()}
    for view, conds in s.GPU_VIEWS.items():
        for col, _op, value in conds:
            assert col in cols, view
            if cols[col]["type"] == s.SINGLE_SELECT:
                assert value in {o["name"] for o in cols[col]["property"]["options"]}, view


def test_condition_resolves_option_names_to_ids():
    fields = {"状态": {"field_id": "fld1", "type": s.SINGLE_SELECT,
                       "property": {"options": [{"id": "optF", "name": "Free"}]}},
              "利用率": {"field_id": "fld2", "type": s.NUMBER}}
    assert s._condition(fields, "状态", "is", "Free") == {
        "field_id": "fld1", "operator": "is", "value": json.dumps(["optF"])}
    assert s._condition(fields, "利用率", "is", 0)["value"] == '["0"]'


def test_dry_run_prints_schema_without_credentials(capsys, monkeypatch):
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    s.main(["--gpu-table", "H200", "--cpu-table", "CPU", "--dry-run"])
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines()]
    assert [(l["kind"], l["name"]) for l in lines] == [("gpu", "H200"), ("cpu", "CPU")]
