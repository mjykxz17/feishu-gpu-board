"""Offline tests for the node agent's served-model detection (no /proc, no network).

The cmdlines below are real launch commands captured from production GPU
nodes, with paths and names anonymised and irrelevant tails cut.
"""
import importlib.util
import pathlib
import sys

spec = importlib.util.spec_from_file_location(
    "node_agent",
    pathlib.Path(__file__).parent.parent / "board" / "node_agent.py")
a = importlib.util.module_from_spec(spec)
sys.modules["node_agent"] = a
spec.loader.exec_module(a)

# --- real cmdlines, as argv lists (as /proc/<pid>/cmdline splits them) ---

VLLM_SERVE = (
    "/home/alice/venv/bin/python /home/alice/venv/bin/vllm serve "
    "/data/models/qwen3_8b --served-model-name Qwen/Qwen3-8B "
    "--host 0.0.0.0 --port 18090 --tensor-parallel-size 1 --dtype bfloat16"
).split()

VLLM_ENTRYPOINTS = (
    "/opt/envs/vllm/bin/python "
    "-m vllm.entrypoints.openai.api_server "
    "--model /data/models/DeepSeek-V3 "
    "--served-model-name deepseek-v3 --host 0.0.0.0 --port 8002 --trust-remote-code"
).split()

# nvidia-smi reports these proc-titles, not the real command line.
WORKER_TITLE = ["VLLM::Worker_TP0_EP0"]
ENGINE_TITLE = ["VLLM::EngineCore"]

TRAINING = ("/usr/bin/python3 -m torch.distributed.run --nproc_per_node 8 "
            "/home/bob/LLaMA-Factory/src/train.py --stage sft").split()


def resolver_from(tree):
    """tree: pid -> (argv, ppid). Returns a reader in the shape the agent wants."""
    def read(pid):
        return tree.get(pid, (None, None))
    return read


# --- parse_server ---

def test_parses_vllm_serve_positional_model_and_served_name():
    got = a.parse_server(VLLM_SERVE)
    assert got == {"model": "Qwen/Qwen3-8B", "host": "0.0.0.0", "port": 18090}


def test_parses_vllm_entrypoints_module_form():
    got = a.parse_server(VLLM_ENTRYPOINTS)
    assert got == {"model": "deepseek-v3", "host": "0.0.0.0", "port": 8002}


def test_served_model_name_wins_over_model_path():
    argv = "python -m vllm.entrypoints.openai.api_server --model /models/Foo-8B --port 9000".split()
    assert a.parse_server(argv)["model"] == "Foo-8B"  # basename of the path


def test_bare_repo_id_is_not_basenamed():
    argv = "python -m vllm.entrypoints.openai.api_server --model Qwen/Qwen3-8B".split()
    assert a.parse_server(argv)["model"] == "Qwen/Qwen3-8B"


def test_equals_form_arguments():
    argv = "python -m vllm.entrypoints.openai.api_server --model=/m/X --port=7001".split()
    got = a.parse_server(argv)
    assert got["model"] == "X" and got["port"] == 7001


def test_port_defaults_per_engine():
    assert a.parse_server("python -m vllm.entrypoints.openai.api_server --model /m/X"
                          .split())["port"] == 8000
    assert a.parse_server("python -m sglang.launch_server --model-path /m/X"
                          .split())["port"] == 30000


def test_sglang_model_path_flag():
    argv = "python -m sglang.launch_server --model-path /models/Bar-32B --port 30001".split()
    got = a.parse_server(argv)
    assert got == {"model": "Bar-32B", "host": "", "port": 30001}


def test_non_serving_process_is_not_a_server():
    assert a.parse_server(TRAINING) is None
    assert a.parse_server(WORKER_TITLE) is None


# --- find_server: the parent walk ---

def test_walks_up_from_worker_proc_title_to_the_api_server():
    tree = {100: (WORKER_TITLE, 90), 90: (ENGINE_TITLE, 80),
            80: (VLLM_ENTRYPOINTS, 1), 1: (["/sbin/init"], 0)}
    assert a.find_server(100, resolver_from(tree)) == VLLM_ENTRYPOINTS


