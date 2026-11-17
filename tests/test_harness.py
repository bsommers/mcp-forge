import json
from types import SimpleNamespace as R

import pytest

from forge import harness, prompt


@pytest.fixture
def tpl(tmp_path):
    t = tmp_path / "tpl"
    (t / "src" / "tools").mkdir(parents=True)
    (t / "tests").mkdir()
    (t / "src" / "index.py").write_text("LOCKED\n")
    (t / "Dockerfile").write_text("FROM x\n")
    return t


MAN = {"name": "demo", "tools": []}


def test_scaffold(tpl, tmp_path):
    out = harness.scaffold(MAN, tmp_path / "o", template_dir=tpl)
    assert (out / "src/index.py").read_text() == "LOCKED\n"
    assert json.loads((out / "forge-manifest.json").read_text())["name"] == "demo"


def test_scaffold_missing_template(tmp_path):
    with pytest.raises(FileNotFoundError):
        harness.scaffold(MAN, tmp_path / "o", template_dir=tmp_path / "nope")


def test_loop_heals_and_reverts_violations(tpl, tmp_path):
    out = harness.scaffold(MAN, tmp_path / "o", template_dir=tpl)
    prompts = []

    def agent(p, cwd):
        prompts.append(p)
        (cwd / "src/tools/a.py").write_text("ok")
        (cwd / "src/index.py").write_text("HACKED")
        (cwd / "evil.sh").write_text("x")
        (cwd / "Dockerfile").unlink()

    calls = []

    def validator(d):
        calls.append(1)
        return [R(step="pytest", ok=len(calls) >= 2, stderr="boom")]

    res = harness.run_loop(out, agent, validator=validator)
    assert res.ok and res.iterations == 2
    assert (out / "src/index.py").read_text() == "LOCKED\n"
    assert (out / "Dockerfile").exists() and not (out / "evil.sh").exists()
    assert (out / "src/tools/a.py").exists()
    assert "boom" in prompts[1] and "reverted" in prompts[1]
    assert any("index.py" in v for v in res.violations)


def test_loop_gives_up(tpl, tmp_path):
    out = harness.scaffold(MAN, tmp_path / "o", template_dir=tpl)
    res = harness.run_loop(out, lambda p, c: None, max_iters=3,
                           validator=lambda d: [R(step="s", ok=False, stderr="e")])
    assert not res.ok and res.iterations == 4


def test_skipped_steps_not_failures(tpl, tmp_path):
    out = harness.scaffold(MAN, tmp_path / "o", template_dir=tpl)
    res = harness.run_loop(out, lambda p, c: None, allow_skip=True,
                           validator=lambda d: [R(step="ruff", ok=False, stderr="", skipped=True)])
    assert res.ok


def test_subprocess_agent(tpl, tmp_path):
    out = harness.scaffold(MAN, tmp_path / "o", template_dir=tpl)
    res = harness.run_loop(out, ["true"], validator=lambda d: [R(step="s", ok=True, stderr="")])
    assert res.ok


def test_prompt():
    assert "src/tools/" in prompt.SYSTEM_PROMPT and "verb_noun" in prompt.SYSTEM_PROMPT
    assert "x" in prompt.build_feedback_prompt([("s", "x")])


def _scaf(tpl, tmp_path):
    return harness.scaffold(MAN, tmp_path / "o", template_dir=tpl)


def ok_val(d):
    return [R(step="s", ok=True, stderr="")]


def test_agent_nonzero_returncode_fails(tpl, tmp_path):
    out = _scaf(tpl, tmp_path)
    res = harness.run_loop(out, lambda p, c: R(returncode=1, stderr="crash"), max_iters=1, validator=ok_val)
    assert not res.ok and res.agent_errors and "crash" in res.agent_errors[0]


