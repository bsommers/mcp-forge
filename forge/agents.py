"""Agent CLI adapters: argv/env construction for headless one-shot runs (never executes anything)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from . import prompt as _prompt

ALLOWED_TOOLS = "Read,Write,Edit,Glob,Grep"


@dataclass(frozen=True)
class Agent:
    name: str
    binary: str
    auth_env: tuple[str, ...]
    verified: bool  # True only if the flags below were confirmed via local `--help`

    def base_cmd(self) -> list[str]:
        raise NotImplementedError

    def build_argv(self, task: str, cwd, base: Sequence[str] | None = None,
                   system_prompt: str | None = None) -> list[str]:
        """Full argv for a headless run. `base` overrides the leading command (e.g. user-supplied list)."""
        raise NotImplementedError

    def env(self, home, environ=None) -> dict[str, str]:
        """Scrubbed env: PATH, scratch HOME, LANG and only this agent's auth vars."""
        src = os.environ if environ is None else environ
        env = {"PATH": src.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(home),
               "LANG": src.get("LANG", "C.UTF-8")}
        for k in self.auth_env:
            if k in src:
                env[k] = src[k]
        return env

    def docker_argv(self, argv: Sequence[str], workdir, image: str, environ=None) -> list[str]:
        from .harness import build_docker_cmd
        return build_docker_cmd(argv, workdir, image, environ=environ, auth_vars=self.auth_env)


@dataclass(frozen=True)
class ClaudeAgent(Agent):
    name: str = "claude"
    binary: str = "claude"
    auth_env: tuple[str, ...] = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                                 "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL")
    verified: bool = True

    def base_cmd(self):
        return ["claude", "-p"]

    def build_argv(self, task, cwd, base=None, system_prompt=None):
        cmd = list(base) if base else self.base_cmd()
        if "--append-system-prompt" not in cmd:
            cmd += ["--append-system-prompt", system_prompt or _prompt.SYSTEM_PROMPT]
        if "--allowedTools" not in cmd:
            cmd += ["--allowedTools", ALLOWED_TOOLS]  # scoped tools: no Bash
        if "--permission-mode" not in cmd:
            cmd += ["--permission-mode", "acceptEdits"]
        if "--add-dir" not in cmd:
            cmd += ["--add-dir", str(cwd)]
        return cmd + ["--", task]  # "--": variadic flags must not eat the task


AGY_ALLOWED_COMMANDS = ("ls", "cat", "grep", "find", "head", "rm", "python", "python3")


@dataclass(frozen=True)
class AgyAgent(Agent):
    """Antigravity. Flags verified against `agy --help` (v1.5.9). agy has no tool allowlist and no
    system-prompt flag: writes are scoped by --mode accept-edits + --add-dir + --sandbox, and the
    harness lock enforcement; the system prompt is prepended to the task. Auth is the local login
    (no env var documented in --help), so none is passed through."""
    name: str = "agy"
    binary: str = "agy"
    auth_env: tuple[str, ...] = ()
    verified: bool = True

    def base_cmd(self):
        return ["agy"]

    def build_argv(self, task, cwd, base=None, system_prompt=None):
        cmd = list(base) if base else self.base_cmd()
        if "--mode" not in cmd:
            cmd += ["--mode", "accept-edits"]
        if "--add-dir" not in cmd:
            cmd += ["--add-dir", str(cwd)]
        if "--sandbox" not in cmd:
            cmd += ["--sandbox"]
        if "--settings" not in cmd:
            # Headless agy auto-denies (silently, rc 0) any shell command not allow-listed.
            # Bare command names only: argument patterns such as command(rm src/*) do not match.
            cmd += ["--settings", json.dumps({"permissions": {"allow": [f"command({c})" for c in AGY_ALLOWED_COMMANDS]}})]
        if not any(a in cmd for a in ("-p", "--print", "--prompt")):
            cmd += ["--print"]
        return cmd + [f"{system_prompt or _prompt.SYSTEM_PROMPT}\n\n{task}"]


@dataclass(frozen=True)
class CursorAgent(Agent):
    """cursor-agent. NOT verified locally (binary not installed); flags taken from
    https://cursor.com/docs/cli/reference/parameters: -p/--print, --force, --trust, --workspace,
    --output-format, --sandbox <enabled|disabled>; auth via CURSOR_API_KEY. No scoped-write flag
    exists, so scoping relies on --sandbox enabled + harness lock enforcement."""
    name: str = "cursor"
    binary: str = "cursor-agent"
    auth_env: tuple[str, ...] = ("CURSOR_API_KEY",)
    verified: bool = False

    def base_cmd(self):
        return ["cursor-agent", "-p"]

    def build_argv(self, task, cwd, base=None, system_prompt=None):
        cmd = list(base) if base else self.base_cmd()
        if "--force" not in cmd:
            cmd += ["--force"]
        if "--trust" not in cmd:
            cmd += ["--trust"]
        if "--sandbox" not in cmd:
            cmd += ["--sandbox", "enabled"]
        if "--workspace" not in cmd:
            cmd += ["--workspace", str(cwd)]
        return cmd + [f"{system_prompt or _prompt.SYSTEM_PROMPT}\n\n{task}"]


_REGISTRY = {a.name: a for a in (ClaudeAgent(), AgyAgent(), CursorAgent())}
_ALIASES = {"cursor-agent": "cursor", "antigravity": "agy"}


def get_agent(name: str) -> Agent:
    key = _ALIASES.get(name, name)
    if key not in _REGISTRY:
        raise ValueError(f"unknown agent {name!r}; choose from {sorted(_REGISTRY)}")
    return _REGISTRY[key]


def agent_for_cmd(cmd: Sequence[str]) -> Agent | None:
    """Adapter matching a command list's binary (basename), or None for unknown commands."""
    if not cmd:
        return None
    b = Path(cmd[0]).name
    for a in _REGISTRY.values():
        if a.binary == b:
            return a
    return None


def agent_names() -> list[str]:
    return sorted(_REGISTRY)
