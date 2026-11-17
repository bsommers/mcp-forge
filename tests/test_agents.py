"""Argv/env construction per agent adapter. Nothing is executed."""
import pytest

from forge import agents, cli, harness


def test_registry():
    assert agents.get_agent("claude").verified and agents.get_agent("agy").verified
    assert not agents.get_agent("cursor").verified
    assert agents.get_agent("cursor-agent").name == "cursor"
    with pytest.raises(ValueError):
        agents.get_agent("nope")


def test_claude_argv():
    a = agents.get_agent("claude").build_argv("T", "/w")
    assert a[:2] == ["claude", "-p"] and a[-2:] == ["--", "T"]
    assert a[a.index("--allowedTools") + 1] == "Read,Write,Edit,Glob,Grep"
    assert a[a.index("--permission-mode") + 1] == "acceptEdits" and "--add-dir" in a


def test_agy_argv():
    a = agents.get_agent("agy").build_argv("T", "/w", system_prompt="SYS")
    assert a[0] == "agy" and a[a.index("--mode") + 1] == "accept-edits"
    assert a[a.index("--add-dir") + 1] == "/w" and "--sandbox" in a and "--print" in a
    assert a[-1] == "SYS\n\nT"


def test_cursor_argv():
    a = agents.get_agent("cursor").build_argv("T", "/w", system_prompt="SYS")
    assert a[:2] == ["cursor-agent", "-p"] and "--force" in a and "--trust" in a
    assert a[a.index("--sandbox") + 1] == "enabled" and a[a.index("--workspace") + 1] == "/w"
    assert a[-1] == "SYS\n\nT"


def test_env_passthrough_is_per_agent(tmp_path):
    src = {"PATH": "/p", "ANTHROPIC_API_KEY": "a", "CURSOR_API_KEY": "c", "GH_TOKEN": "g"}
    assert "CURSOR_API_KEY" not in agents.get_agent("claude").env(tmp_path, src)
    e = agents.get_agent("cursor").env(tmp_path, src)
    assert e["CURSOR_API_KEY"] == "c" and "ANTHROPIC_API_KEY" not in e and "GH_TOKEN" not in e
    assert agents.get_agent("agy").env(tmp_path, src).keys() == {"PATH", "HOME", "LANG"}


def test_docker_variant():
    ag = agents.get_agent("cursor")
    d = ag.docker_argv(ag.build_argv("T", "/w"), "/w", "img", environ={"CURSOR_API_KEY": "c", "ANTHROPIC_API_KEY": "a"})
    assert "--network=none" in d and "CURSOR_API_KEY" in d and "ANTHROPIC_API_KEY" not in d and "c" not in d
    assert d[d.index("img") + 1] == "cursor-agent"


def test_run_loop_agent_selection(tmp_path):
    import json
    from forge.harness import scaffold  # noqa: F401
    (tmp_path / "forge-manifest.json").write_text(json.dumps({"name": "x", "tools": []}))
    seen = {}

    class R:
        returncode = 0
        stderr = ""

    def run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        return R()

    harness.run_loop(tmp_path, None, max_iters=0, validator=lambda d: [], runner_exec=run, agent="cursor")
    assert seen["cmd"][0] == "cursor-agent"
    harness.run_loop(tmp_path, ["agy"], max_iters=0, validator=lambda d: [], runner_exec=run)
    assert seen["cmd"][0] == "agy" and "--mode" in seen["cmd"]


def test_cli_agent_flag():
    assert cli.build_parser().parse_args(["create", "--spec", "s", "--agent", "agy"]).agent == "agy"
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["create", "--spec", "s", "--agent", "zzz"])


def test_agy_argv_allowlists_commands_headless():
    import json
    argv = agents.get_agent("agy").build_argv("task", "/w")
    s = json.loads(argv[argv.index("--settings") + 1])
    allow = s["permissions"]["allow"]
    assert "command(cat)" in allow and "command(rm)" in allow
    assert not any(" " in a for a in allow)  # arg patterns don't match in agy