def test_walk_stops_at_init_without_a_match():
    tree = {100: (TRAINING, 1), 1: (["/sbin/init"], 0)}
    assert a.find_server(100, resolver_from(tree)) is None


def test_walk_is_bounded_even_if_the_chain_loops():
    tree = {100: (WORKER_TITLE, 101), 101: (WORKER_TITLE, 100)}
    assert a.find_server(100, resolver_from(tree)) is None


def test_walk_tolerates_a_pid_that_exited_mid_walk():
    tree = {100: (WORKER_TITLE, 90)}  # 90 is gone
    assert a.find_server(100, resolver_from(tree)) is None


# --- endpoint formatting ---

def test_endpoint_uses_node_ip_when_bound_to_all_interfaces():
    assert a.format_endpoint({"host": "0.0.0.0", "port": 8002}, "10.0.0.64") \
        == "10.0.0.64:8002"


def test_loopback_bound_server_is_flagged_local_only():
    assert a.format_endpoint({"host": "127.0.0.1", "port": 8000}, "10.0.0.64") \
        == "127.0.0.1:8000 (local only)"


def test_explicit_host_is_kept_verbatim():
    assert a.format_endpoint({"host": "10.0.0.64", "port": 8000}, "10.0.0.99") \
        == "10.0.0.64:8000"


def test_missing_node_ip_falls_back_to_port_only():
    assert a.format_endpoint({"host": "0.0.0.0", "port": 8000}, "") == ":8000"


# --- parse(): whole-snapshot wiring ---

GPU_CSV = ("0, GPU-aaa, 90, 136000, 143771\n"
           "1, GPU-bbb, 85, 136000, 143771\n"
           "2, GPU-ccc, 0, 0, 143771\n")


def test_serving_cards_carry_model_and_endpoint_and_idle_cards_stay_blank():
    proc_csv = ("GPU-aaa, 100, 136000, VLLM::Worker_TP0_EP0\n"
                "GPU-bbb, 101, 136000, VLLM::Worker_TP1_EP1\n")
    tree = {100: (WORKER_TITLE, 80), 101: (WORKER_TITLE, 80),
            80: (VLLM_ENTRYPOINTS, 1), 1: (["/sbin/init"], 0)}
    rows = a.parse(GPU_CSV, proc_csv, 2048,
                   resolver=resolver_from(tree), node_ip="10.0.0.64")
    assert [r["model"] for r in rows] == ["deepseek-v3", "deepseek-v3", ""]
    assert [r["endpoint"] for r in rows] == ["10.0.0.64:8002", "10.0.0.64:8002", ""]


def test_two_servers_on_one_card_are_joined_and_deduped():
    proc_csv = ("GPU-aaa, 100, 68000, VLLM::Worker_TP0_EP0\n"
                "GPU-aaa, 200, 68000, VLLM::Worker_TP0_EP0\n"
                "GPU-aaa, 201, 68000, VLLM::Worker_TP0_EP0\n")
    tree = {100: (WORKER_TITLE, 80), 80: (VLLM_ENTRYPOINTS, 1),
            200: (WORKER_TITLE, 81), 201: (WORKER_TITLE, 81),
            81: (VLLM_SERVE, 1), 1: (["/sbin/init"], 0)}
    rows = a.parse(GPU_CSV, proc_csv, 2048,
                   resolver=resolver_from(tree), node_ip="10.0.0.64")
    assert rows[0]["model"] == "deepseek-v3, Qwen/Qwen3-8B"
    assert rows[0]["endpoint"] == "10.0.0.64:8002, 10.0.0.64:18090"


