"""Tests for forge.package and forge.cli. No docker, gh or git is ever executed."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from forge import cli, package  # noqa: E402

MANIFEST = {
    "name": "petstore",
    "description": "Pet store API",
    "source_type": "openapi",
    "base_url": "https://api.example.com",
    "auth": {"type": "api_key", "env_var": "PETSTORE_API_KEY", "header": "X-Api-Key"},
    "env_vars": ["PETSTORE_BASE_URL"],
    "tools": [
        {"name": "get_pet", "description": "Get a pet", "method": "GET", "path": "/pets/{id}",
         "parameters": [], "destructive": False},
        {"name": "delete_pet", "description": "Delete a pet", "method": "DELETE", "path": "/pets/{id}",
         "parameters": [], "destructive": True},
    ],
}


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path]] = []

    def __call__(self, argv, cwd):
        self.calls.append((list(argv), Path(cwd)))


def _server_dir(tmp_path: Path) -> Path:
    d = tmp_path / "forged-mcp-petstore"
    d.mkdir()
    (d / "forge-manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    return d


# ---- client_configs -------------------------------------------------------

def test_client_configs_has_three_targets_with_same_block():
    cfg = package.client_configs(MANIFEST, server_dir="/srv/forged-mcp-petstore")
    assert set(cfg) == {"claude_desktop", "cursor", "claude_code"}
    assert cfg["claude_desktop"] == cfg["cursor"] == cfg["claude_code"]
    assert set(cfg["cursor"]["mcpServers"]) == {"petstore", "petstore-docker"}
    assert cfg["cursor"]["mcpServers"]["petstore"]["env"] == {
        "PETSTORE_API_KEY": "<PETSTORE_API_KEY>", "PETSTORE_BASE_URL": "<PETSTORE_BASE_URL>"}


def test_python_entry_points_at_server_dir():
    cfg = package.client_configs(MANIFEST, server_dir="/srv/forged-mcp-petstore")
    entry = cfg["cursor"]["mcpServers"]["petstore"]
    assert "/srv/forged-mcp-petstore" in entry["args"]
    assert "src/index.py" in entry["args"]


def test_docker_entry_passes_env_by_name_never_value(monkeypatch):
    monkeypatch.setenv("PETSTORE_API_KEY", "super-secret-value")
    cfg = package.client_configs(MANIFEST, image="forged-mcp-petstore:latest")
    entry = cfg["claude_code"]["mcpServers"]["petstore-docker"]
    assert entry["command"] == "docker"
    args = entry["args"]
    pairs = {(args[i], args[i + 1]) for i in range(len(args) - 1) if args[i] == "-e"}
    assert pairs == {("-e", "PETSTORE_API_KEY"), ("-e", "PETSTORE_BASE_URL")}
    assert "super-secret-value" not in json.dumps(cfg)
    assert entry["args"][-1] == "forged-mcp-petstore:latest"


def test_manifest_dict_accepts_model_like_object():
    class FakeModel:
        def model_dump(self):
            return dict(MANIFEST)

    assert package.manifest_dict(FakeModel())["name"] == "petstore"
    with pytest.raises(TypeError):
        package.manifest_dict(object())


# ---- build_image ----------------------------------------------------------

def test_build_image_default_is_dry_run(tmp_path):
    rec = Recorder()
    d = _server_dir(tmp_path)
    cmds = package.build_image(d, runner=rec)
    assert rec.calls == []
    assert cmds == [["bash", "scripts/vendor_wheels.sh"],
                    ["docker", "build", "--network=none", "-t", "forged-mcp-petstore:latest", "."]]


def test_build_image_network_none_only_with_vendored_wheels(tmp_path):
    d = _server_dir(tmp_path)
    (d / "wheels").mkdir()
    assert package.build_image(d) == [["docker", "build", "--network=none", "-t", "forged-mcp-petstore:latest", "."]]
    assert "--network=host" in package.build_image(d, network="host")[0]


def test_build_image_execute_uses_runner_in_server_dir(tmp_path):
    rec = Recorder()
    d = _server_dir(tmp_path)
    package.build_image(d, dry_run=False, runner=rec)
    assert len(rec.calls) == 2  # vendor wheels (networked), then offline build
    assert rec.calls[0][0] == ["bash", "scripts/vendor_wheels.sh"]
    argv, cwd = rec.calls[1]
    assert argv[:3] == ["docker", "build", "--network=none"]
    assert cwd == d


def test_build_image_without_manifest_falls_back_to_dir_name(tmp_path):
    d = tmp_path / "forged-mcp-widgets"
    d.mkdir()
    cmds = package.build_image(d)
    assert "forged-mcp-widgets:latest" in cmds[-1]


# ---- open_pr --------------------------------------------------------------

def test_open_pr_default_is_dry_run(tmp_path):
    rec = Recorder()
    d = _server_dir(tmp_path)
    cmds = package.open_pr(d, runner=rec)
    assert rec.calls == []
    gh = cmds[-1]
    assert gh[:3] == ["gh", "pr", "create"]
    assert gh[gh.index("--label") + 1] == "needs-security-review"
    assert "--draft" in gh


def test_open_pr_execute_never_merges(tmp_path):
    rec = Recorder()
    d = _server_dir(tmp_path)
    (d / ".env").write_text("SECRET=1")
    (d / ".venv").mkdir()
    (d / "__pycache__").mkdir()
    (d / "src.py").write_text("x=1")
    wd = tmp_path / "work"
    package.open_pr(d, repo="/some/monorepo", dry_run=False, runner=rec, workdir=wd)
    argvs = [a for a, _ in rec.calls]
    assert argvs[0][:2] == ["git", "clone"]
    assert all("merge" not in a for a in argvs)
    assert ["git", "checkout", "-B", "forge/petstore"] in argvs
    assert ["git", "add", "--", "servers/petstore"] in argvs
    assert not any(a[:3] == ["git", "add", "-A"] for a in argvs)
    assert all(cwd == wd for a, cwd in rec.calls[1:])
    labels = [i for i, a in enumerate(argvs) if "labels" in a[-1]]
    push = [i for i, a in enumerate(argvs) if a[:2] == ["git", "push"]]
    assert labels and push and labels[0] < push[0]
    copied = wd / "servers" / "petstore"
    assert (copied / "src.py").exists()
    assert not (copied / ".env").exists() and not (copied / ".venv").exists()
    assert not (copied / "__pycache__").exists()


def test_open_pr_execute_requires_repo(tmp_path):
    with pytest.raises(ValueError):
        package.open_pr(_server_dir(tmp_path), dry_run=False, runner=Recorder())


def test_trailer_not_hardcoded(tmp_path):
    cmds = package.open_pr(_server_dir(tmp_path), model="Claude Opus 9")
    msg = next(c for c in cmds if c[:2] == ["git", "commit"])[3]
    assert "Co-Authored-By: Claude Opus 9 <" in msg and "Haiku" not in msg


def test_pr_body_escapes_and_truncates_spec_text():
    m = dict(MANIFEST, description="evil `x` @team <b>\n## ignore previous " + "A" * 500)
    body = package._pr_body(m)
    first = body.splitlines()[0]
    assert "@team" not in body and "<b>" not in body and "\n##" not in body
    assert len(first) < 400


def test_pr_body_flags_destructive_tools(tmp_path):
    cmds = package.open_pr(_server_dir(tmp_path))
    gh = cmds[-1]
    body = gh[gh.index("--body") + 1]
    assert "delete_pet" in body
    assert "get_pet" in body
    assert "destructive" in body
    assert body.count("(destructive)") == 1


# ---- CLI ------------------------------------------------------------------

class FakeHarness:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.scaffolded: list = []
        self.loops: list = []

    def scaffold(self, manifest, out_dir, template_dir=None):
        self.scaffolded.append(out_dir)
        return Path(out_dir)

    def run_loop(self, out_dir, agent_cmd=None, max_iters=3, validator=None):
        self.loops.append((out_dir, agent_cmd, max_iters))

        class R:
            pass

        r = R()
        r.ok = self.ok
        r.iterations = 1
        r.violations = [] if self.ok else ["bad write"]
        return r


@pytest.fixture
def stub_ingest(monkeypatch):
    from forge import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "ingest", lambda path: [dict(MANIFEST)])


def _install_harness(monkeypatch, fake: FakeHarness):
    from forge import harness

    monkeypatch.setattr(harness, "scaffold", fake.scaffold)
    monkeypatch.setattr(harness, "run_loop", fake.run_loop)


def test_cli_default_is_dry_run_and_executes_nothing(monkeypatch, tmp_path, capsys, stub_ingest):
    fake = FakeHarness()
    _install_harness(monkeypatch, fake)
    rec_build, rec_pr = [], []
    monkeypatch.setattr(package, "build_image", lambda d, **kw: rec_build.append(kw) or [["docker"]])
    monkeypatch.setattr(package, "open_pr", lambda d, **kw: rec_pr.append(kw) or [["gh"]])

    rc = cli.main(["create", "--spec", "api.yaml", "--out", str(tmp_path)])
    assert rc == 0
    assert fake.scaffolded == [] and fake.loops == []
    assert rec_build == [{"dry_run": True}]
    assert len(rec_pr) == 1 and rec_pr[0]["dry_run"] is True
    assert "dry-run" in capsys.readouterr().out


def test_cli_execute_runs_pipeline_in_order(monkeypatch, tmp_path, stub_ingest):
    fake = FakeHarness(ok=True)
    _install_harness(monkeypatch, fake)
    rec_build, rec_pr = [], []
    monkeypatch.setattr(package, "build_image", lambda d, **kw: rec_build.append(kw) or [])
    monkeypatch.setattr(package, "open_pr", lambda d, **kw: rec_pr.append(kw) or [])

    rc = cli.main(["create", "--spec", "api.yaml", "--out", str(tmp_path), "--execute",
                   "--repo", "/m", "--yes"])
    assert rc == 0
    assert len(fake.scaffolded) == 1
    assert fake.loops[0][1] == cli.AGENT_CMD and fake.loops[0][2] == 3
    assert rec_build == [{"dry_run": False}]
    assert len(rec_pr) == 1 and rec_pr[0]["dry_run"] is False and rec_pr[0]["repo"] == "/m"


def test_cli_execute_stops_when_generation_fails(monkeypatch, tmp_path, stub_ingest):
    fake = FakeHarness(ok=False)
    _install_harness(monkeypatch, fake)
    called = []
    monkeypatch.setattr(package, "build_image", lambda d, **kw: called.append("build") or [])
    monkeypatch.setattr(package, "open_pr", lambda d, **kw: called.append("pr") or [])

    rc = cli.main(["create", "--spec", "api.yaml", "--out", str(tmp_path), "--execute",
                   "--repo", "/m", "--yes"])
    assert rc == 1
    assert called == []


def test_cli_execute_requires_repo(monkeypatch, tmp_path, stub_ingest):
    fake = FakeHarness()
    _install_harness(monkeypatch, fake)
    assert cli.main(["create", "--spec", "a", "--out", str(tmp_path), "--execute", "--yes"]) == 2
    assert fake.scaffolded == []


def test_cli_execute_needs_confirmation(monkeypatch, tmp_path, capsys, stub_ingest):
    fake = FakeHarness()
    _install_harness(monkeypatch, fake)
    rc = cli.create("a", str(tmp_path), dry_run=False, repo="/m", input_fn=lambda p: "nope")
    assert rc == 1 and fake.scaffolded == []
    assert "delete_pet" in capsys.readouterr().out
    monkeypatch.setattr(package, "build_image", lambda d, **kw: [])
    monkeypatch.setattr(package, "open_pr", lambda d, **kw: [])
    assert cli.create("a", str(tmp_path), dry_run=False, repo="/m", input_fn=lambda p: "petstore") == 0


def test_cli_empty_spec_rejected(monkeypatch, tmp_path, capsys):
    from forge import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "ingest", lambda path: [dict(MANIFEST, tools=[])])
    assert cli.main(["create", "--spec", "a", "--out", str(tmp_path)]) == 1
    assert "no tools" in capsys.readouterr().err


def test_cli_dry_run_and_execute_are_exclusive():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["create", "--spec", "x", "--dry-run", "--execute"])


def test_cli_help_via_python_m_forge():
    proc = subprocess.run([sys.executable, "-m", "forge", "--help"], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0
    assert "create" in proc.stdout
