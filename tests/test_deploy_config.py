"""deploy.sh config handling, run for real against a throwaway config (no ssh)."""
import pathlib
import subprocess

DEPLOY = pathlib.Path(__file__).parent.parent / "deploy" / "deploy.sh"


def run(conf_text, *args, tmp_path):
    conf = tmp_path / "targets.conf"
    conf.write_text(conf_text)
    return subprocess.run(["bash", str(DEPLOY), *args], capture_output=True, text=True,
                          env={"CONFIG": str(conf), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})


def test_targets_lists_names_in_order(tmp_path):
    r = run("target a host-a node-a tblA\ntarget b host-b node-b tblB\n", "targets",
            tmp_path=tmp_path)
    assert r.returncode == 0 and r.stdout.split() == ["a", "b"]


def test_example_config_parses():
    r = subprocess.run(["bash", str(DEPLOY), "targets"], capture_output=True, text=True,
                       env={"CONFIG": str(DEPLOY.parent / "targets.example.conf"),
                            "PATH": "/usr/bin:/bin", "HOME": "/nonexistent"})
    assert r.returncode == 0 and r.stdout.split() == ["h200-a", "h200-b", "b200-a"]


def test_malformed_target_line_is_rejected(tmp_path):
    r = run("target a host-a\n", "targets", tmp_path=tmp_path)
    assert r.returncode != 0 and "needs 4 fields" in r.stderr


def test_unknown_target_is_rejected(tmp_path):
    r = run("target a host-a node-a tblA\n", "nope", "status", tmp_path=tmp_path)
    assert r.returncode != 0 and "unknown target" in r.stderr


def test_deploy_refuses_without_a_secret_file(tmp_path):
    r = run("FEISHU_APP_ID=cli_x\nFEISHU_BASE_TOKEN=b\ntarget a host-a node-a tblA\n", "a",
            tmp_path=tmp_path)
    assert r.returncode != 0 and "FEISHU_APP_SECRET" in r.stderr


def test_missing_config_explains_what_to_do(tmp_path):
    r = subprocess.run(["bash", str(DEPLOY), "targets"], capture_output=True, text=True,
                       env={"CONFIG": str(tmp_path / "none.conf"), "PATH": "/usr/bin:/bin"})
    assert r.returncode != 0 and "targets.example.conf" in r.stderr


def fake_ssh_env(tmp_path, line):
    """PATH with an `ssh` that ignores its input and prints `line`."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text("#!/bin/bash\ncat > /dev/null\nprintf '%s\\n' " + repr(line) + "\n")
    ssh.chmod(0o755)
    conf = tmp_path / "targets.conf"
    conf.write_text("target a host-a node-a tblA\n")
    return {"CONFIG": str(conf), "PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path)}


def check(tmp_path, line):
    return subprocess.run(["bash", str(DEPLOY), "a", "check"], capture_output=True, text=True,
                          env=fake_ssh_env(tmp_path, line))


def test_check_healthy_down_unreachable(tmp_path):
    for sub, line, rc, word in (("h", "sup=1 snap=10 mlog=20", 0, "healthy"),
                                ("d", "sup=0 snap=10 mlog=20", 1, "down"),
                                ("o", "sup=1 snap=900 mlog=20", 1, "down"),
                                ("u", "garbage", 2, "unreachable")):
        d = tmp_path / sub
        d.mkdir()
        r = check(d, line)
        assert (r.returncode, r.stdout.split()[0]) == (rc, word), (line, r.stdout, r.stderr)


def test_check_never_evaluates_what_the_box_sends(tmp_path):
    marker = tmp_path / "pwned"
    r = check(tmp_path, f"sup=1 snap=5 mlog=5; touch {marker}")
    assert r.stdout.startswith("unreachable") and not marker.exists()


def test_secret_refuses_without_a_terminal(tmp_path):
    r = subprocess.run(["bash", str(DEPLOY), "secret"], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL,
                       env={"CONFIG": str(tmp_path / "none.conf"), "PATH": "/usr/bin:/bin",
                            "HOME": str(tmp_path)})
    assert r.returncode != 0 and "terminal" in r.stderr
