"""Packaging stage: client config snippets, container build, and PR opening.

Side-effecting functions default to dry_run=True and return the planned argv
lists without executing anything. Execution goes through an injectable runner
(argv, cwd) so tests never touch docker or gh.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

Runner = Callable[[list[str], Path], Any]

PR_LABEL = "needs-security-review"
DESTRUCTIVE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
DEFAULT_AGENT_MODEL = "Claude"
_PR_DESC_MAX = 200
_COPY_IGNORE = shutil.ignore_patterns(
    ".env", ".env.*", ".venv", "venv", "__pycache__", "*.pyc", ".git", ".validate-stderr.log",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules",
)


def author_trailer(model: str | None = None) -> str:
    """Commit trailer naming the model that actually produced the code (not hardcoded)."""
    name = re.sub(r"[^\w .\-]", "", model or "").strip() or DEFAULT_AGENT_MODEL
    return f"Co-Authored-By: {name} <noreply@anthropic.com>"
_PLACEHOLDER_DIR = "<path-to-forged-server>"


def manifest_dict(manifest: Any) -> dict[str, Any]:
    """Accept a Manifest (pydantic) or a plain dict and return a dict."""
    if isinstance(manifest, dict):
        return manifest
    dump = getattr(manifest, "model_dump", None)
    if callable(dump):
        return dump()
    raise TypeError(f"expected Manifest or dict, got {type(manifest).__name__}")


def _read_manifest(server_dir: Path) -> dict[str, Any]:
    path = server_dir / "forge-manifest.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    # Fallback for dry-runs before scaffolding: forged-mcp-<name> -> <name>.
    return {"name": server_dir.name.removeprefix("forged-mcp-"), "tools": []}


def _env_names(m: dict[str, Any]) -> list[str]:
    names = list(m.get("env_vars") or [])
    env_var = (m.get("auth") or {}).get("env_var")
    if env_var and env_var not in names:
        names.append(env_var)
    return names


def _python_entry(server_dir: str, env_names: list[str]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "command": "uv",
        "args": ["run", "--directory", server_dir, "python", "src/index.py", "--transport", "stdio"],
    }
    if env_names:
        entry["env"] = {v: f"<{v}>" for v in env_names}
    return entry


def _docker_entry(image: str, env_names: list[str]) -> dict[str, Any]:
    args = ["run", "--rm", "-i"]
    for var in env_names:
        # Passing by name only: the env block supplies a placeholder, never a real secret.
        args += ["-e", var]
    args.append(image)
    entry: dict[str, Any] = {"command": "docker", "args": args}
    if env_names:
        entry["env"] = {v: f"<{v}>" for v in env_names}
    return entry


def client_configs(manifest: Any, *, server_dir: str | None = None, image: str | None = None) -> dict[str, Any]:
    """Return ready-to-paste mcpServers blocks (stdio and docker) for Claude Desktop, Cursor and Claude Code."""
    m = manifest_dict(manifest)
    names = _env_names(m)
    image = image or f"forged-mcp-{m['name']}:latest"
    snippet = {"mcpServers": {
        m["name"]: _python_entry(server_dir or _PLACEHOLDER_DIR, names),
        f"{m['name']}-docker": _docker_entry(image, names),
    }}
    return {
        "claude_desktop": copy.deepcopy(snippet),
        "cursor": copy.deepcopy(snippet),
        "claude_code": copy.deepcopy(snippet),
    }


def _default_runner(argv: list[str], cwd: Path) -> Any:
    return subprocess.run(argv, cwd=cwd, check=True)


def _execute(commands: list[list[str]], cwd: Path, dry_run: bool, runner: Runner | None) -> list[list[str]]:
    if not dry_run:
        run = runner or _default_runner
        for argv in commands:
            run(argv, cwd)
    return commands


_VENDOR_DIRS = ("wheels", "vendor", "vendored")


def build_image(server_dir: str | Path, *, tag: str | None = None, dry_run: bool = True,
                runner: Runner | None = None, network: str | None = None) -> list[list[str]]:
    """Plan (dry-run) or run the image build.

    network: docker build --network value. Default: 'none' only when a vendored
    wheels dir exists in the server dir (so no pip fetch is needed), else the
    docker default (network allowed for the pip install step).
    """
    d = Path(server_dir)
    name = _read_manifest(d)["name"]
    tag = tag or f"forged-mcp-{name}:latest"
    cmds: list[list[str]] = []
    if network is None:
        # Wheels are vendored at build time only (never copied by scaffold): fetch them
        # with network access first, then build with no network.
        if not any((d / v).is_dir() for v in _VENDOR_DIRS):
            cmds.append(["bash", "scripts/vendor_wheels.sh"])
        network = "none"
    argv = ["docker", "build"]
    if network:
        argv.append(f"--network={network}")
    argv += ["-t", tag, "."]
    cmds.append(argv)
    return _execute(cmds, d, dry_run, runner)


def _clean(text: Any, limit: int = _PR_DESC_MAX) -> str:
    """Untrusted spec text -> single-line, markdown/mention-inert, truncated."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    s = re.sub(r"[`<>@\[\]|#~\\]", "", s)
    return s[:limit] + ("..." if len(s) > limit else "")


def _pr_body(m: dict[str, Any]) -> str:
    lines = [f"Forged MCP server `{_clean(m['name'], 64)}`: {_clean(m.get('description', ''))}", "", "Tools:"]
    for t in m.get("tools") or []:
        method = _clean(str(t.get("method", "")).upper(), 10)
        flag = " **(destructive)**" if t.get("destructive") or str(t.get("method", "")).upper() in DESTRUCTIVE_METHODS else ""
        lines.append(f"- `{_clean(t.get('name', ''), 64)}` {method} `{_clean(t.get('path', ''), 120)}`{flag}")
    lines += [
        "",
        f"Opened as a draft with label `{PR_LABEL}`. A human reviewer must verify tool "
        "permissions, especially destructive tools, before this is marked ready or merged.",
    ]
    return "\n".join(lines)


def destructive_tools(manifest: Any) -> list[str]:
    m = manifest_dict(manifest)
    return [f"{t.get('name', '')} ({str(t.get('method', '')).upper()} {t.get('path', '')})"
            for t in m.get("tools") or []
            if t.get("destructive") or str(t.get("method", "")).upper() in DESTRUCTIVE_METHODS]


def open_pr(server_dir: str | Path, *, repo: str | Path | None = None, dry_run: bool = True,
            runner: Runner | None = None, base: str = "main", model: str | None = None,
            workdir: str | Path | None = None) -> list[list[str]]:
    """Plan (dry-run) or open a draft PR against an explicit monorepo. Never merges.

    The monorepo (repo: path or URL) is cloned into a scratch workdir, the server is
    copied (minus .env/.venv/__pycache__ etc.) into servers/<name>/, and only that
    path is staged. The review label must exist before anything is pushed.
    """
    d = Path(server_dir)
    if repo is None:
        if not dry_run:
            raise ValueError("open_pr requires an explicit repo (monorepo path or URL)")
        repo_arg = "<monorepo>"
    else:
        repo_arg = str(repo)
    m = _read_manifest(d)
    name = m["name"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", str(name)):
        raise ValueError(f"unsafe server name: {name!r}")
    branch = f"forge/{name}"
    title = f"Add forged MCP server: {name}"
    wd = Path(workdir) if workdir else Path(tempfile.gettempdir()) / "forge-pr" / name
    dest = f"servers/{name}"
    commands = [
        ["git", "clone", "--", repo_arg, str(wd)],
        ["git", "checkout", "-B", branch],
        ["git", "add", "--", dest],
        ["git", "commit", "-m", f"{title}\n\n{author_trailer(model)}"],
        ["gh", "api", f"repos/:owner/:repo/labels/{PR_LABEL}"],  # fails (404) if label is missing
        ["git", "push", "-u", "origin", branch],
        ["gh", "pr", "create", "--draft", "--base", base, "--head", branch,
         "--title", title, "--body", _pr_body(m), "--label", PR_LABEL],
    ]
    if not dry_run:
        run = runner or _default_runner
        wd.parent.mkdir(parents=True, exist_ok=True)
        run(commands[0], d)
        target = wd / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(d, target, ignore=_COPY_IGNORE, dirs_exist_ok=True)
        for argv in commands[1:]:
            run(argv, wd)
    return commands