def test_training_workload_reports_busy_with_no_model():
    proc_csv = "GPU-aaa, 100, 136000, python3\n"
    tree = {100: (TRAINING, 1), 1: (["/sbin/init"], 0)}
    rows = a.parse(GPU_CSV, proc_csv, 2048,
                   resolver=resolver_from(tree), node_ip="10.0.0.64")
    assert rows[0]["status"] == "Busy"
    assert rows[0]["model"] == "" and rows[0]["endpoint"] == ""


def test_parse_without_a_resolver_keeps_the_old_shape_working():
    rows = a.parse(GPU_CSV, "GPU-aaa, 100, 136000, python3\n", 2048)
    assert rows[0]["status"] == "Busy" and rows[0]["user_proc"] == "python3:100"
    assert rows[0]["model"] == "" and rows[0]["endpoint"] == ""


# --- SGLang ---
# Three launch forms are in use at once, and they must not all be matched by a
# loose "sglang" substring: see test_ray_rollout_engine_* below for why.

SGLANG_CONSOLE = (            # the `sglang` console script
    "/usr/bin/python3 /usr/local/bin/sglang serve "
    "--model-path /models/Qwen3-30B-A3B --reasoning-parser qwen3 "
    "--tool-call-parser qwen3_coder --tp 1 --dp 4 --context-length 262144 "
    "--mem-fraction-static 0.90 --host 0.0.0.0 --port 30000"
).split()

SGLANG_CONSOLE_NAMED = (      # console form carrying a served name
    "/usr/bin/python3 /usr/local/bin/sglang serve --model-path /model "
    "--served-model-name Qwen3-30B-A3B-sft-v2 "
    "--host 0.0.0.0 --port 8010 --tp-size 1 --dp-size 4 --trust-remote-code"
).split()

SGLANG_MODULE = (             # the -m module form
    "python3 -m sglang.launch_server --trust-remote-code --model-path /model "
    "--served-model-name DeepSeek-V3 --tp 4 "
    "--moe-runner-backend marlin --host 0.0.0.0 --port 30000"
).split()

SGL_SCHED = ["sglang::scheduler_DP0"]
SGL_SCHED_EP = ["sglang::scheduler_DP0_TP0_EP0"]
SGL_DP_CTRL = ["sglang::data_parallel_controller"]
SPAWN_MAIN = ("/usr/bin/python3 -c from multiprocessing.spawn import spawn_main; "
              "spawn_main(tracker_fd=114, pipe_handle=116) --multiprocessing-fork").split()
RAY_SGLANG_ENGINE = ["ray::SGLangEngine"]
GRPO_SCRIPT = ["bash", "/home/carol/grpo/scripts/train_grpo.sh"]


def test_parses_the_sglang_console_script_form():
    assert a.parse_server(SGLANG_CONSOLE) == {
        "model": "Qwen3-30B-A3B", "host": "0.0.0.0", "port": 30000}


def test_console_form_served_model_name_wins():
    got = a.parse_server(SGLANG_CONSOLE_NAMED)
    assert got["model"] == "Qwen3-30B-A3B-sft-v2" and got["port"] == 8010


def test_parses_the_sglang_module_form():
    assert a.parse_server(SGLANG_MODULE) == {
        "model": "DeepSeek-V3", "host": "0.0.0.0", "port": 30000}


def test_console_form_without_a_port_defaults_to_sglangs_own_default():
    argv = "/usr/bin/python3 /usr/local/bin/sglang serve --model-path /m/X".split()
    assert a.parse_server(argv)["port"] == 30000


def test_walks_from_an_sglang_scheduler_through_the_dp_controller():
    """scheduler -> data_parallel_controller -> the serve command."""
    tree = {10: (SGL_SCHED, 11), 11: (SGL_DP_CTRL, 12),
            12: (SGLANG_CONSOLE, 13), 13: (["/usr/bin/containerd-shim-runc-v2"], 1)}
    assert a.find_server(10, resolver_from(tree)) == SGLANG_CONSOLE


def test_containerised_server_is_still_found_through_the_shim():
    tree = {10: (["sglang::scheduler_TP0"], 11), 11: (SGLANG_MODULE, 12),
            12: (["/usr/bin/containerd-shim-runc-v2"], 1)}
    assert a.find_server(10, resolver_from(tree)) == SGLANG_MODULE