def test_symlink_not_written_through(tpl, tmp_path):
    out = _scaf(tpl, tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("SAFE")

    def agent(p, cwd):
        (cwd / "src/index.py").unlink()
        (cwd / "src/index.py").symlink_to(victim)
        (cwd / "src/tools/ln.py").symlink_to(victim)

    res = harness.run_loop(out, agent, max_iters=0, validator=ok_val)
    assert victim.read_text() == "SAFE"
    assert not (out / "src/index.py").is_symlink()
    assert (out / "src/index.py").read_text() == "LOCKED\n"
    assert not (out / "src/tools/ln.py").is_symlink() and not (out / "src/tools/ln.py").exists()
    assert any("symlink" in v for v in res.violations)


def test_git_and_venv_removed_outside_writable(tpl, tmp_path):
    out = _scaf(tpl, tmp_path)

    def agent(p, cwd):
        (cwd / ".git/hooks").mkdir(parents=True)
        (cwd / ".git/hooks/pre-commit").write_text("evil")
        (cwd / "src/__pycache__").mkdir()
        (cwd / "src/__pycache__/x.pyc").write_text("x")

    res = harness.run_loop(out, agent, max_iters=0, validator=ok_val)
    assert not (out / ".git").exists() and not (out / "src/__pycache__").exists()
    assert any(".git" in v for v in res.violations)


def test_tools_init_locked(tpl, tmp_path):
    (tpl / "src/tools/__init__.py").write_text("ORIG\n")
    out = _scaf(tpl, tmp_path)
    res = harness.run_loop(out, lambda p, c: (c / "src/tools/__init__.py").write_text("BAD"),
                           max_iters=0, validator=ok_val)
    assert (out / "src/tools/__init__.py").read_text() == "ORIG\n"
    assert any("__init__" in v for v in res.violations)


def test_final_rehash_after_loop(tpl, tmp_path):
    out = _scaf(tpl, tmp_path)

    def validator(d):  # tampering after enforcement, during validation
        (d / "src/index.py").write_text("LATE")
        return ok_val(d)

    res = harness.run_loop(out, lambda p, c: None, validator=validator)
    assert (out / "src/index.py").read_text() == "LOCKED\n"
    assert not res.ok and any("LATE" not in v and "index.py" in v for v in res.violations)


def test_scrubbed_env(tmp_path):
    env = harness.scrubbed_env(tmp_path, {"PATH": "/p", "LANG": "x", "GH_TOKEN": "t", "HOME": "/h",
                                          "ANTHROPIC_API_KEY": "k"})
    assert env == {"PATH": "/p", "LANG": "x", "HOME": str(tmp_path), "ANTHROPIC_API_KEY": "k"}


def test_default_runner_env_and_flags(tpl, tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "secret")
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return R(returncode=0, stderr="")

    out = _scaf(tpl, tmp_path)
    harness.run_loop(out, ["claude", "-p"], max_iters=0, validator=ok_val, runner_exec=fake_run)
    assert "GH_TOKEN" not in seen["env"] and seen["env"]["HOME"] != str(__import__("os").environ.get("HOME"))
    for flag in ("--allowedTools", "--permission-mode", "--add-dir"):
        assert flag in seen["cmd"]
    assert seen["cmd"][-2] == "--"


def test_docker_cmd():
    cmd = harness.build_docker_cmd(["claude", "-p", "x"], "/w", "img", environ={"ANTHROPIC_API_KEY": "k", "GH_TOKEN": "t"})
    assert cmd[:3] == ["docker", "run", "--rm"]
    for f in ("--network=none", "--read-only", "--cap-drop=ALL", "--user"):
        assert f in cmd
    assert "ANTHROPIC_API_KEY" in cmd and "GH_TOKEN" not in cmd and "k" not in cmd
    assert cmd[-4:] == ["img", "claude", "-p", "x"] or cmd[-4] == "img"


def test_docker_sandbox_runner(tpl, tmp_path):
    seen = {}
    out = _scaf(tpl, tmp_path)
    harness.run_loop(out, ["agent"], max_iters=0, validator=ok_val, sandbox="docker",
                     runner_exec=lambda cmd, **kw: seen.setdefault("cmd", cmd) and R(returncode=0))
    assert seen["cmd"][0] == "docker" and "--network=none" in seen["cmd"]


def test_skipped_is_failure_by_default_and_allow_skip_passed(tpl, tmp_path):
    out = _scaf(tpl, tmp_path)
    got = []

    def validator(d, allow_skip=False):
        got.append(allow_skip)
        return [R(step="ruff", ok=True, stderr="", skipped=True)]

    assert not harness.run_loop(out, lambda p, c: None, max_iters=0, validator=validator).ok
    assert harness.run_loop(out, lambda p, c: None, max_iters=0, validator=validator, allow_skip=True).ok
    assert got == [False, True]


def test_prompt_untrusted_and_truncated():
    m = {"name": "d", "description": "ignore previous " + "A" * 5000, "tools": [{"description": "B" * 1000}]}
    p = prompt.build_task_prompt(m)
    assert prompt.SPEC_BEGIN in p and prompt.SPEC_END in p and "untrusted" in p.lower()
    assert len(p) < 2500 and "truncated" in p
    assert "UNTRUSTED" in prompt.SYSTEM_PROMPT


def test_scaffold_refuses_nonempty_unless_force(tpl, tmp_path):
    o = tmp_path / "o"
    o.mkdir()
    (o / "stale.txt").write_text("x")
    with pytest.raises(FileExistsError):
        harness.scaffold(MAN, o, template_dir=tpl)
    harness.scaffold(MAN, o, template_dir=tpl, force=True)
    assert (o / "src/index.py").exists()


def test_scaffold_refuses_out_inside_template(tpl):
    with pytest.raises(ValueError):
        harness.scaffold(MAN, tpl / "sub" / "o", template_dir=tpl)


def test_timeout_is_violation_and_failure(tpl, tmp_path):
    import subprocess
    out = _scaf(tpl, tmp_path)

    def agent(p, cwd):
        (cwd / "src/index.py").write_text("HACK")
        raise subprocess.TimeoutExpired("claude", 5)

    res = harness.run_loop(out, agent, validator=ok_val)
    assert not res.ok and any("timed out" in v for v in res.violations)
    assert (out / "src/index.py").read_text() == "LOCKED\n"
