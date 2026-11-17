"""Scaffold a server from the golden template and run the agent/validate self-healing loop."""
from __future__ import annotations

import hashlib
import inspect
import os
import shutil
import tempfile
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from . import prompt as _prompt

DEFAULT_TEMPLATE = Path(__file__).resolve().parent.parent / "template"
WRITABLE_DIRS = ("src/tools/", "tests/")
WRITABLE_FILES = ("README.md",)
LOCKED_IN_WRITABLE = ("src/tools/__init__.py",)
_JUNK_DIRS = (".git", "__pycache__", ".venv", ".pytest_cache", ".mypy_cache", ".ruff_cache")
_LOUD_JUNK = (".git", ".venv")
AUTH_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                 "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL")
ALLOWED_TOOLS = "Read,Write,Edit,Glob,Grep"
DOCKER_IMAGE = "forge-agent:latest"
_IGNORE = ("__pycache__", "*.pyc", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", ".git", "wheels")


def _is_writable(rel: str) -> bool:
    return rel in WRITABLE_FILES or any(rel.startswith(d) for d in WRITABLE_DIRS)


def _manifest_dict(manifest) -> dict:
    if hasattr(manifest, "model_dump"):
        return manifest.model_dump(mode="json")
    return dict(manifest)


def scaffold(manifest, out_dir, template_dir=None, force: bool = False) -> Path:
    """Copy the template into out_dir and write forge-manifest.json.

    Refuses a non-empty target unless force=True, and refuses an out dir inside the template.
    """
    import json

    out = Path(out_dir)
    tpl = Path(template_dir) if template_dir else DEFAULT_TEMPLATE
    if not tpl.is_dir():
        raise FileNotFoundError(f"template dir not found: {tpl}")
    out_r, tpl_r = out.resolve(), tpl.resolve()
    if out_r == tpl_r or tpl_r in out_r.parents:
        raise ValueError(f"output dir {out} is inside the template dir {tpl}")
    if out.is_symlink() or (out.exists() and not out.is_dir()):
        raise FileExistsError(f"output path {out} exists and is not a directory")
    if out.is_dir() and any(out.iterdir()) and not force:
        raise FileExistsError(f"output dir {out} is not empty (use force=True to overwrite)")
    shutil.copytree(tpl, out, dirs_exist_ok=True, symlinks=True, ignore=shutil.ignore_patterns(*_IGNORE))
    (out / "src" / "tools").mkdir(parents=True, exist_ok=True)
    (out / "tests").mkdir(parents=True, exist_ok=True)
    (out / "forge-manifest.json").write_text(json.dumps(_manifest_dict(manifest), indent=2) + "\n")
    return out


def _in_writable_area(rel: str) -> bool:
    return any((rel + "/").startswith(d) or rel.startswith(d) for d in WRITABLE_DIRS)


def _is_locked(rel: str) -> bool:
    return rel in LOCKED_IN_WRITABLE or not _is_writable(rel)


def _sweep(root: Path, fix: bool) -> tuple[dict[str, bytes], list[str]]:
    """Walk root without following symlinks.

    Returns (locked regular files -> bytes, violations). Symlinks and non-regular files anywhere,
    and junk dirs (.git/.venv/caches) outside src/tools and tests, are violations; when fix is
    True they are removed (unlink/rmtree, never written through).
    """
    files: dict[str, bytes] = {}
    viol: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        for name in list(dirnames):
            path = base / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                dirnames.remove(name)
                viol.append(f"symlink {rel} (removed)")
                if fix:
                    path.unlink()
            elif name in _JUNK_DIRS and not _in_writable_area(rel):
                dirnames.remove(name)
                if name in _LOUD_JUNK:
                    viol.append(f"forbidden dir {rel} (removed)")
                if fix:
                    shutil.rmtree(path, ignore_errors=True)
        for name in filenames:
            path = base / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                viol.append(f"symlink {rel} (removed)")
                if fix:
                    path.unlink()
            elif not path.is_file():
                viol.append(f"non-regular file {rel} (removed)")
                if fix:
                    path.unlink()
            elif _is_locked(rel):
                files[rel] = path.read_bytes()
    return files, viol


def snapshot_locked(out_dir) -> dict[str, bytes]:
    """Map relpath -> content for every locked regular file (kept in memory for revert)."""
    return _sweep(Path(out_dir), fix=False)[0]


def checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _clear_path_obstructions(root: Path, rel: str) -> None:
    """Make sure root/rel can be written: parents are real dirs, target is not a link/dir."""
    cur = root
    parts = Path(rel).parts
    for part in parts[:-1]:
        cur = cur / part
        if cur.is_symlink() or (cur.exists() and not cur.is_dir()):
            cur.unlink()
        cur.mkdir(exist_ok=True)
    tgt = root / rel
    if tgt.is_symlink() or tgt.is_file():
        tgt.unlink()
    elif tgt.is_dir():
        shutil.rmtree(tgt)


def enforce_locks(out_dir, snap: dict[str, bytes]) -> list[str]:
    """Revert modified/deleted/added locked files; remove symlinks and non-regular files.

    Returns list of violation descriptions.
    """
    root = Path(out_dir)
    current, violations = _sweep(root, fix=True)
    for rel, data in snap.items():
        if rel not in current:
            _clear_path_obstructions(root, rel)
            (root / rel).write_bytes(data)
            violations.append(f"deleted {rel} (restored)")
        elif checksum(current[rel]) != checksum(data):
            (root / rel).write_bytes(data)
            violations.append(f"modified {rel} (reverted)")
    for rel in current:
        if rel not in snap:
            (root / rel).unlink()
            violations.append(f"added {rel} (removed)")
    return violations


@dataclass
class LoopResult:
    ok: bool
    iterations: int
    results: list = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    agent_errors: list[str] = field(default_factory=list)


AgentRunner = Callable[[str, Path], object]


def scrubbed_env(home: str | Path, environ=None, auth_vars=None) -> dict[str, str]:
    """Minimal env for the agent: PATH, scratch HOME, LANG and the agent's auth vars only (default claude)."""
    src = os.environ if environ is None else environ
    env = {"PATH": src.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(home),
           "LANG": src.get("LANG", "C.UTF-8")}
    for k in (AUTH_ENV_VARS if auth_vars is None else auth_vars):
        if k in src:
            env[k] = src[k]
    return env


def build_docker_cmd(cmd: Sequence[str], workdir, image: str = DOCKER_IMAGE, environ=None,
                     auth_vars=None) -> list[str]:
    """Build the `docker run` command line for sandboxed agent execution (not executed here)."""
    src = os.environ if environ is None else environ
    out = ["docker", "run", "--rm", "--network=none", "--read-only", "--cap-drop=ALL",
           "--security-opt=no-new-privileges", "--user", "65534:65534",
           "--tmpfs", "/tmp", "--tmpfs", "/home/agent",
           "-v", f"{Path(workdir).resolve()}:/work", "-w", "/work", "-e", "HOME=/home/agent"]
    for k in (AUTH_ENV_VARS if auth_vars is None else auth_vars):
        if k in src:
            out += ["-e", k]  # value inherited from the docker client env, never on the cmdline
    return out + [image] + list(cmd)


def _default_runner(agent_cmd: Sequence[str], sandbox: str | None = None,
                    image: str = DOCKER_IMAGE, run=subprocess.run, timeout: int = 1800,
                    agent=None) -> AgentRunner:
    from .agents import agent_for_cmd, get_agent

    adapter = get_agent(agent) if isinstance(agent, str) else (agent or agent_for_cmd(agent_cmd))

    def runner(task: str, cwd: Path):
        if adapter is not None:
            cmd = adapter.build_argv(task, cwd, base=list(agent_cmd))
        else:
            cmd = list(agent_cmd) + [task]
        home = tempfile.mkdtemp(prefix="forge-home-")
        try:
            if sandbox == "docker":
                full = build_docker_cmd(cmd, cwd, image, auth_vars=adapter.auth_env if adapter else None)
            elif sandbox is None:
                full = cmd
            else:
                raise ValueError(f"unknown sandbox: {sandbox!r}")
            return run(full, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                       env=scrubbed_env(home, auth_vars=adapter.auth_env if adapter else None))
        finally:
            shutil.rmtree(home, ignore_errors=True)
    return runner


DEFAULT_AGENT_CMD = ["claude", "-p"]


def _call_validator(validator, root, allow_skip):
    try:
        params = inspect.signature(validator).parameters
        ok = "allow_skip" in params or any(p.kind == p.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        ok = False
    return validator(root, allow_skip=allow_skip) if ok else validator(root)


def run_loop(out_dir, agent_cmd=None, max_iters: int = 3, validator=None,
             allow_skip: bool = False, sandbox: str | None = None, runner_exec=subprocess.run,
             agent: str | None = None) -> LoopResult:
    """Run agent then validator; feed failing stderr back up to max_iters fix rounds.

    agent_cmd: list[str] (subprocess, task appended as last arg) or callable(prompt, cwd).
    validator: callable(server_dir[, allow_skip])->list of objects with .step/.ok/.stderr
        (default forge.validate.validate). Skipped steps count as failures unless allow_skip.
    agent: 'claude'|'agy'|'cursor' adapter name; when agent_cmd is None its default command is used.
    sandbox: None or 'docker' (agent runs in a network-less read-only container).
    """
    import json

    root = Path(out_dir)
    if validator is None:
        from .validate import validate as validator  # lazy
    if agent_cmd is None:
        if agent:
            from .agents import get_agent
            agent_cmd = get_agent(agent).base_cmd()
        else:
            agent_cmd = DEFAULT_AGENT_CMD
    runner = agent_cmd if callable(agent_cmd) else _default_runner(agent_cmd, sandbox, run=runner_exec,
                                                                   agent=agent)

    manifest = json.loads((root / "forge-manifest.json").read_text())
    snap = snapshot_locked(root)
    task = _prompt.build_task_prompt(manifest)
    out = LoopResult(ok=False, iterations=0)
    for _ in range(max_iters + 1):  # initial generation + max_iters fix rounds
        agent_failure = None
        try:
            proc = runner(task, root)
        except subprocess.TimeoutExpired as e:
            out.iterations += 1
            msg = f"agent timed out after {e.timeout}s"
            out.violations.append(msg)
            out.agent_errors.append(msg)
            out.violations += enforce_locks(root, snap)
            return out
        out.iterations += 1
        rc = getattr(proc, "returncode", 0)
        err = (getattr(proc, "stderr", "") or "") if isinstance(getattr(proc, "stderr", ""), str) else ""
        if isinstance(rc, int) and rc != 0:
            agent_failure = f"agent exited with code {rc}: {err[-2000:]}"
            out.agent_errors.append(agent_failure)
        elif "no output produced" in err and "permission" in err:
            # agy exits 0 when headless mode auto-denies a permission; surface it.
            agent_failure = f"agent blocked by a denied permission: {err[-2000:]}"
            out.agent_errors.append(agent_failure)
        viol = enforce_locks(root, snap)
        out.violations += viol
        if agent_failure:
            failures = [("agent", agent_failure)]
        else:
            results = _call_validator(validator, root, allow_skip)
            out.results = results
            failures = []
            for r in results:
                skipped = getattr(r, "skipped", False)
                if skipped and not allow_skip:
                    failures.append((r.step, f"step skipped (tool unavailable): {r.stderr}"))
                elif not skipped and not r.ok:
                    failures.append((r.step, r.stderr))
        if not failures:
            out.ok = True
            break
        task = _prompt.build_feedback_prompt(failures, viol)
    final = enforce_locks(root, snap)  # re-hash after the whole loop
    if final:
        out.violations += final
        out.ok = False
    return out