# The trap: a bare "sglang" pattern would match the proc-titles below at hop 0
# and publish an endpoint for an RL rollout engine that serves nothing to
# anyone. Ray GRPO with an SGLang rollout engine has exactly this shape and
# must keep both columns blank.

def test_ray_rollout_engine_is_not_advertised_as_a_served_model():
    tree = {10: (SGL_SCHED_EP, 11), 11: (SGL_DP_CTRL, 12), 12: (SPAWN_MAIN, 13),
            13: (RAY_SGLANG_ENGINE, 14), 14: (GRPO_SCRIPT, 1)}
    assert a.find_server(10, resolver_from(tree)) is None


def test_sglang_proc_titles_are_never_servers_on_their_own():
    for title in (SGL_SCHED, SGL_SCHED_EP, SGL_DP_CTRL, RAY_SGLANG_ENGINE):
        assert a.parse_server(title) is None, title


def test_sglang_cards_carry_the_endpoint_end_to_end():
    proc_csv = ("GPU-aaa, 10, 130000, sglang::scheduler_DP0\n"
                "GPU-bbb, 11, 130000, sglang::scheduler_DP1\n")
    tree = {10: (SGL_SCHED, 20), 11: (["sglang::scheduler_DP1"], 20),
            20: (SGL_DP_CTRL, 21), 21: (SGLANG_CONSOLE, 1)}
    rows = a.parse(GPU_CSV, proc_csv, 2048,
                   resolver=resolver_from(tree), node_ip="10.0.0.29")
    assert rows[0]["model"] == "Qwen3-30B-A3B"
    assert rows[0]["endpoint"] == "10.0.0.29:30000"
    assert rows[1]["endpoint"] == "10.0.0.29:30000"


# --- other engines (best effort) ---

def test_lmdeploy_api_server_positional_model_and_server_flags():
    argv = ("lmdeploy serve api_server /data/models/internlm2-7b "
            "--server-name 0.0.0.0 --server-port 23334 --model-name internlm2").split()
    assert a.parse_server(argv) == {"model": "internlm2", "host": "0.0.0.0", "port": 23334}
    bare = "lmdeploy serve api_server /data/models/internlm2-7b".split()
    assert a.parse_server(bare) == {"model": "internlm2-7b", "host": "", "port": 23333}


def test_trtllm_serve_defaults_to_localhost():
    got = a.parse_server("trtllm-serve /data/engines/llama3-8b --port 8100".split())
    assert got == {"model": "llama3-8b", "host": "localhost", "port": 8100}
    assert a.format_endpoint(got, "10.0.0.5") == "localhost:8100 (local only)"
    sub = a.parse_server("trtllm-serve serve meta-llama/Llama-3-8B --host 0.0.0.0".split())
    assert sub == {"model": "meta-llama/Llama-3-8B", "host": "0.0.0.0", "port": 8000}


def test_tgi_launcher_model_id_and_hostname():
    argv = "text-generation-launcher --model-id Qwen/Qwen3-8B --hostname 0.0.0.0".split()
    assert a.parse_server(argv) == {"model": "Qwen/Qwen3-8B", "host": "0.0.0.0", "port": 3000}


def test_serve_patterns_env_extends_the_builtins(monkeypatch):
    import importlib
    monkeypatch.setenv("SERVE_PATTERNS", "myserver --listen, mycorp-serve")
    spec2 = importlib.util.spec_from_file_location(
        "node_agent_env", pathlib.Path(__file__).parent.parent / "board" / "node_agent.py")
    b = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(b)
    assert "mycorp-serve" in b.SERVE_PATTERNS and "vllm serve" in b.SERVE_PATTERNS
    assert b.parse_server("mycorp-serve --model /m/X --port 9100".split())["port"] == 9100
    assert b.parse_server(VLLM_SERVE) is not None    # built-ins still recognised
