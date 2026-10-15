"""Offline tests for board/cpu_agent.py (no /proc reads, no subprocesses)."""
import importlib.util
import json
import pathlib
import sys

spec = importlib.util.spec_from_file_location(
    "cpu_agent", pathlib.Path(__file__).parent.parent / "board" / "cpu_agent.py")
c = importlib.util.module_from_spec(spec)
sys.modules["cpu_agent"] = c
spec.loader.exec_module(c)


def test_parse_diskstats_whole_devices_only():
    text = ("259 0 nvme0n1 1 0 8 3 5 0 40 9 0 1234 12\n"
            "259 1 nvme0n1p1 1 0 8 3 5 0 40 9 0 999 12\n"
            "8 0 sda 1 0 8 3 5 0 40 9 0 55 12\n"
            "7 0 loop0 1 0 8 3 5 0 40 9 0 77 12\n")
    assert c.parse_diskstats(text) == {"nvme0n1": 1234, "sda": 55}


PS = """  PID COMMAND
  101 /usr/bin/python3 -m vllm.entrypoints.openai.api_server --model /m/X
  102 VLLM::EngineCore
  103 torchrun --nproc_per_node 8 train.py
  104 /usr/bin/node_exporter --collector.filesystem.fs-types-exclude=^(overlay|tmpfs)$
  105 python3 /root/gpu-board/cpu_agent.py --loop 60
  106 bash
"""


def test_workload_matching_skips_itself_and_the_overlay_false_positive():
    procs = c.match_workloads(PS)
    assert [p for p, _ in procs] == ["101", "102", "103"]


def test_summary_counts_by_label():
    summary = c.summarize(c.match_workloads(PS))
    assert "· vLLM 推理服务" in summary and "· vLLM worker" in summary
    assert "分布式训练 (torchrun)" in summary


def test_custom_patterns_fall_back_to_the_raw_pattern_as_label():
    procs = c.match_workloads("  7 my_trainer --cfg a.yaml\n", patterns=("my_trainer",))
    assert c.summarize(procs, patterns=("my_trainer",)) == "· my_trainer"


def test_load_peak_keeps_the_last_hour_only():
    c._LOAD_HIST.clear()
    assert c.load_peak_1h(5.0, 0) == 5.0
    assert c.load_peak_1h(1.0, 1000) == 5.0
    assert c.load_peak_1h(2.0, 4000) == 2.0     # the 5.0 sample is > 1 h old


def test_write_snap_is_atomic_and_stamped(tmp_path):
    dst = c.write_snap({"host": "box-a", "ts": 1, "procs": 0}, out_dir=str(tmp_path))
    data = json.loads(pathlib.Path(dst).read_text())
    assert data["host"] == "box-a" and "recv_ts" in data
    assert not list(tmp_path.glob(".*.tmp"))
