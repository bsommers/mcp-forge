"""Deterministic validation suite for a forged MCP server (python/FastMCP)."""
from __future__ import annotations

import ast
import json
import re
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

HANDSHAKE_TIMEOUT = 30.0
CALL_TIMEOUT = 15.0
MAX_TOOLS = 15
LOCKED_SRC = {"index.py", "auth.py", "errors.py", "http_client.py", "logging_util.py"}


@dataclass
class StepResult:
    step: str
    ok: bool
    stderr: str = ""
    skipped: bool = False


INJECTION_RE = re.compile(
    r"(ignore|disregard|forget)\s+(all\s+)?(the\s+)?(previous|prior|above|earlier|preceding)|"
    r"system\s+prompt|you\s+must\s+now|do\s+not\s+tell\s+the\s+user|"
    r"override\s+(the\s+|all\s+)?(previous\s+)?instructions",
    re.I,
)
SECRET_NAME_RE = re.compile(r"(secret|password|passwd|token|api_?key|apikey|credential|private_key)", re.I)
SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[bp]-[A-Za-z0-9-]{10,}|"
    r"Bearer\s+[A-Za-z0-9._-]{16,})"
)
ENVNAME_RE = re.compile(r"[A-Z][A-Z0-9_]*")
BANNED_CALLS = {"eval", "exec", "compile", "__import__", "os.system", "os.popen", "os.write",
                "os.execv", "os.execve", "os.execl", "os.execlp", "os.execvp", "os.spawnl",
                "os.spawnv", "os.posix_spawn", "os.fdopen", "builtins.eval", "builtins.exec"}
BANNED_MODULES = {"pickle", "marshal", "ctypes", "builtins", "cPickle", "dill", "_pickle"}
BANNED_GETATTR_STR = {"eval", "exec", "compile", "__import__", "system", "popen", "__builtins__"}
SUBPROCESS_FUNCS = {"run", "call", "check_call", "check_output", "Popen", "getoutput", "getstatusoutput"}

SEMGREP_RULES = """rules:
  - id: forge-shell-true
    languages: [python]
    severity: ERROR
    message: shell=True is forbidden
    pattern: subprocess.$F(..., shell=True, ...)
  - id: forge-eval
    languages: [python]
    severity: ERROR
    message: eval/exec forbidden
    pattern-either:
      - pattern: eval(...)
      - pattern: exec(...)
  - id: forge-os-system
    languages: [python]
    severity: ERROR
    message: os.system forbidden
    pattern: os.system(...)
  - id: forge-pickle
    languages: [python]
    severity: ERROR
    message: pickle forbidden
    pattern: pickle.$F(...)
"""


def _run(cmd: list[str], cwd: Path, timeout: int = 300) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, f"timeout running {' '.join(cmd)}"
    return p.returncode, (p.stdout + p.stderr)


# ---- step 1 ---------------------------------------------------------------
def step_lint(server_dir: Path) -> StepResult:
    msgs: list[str] = []
    ok = True
    ran = False
    src = server_dir / "src"
    ruff = shutil.which("ruff")
    if ruff:
        ran = True
        rc, out = _run([ruff, "check", "src", "tests"], server_dir)
        if rc:
            ok = False
            msgs.append("ruff:\n" + out)
    else:
        msgs.append("ruff not installed (skipped)")
    mypy = shutil.which("mypy")
    if mypy:
        ran = True
        rc, out = _run([mypy, "--ignore-missing-imports", "--explicit-package-bases", str(src)], server_dir)
        if rc:
            ok = False
            msgs.append("mypy:\n" + out)
    else:
        msgs.append("mypy not installed (skipped)")
    return StepResult("lint", ok, "\n".join(msgs) if (not ok or not ran) else "", skipped=not ran)


# ---- step 2 ---------------------------------------------------------------
def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _aliases(tree: ast.AST) -> dict[str, str]:
    """Map local names to fully-qualified dotted names (imports and simple `a = b.c` aliases)."""
    al: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                al[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                al[a.asname or a.name] = f"{node.module}.{a.name}"
    for _ in range(3):  # resolve chains like p = print; q = p
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Name, ast.Attribute)):
                d = _resolve(_dotted(node.value), al)
                if d:
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            al[t.id] = d
    return al


def _resolve(name: str, al: dict[str, str]) -> str:
    if not name:
        return ""
    head, _, rest = name.partition(".")
    if head in al:
        return al[head] + ("." + rest if rest else "")
    return name


def _is_literal_false(v: ast.AST) -> bool:
    return isinstance(v, ast.Constant) and v.value is False


def lint_source(code: str, filename: str = "<src>", locked: bool = False,
                secrets: bool = True) -> list[str]:
    """Custom AST security linter. `locked` files (template transport) may reference sys.stdout.
    `secrets=False` skips secret checks (test fixtures use fake credentials)."""
    try:
        tree = ast.parse(code, filename)
    except SyntaxError as e:
        return [f"{filename}:{e.lineno}: syntax error: {e.msg}"]
    issues: list[str] = []
    al = _aliases(tree)

    def add(n: ast.AST, msg: str) -> None:
        issues.append(f"{filename}:{getattr(n, 'lineno', 0)}: {msg}")

    def secret_value(v: ast.AST | None) -> bool:
        return (isinstance(v, ast.Constant) and isinstance(v.value, str) and v.value != ""
                and not ENVNAME_RE.fullmatch(v.value))

    def check_secret(n: ast.AST, name: str, v: ast.AST | None) -> None:
        if secrets and name and SECRET_NAME_RE.search(name) and secret_value(v):
            add(n, f"hardcoded secret assigned to {name}")

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for m in mods:
                if m.split(".")[0] in BANNED_MODULES:
                    add(node, f"import of forbidden module {m}")
            if isinstance(node, ast.ImportFrom) and node.module:
                for a in node.names:
                    if f"{node.module}.{a.name}" in BANNED_CALLS:
                        add(node, f"import of forbidden {node.module}.{a.name}")
        if isinstance(node, ast.Name) and node.id in ("__builtins__",):
            add(node, "__builtins__ access is forbidden")
        if isinstance(node, ast.Call):
            name = _resolve(_dotted(node.func), al)
            if name in BANNED_CALLS or name.split(".")[0] in BANNED_MODULES:
                add(node, f"forbidden call {name}()")
            if name == "getattr" and node.args:
                first = _resolve(_dotted(node.args[0]), al)
                a1 = node.args[1] if len(node.args) > 1 else None
                if first.split(".")[0] in ("builtins", "__builtins__") or not isinstance(a1, ast.Constant) \
                        or (isinstance(a1, ast.Constant) and a1.value in BANNED_GETATTR_STR):
                    if first.split(".")[0] in ("builtins", "__builtins__") or (
                            isinstance(a1, ast.Constant) and a1.value in BANNED_GETATTR_STR):
                        add(node, "getattr on builtins / forbidden attribute is forbidden")
            if name == "print":
                f = [k for k in node.keywords if k.arg == "file"]
                if not f or _resolve(_dotted(f[0].value), al) != "sys.stderr":
                    add(node, "print() to stdout is forbidden (stdout is for MCP frames)")
            for k in node.keywords:
                if k.arg == "shell" and not _is_literal_false(k.value):
                    add(node, "shell= must be literal False")
                if k.arg:
                    check_secret(node, k.arg, k.value)
        if isinstance(node, ast.Attribute) or isinstance(node, ast.Name):
            r = _resolve(_dotted(node), al)
            if r in ("sys.stdout", "sys.__stdout__") and not locked:
                if isinstance(node, ast.Attribute) or isinstance(getattr(node, "ctx", None), ast.Load):
                    add(node, f"reference to {r} is forbidden outside locked files")
            elif r.startswith("sys.__stdout__") and not locked:
                add(node, "sys.__stdout__ is forbidden")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if INJECTION_RE.search(node.value):
                add(node, "prompt-injection phrase in string")
            if secrets and SECRET_VALUE_RE.search(node.value):
                add(node, "hardcoded secret-looking string literal")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = node.args
            pos = a.posonlyargs + a.args
            for arg, d in zip(pos[len(pos) - len(a.defaults):], a.defaults):
                check_secret(node, arg.arg, d)
            for arg, d in zip(a.kwonlyargs, a.kw_defaults):
                check_secret(node, arg.arg, d)
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    check_secret(node, k.value, v)
        targets: list[tuple[ast.AST, ast.AST | None]] = []
        if isinstance(node, ast.Assign):
            targets = [(t, node.value) for t in node.targets]
        elif isinstance(node, ast.AnnAssign):
            targets = [(node.target, node.value)]
        for t, v in targets:
            if isinstance(t, ast.Name):
                nm = t.id
            elif isinstance(t, ast.Attribute):
                nm = t.attr
            elif isinstance(t, ast.Subscript):
                if _resolve(_dotted(t.value), al) == "os.environ":
                    continue
                sl = t.slice
                nm = sl.value if isinstance(sl, ast.Constant) and isinstance(sl.value, str) else ""
            else:
                nm = ""
            check_secret(node, nm, v)
    return issues


def step_security(server_dir: Path, strict: bool = False) -> StepResult:
    issues: list[str] = []
    for sub in ("src", "tests"):
        base = server_dir / sub
        for f in sorted(base.rglob("*.py")) if base.is_dir() else []:
            rel = f.relative_to(server_dir)
            locked = sub == "src" and len(rel.parts) == 2 and rel.name in LOCKED_SRC
            issues += lint_source(f.read_text(), str(rel), locked=locked, secrets=(sub == "src"))
    note = ""
    skipped = False
    sg = shutil.which("semgrep")
    if sg and (server_dir / "src").is_dir():
        with tempfile.TemporaryDirectory(prefix="forge-sg-") as td:
            rules = Path(td) / "rules.yml"
            rules.write_text(SEMGREP_RULES)
            rc, out = _run([sg, "--config", str(rules), "--error", "--quiet", "--metrics", "off",
                            "--disable-version-check", "src", "tests"], server_dir, 300)
        if rc:
            issues.append("semgrep:\n" + out)
    else:
        note = "semgrep not installed (skipped)"
        skipped = True
        if strict:
            issues.append("semgrep not installed and --strict given")
    if issues:
        return StepResult("security", False, "\n".join(issues))
    return StepResult("security", True, note, skipped=skipped)


# ---- step 3 ---------------------------------------------------------------
def _tests_reference_tools(server_dir: Path, tool_names: list[str]) -> str | None:
    """Every tool must be referenced by >=1 test function that makes a real assertion."""
    funcs: list[ast.AST] = []
    for f in sorted((server_dir / "tests").rglob("*.py")):
        try:
            tree = ast.parse(f.read_text())
        except SyntaxError:
            continue
        funcs += [n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")]

    def asserts(fn: ast.AST) -> bool:
        for n in ast.walk(fn):
            if isinstance(n, ast.Assert) and not isinstance(n.test, ast.Constant):
                return True
            if isinstance(n, ast.With) and any("raises" in _dotted(i.context_expr.func)
                                                if isinstance(i.context_expr, ast.Call) else False
                                                for i in n.items):
                return True
        return False

    def mentions(fn: ast.AST, tool: str) -> bool:
        if tool in getattr(fn, "name", ""):
            return True
        return any((isinstance(n, ast.Constant) and isinstance(n.value, str) and tool in n.value)
                   or (isinstance(n, ast.Name) and n.id == tool)
                   or (isinstance(n, ast.Attribute) and n.attr == tool)
                   for n in ast.walk(fn))

    missing = [t for t in tool_names if not any(mentions(fn, t) and asserts(fn) for fn in funcs)]
    if missing:
        return "no test function (with a real assertion) references tool(s): " + ", ".join(missing)
    return None


def step_pytest(server_dir: Path, tool_names: list[str] | None = None) -> StepResult:
    if not (server_dir / "tests").is_dir():
        return StepResult("pytest", False, "no tests/ directory")
    rc, out = _run([sys.executable, "-m", "pytest", "-q", "-x", "tests"], server_dir, 600)
    if rc:
        return StepResult("pytest", False, out)
    if tool_names:
        err = _tests_reference_tools(server_dir, tool_names)
        if err:
            return StepResult("pytest", False, err)
    return StepResult("pytest", True, "")


# ---- step 4 ---------------------------------------------------------------
def _dummy(schema: dict) -> object:
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    for key in ("anyOf", "oneOf"):
        if isinstance(schema.get(key), list) and schema[key]:
            subs = [x for x in schema[key] if isinstance(x, dict) and x.get("type") != "null"]
            if subs:
                return _dummy(subs[0])
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    return {"string": "x", "integer": 1, "number": 1.0, "boolean": True,
            "array": [], "object": {}}.get(t if isinstance(t, str) else "string", "x")


def _check_schema(schema: object) -> str | None:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return "inputSchema must be an object schema"
    try:
        from jsonschema import Draft202012Validator
        Draft202012Validator.check_schema(schema)
    except ImportError:
        json.dumps(schema)
    except Exception as e:  # noqa: BLE001
        return f"invalid JSON Schema: {e}"
    return None


# Errors a tool raised on purpose (typed upstream/auth errors, bad dummy input): not crashes.
HANDLED_ERR_RE = re.compile(
    r"upstream|HTTP\s*[45]\d\d|\b[45]\d\d\b.*(error|not found|limited|forbidden)|resource not found|"
    r"rate limited|authentication failed|missing required environment variable|"
    r"validation error|input should be|unreachable|timed out|connection (error|refused)|"
    r"invalid (argument|input|value)",
    re.I,
)


class _Rpc:
    """Minimal line-delimited JSON-RPC stdio client that records every raw stdout line."""

    def __init__(self, proc: subprocess.Popen):
        self.p = proc
        self.q: queue.Queue = queue.Queue()
        self.raw: list[bytes] = []
        self.n = 0
        self.t = threading.Thread(target=self._pump, daemon=True)
        self.t.start()

    def _pump(self) -> None:
        assert self.p.stdout is not None
        for line in iter(self.p.stdout.readline, b""):
            self.raw.append(line)
            self.q.put(line)
        self.q.put(None)

    def send(self, obj: dict) -> None:
        assert self.p.stdin is not None
        self.p.stdin.write(json.dumps(obj).encode() + b"\n")
        self.p.stdin.flush()

    def request(self, method: str, params: dict, timeout: float) -> dict:
        self.n += 1
        rid = self.n
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        end = time.monotonic() + timeout
        while True:
            try:
                line = self.q.get(timeout=max(0.01, end - time.monotonic()))
            except queue.Empty:
                raise TimeoutError(f"{method} timed out after {timeout}s") from None
            if line is None:
                raise RuntimeError(f"server exited (rc={self.p.poll()}) during {method}")
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # reported by the final stdout audit
            if isinstance(msg, dict) and msg.get("id") == rid and ("result" in msg or "error" in msg):
                return msg
            if end < time.monotonic():
                raise TimeoutError(f"{method} timed out after {timeout}s")


def _audit_stdout(raw: list[bytes]) -> str | None:
    for line in raw:
        s = line.strip()
        if not s:
            continue
        try:
            m = json.loads(s)
        except ValueError:
            return f"non-JSON-RPC line on stdout: {s[:120]!r}"
        if not isinstance(m, dict) or m.get("jsonrpc") != "2.0":
            return f"non-JSON-RPC line on stdout: {s[:120]!r}"
    return None


def _redact(text: str) -> str:
    for v in sorted({v for v in os.environ.values() if len(v) >= 6}, key=len, reverse=True):
        text = text.replace(v, "***")
    return text


def _manifest_names(server_dir: Path) -> list[str] | None:
    mf = server_dir / "forge-manifest.json"
    if not mf.is_file():
        return None
    try:
        return [t["name"] for t in json.loads(mf.read_text()).get("tools", [])]
    except (ValueError, KeyError, TypeError, AttributeError):
        return ["<unreadable forge-manifest.json>"]


def _handshake(server_dir: Path, tools_out: list[str]) -> str | None:
    entry = server_dir / "src" / "index.py"
    if not entry.is_file():
        return "src/index.py missing"
    # stderr goes to a tempfile OUTSIDE the server tree
    errf = tempfile.TemporaryFile()
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen([sys.executable, str(entry)], cwd=server_dir, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=errf, env=env)
    rpc = _Rpc(proc)
    err: str | None = None
    try:
        err = _converse(rpc, server_dir, tools_out)
    except (Exception, BaseExceptionGroup) as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        rpc.t.join(timeout=5)
    if err is None:
        err = _audit_stdout(rpc.raw)
    elif (a := _audit_stdout(rpc.raw)):
        err += f"; {a}"
    if err:
        errf.seek(0)
        err += "\nserver stderr:\n" + _redact(errf.read().decode("utf-8", "replace"))[-2000:]
    errf.close()
    return err


def _converse(rpc: _Rpc, server_dir: Path, tools_out: list[str]) -> str | None:
    r = rpc.request("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                   "clientInfo": {"name": "forge-validate", "version": "1"}}, HANDSHAKE_TIMEOUT)
    if "error" in r:
        return f"initialize failed: {r['error']}"
    rpc.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    r = rpc.request("tools/list", {}, HANDSHAKE_TIMEOUT)
    if "error" in r:
        return f"tools/list failed: {r['error']}"
    tools = r["result"].get("tools", [])
    if not tools:
        return "tools/list returned no tools"
    names = [t.get("name") for t in tools]
    tools_out.extend(names)
    if len(tools) > MAX_TOOLS:
        return f"{len(tools)} tools exceeds the cap of {MAX_TOOLS}"
    if "echo_message" in names and _manifest_names(server_dir) is not None:
        return "placeholder tool echo_message must be removed"
    want = _manifest_names(server_dir)
    if want is not None and set(want) != set(names):
        return (f"tools/list {sorted(names)} != forge-manifest.json {sorted(want)} "
                f"(missing {sorted(set(want) - set(names))}, extra {sorted(set(names) - set(want))})")
    for t in tools:
        err = _check_schema(t.get("inputSchema"))
        if err:
            return f"tool {t.get('name')}: {err}"
    for t in tools:
        props = t["inputSchema"].get("properties", {})
        args = {k: _dummy(props[k]) for k in t["inputSchema"].get("required", []) if k in props}
        c = rpc.request("tools/call", {"name": t["name"], "arguments": args}, CALL_TIMEOUT)
        if "error" in c:
            return f"tool {t['name']}: JSON-RPC error {c['error']}"
        res = c["result"]
        if res.get("isError"):
            text = " ".join(x.get("text", "") for x in res.get("content", []) if isinstance(x, dict))
            if not HANDLED_ERR_RE.search(text):
                return f"tool {t['name']} raised an unhandled error: {text[:300]}"
    return None


def step_handshake(server_dir: Path, tools_out: list[str] | None = None) -> StepResult:
    try:
        err = _handshake(server_dir, tools_out if tools_out is not None else [])
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    return StepResult("handshake", err is None, err or "")


def validate(server_dir, allow_skip: bool = False, strict: bool = False) -> list[StepResult]:
    d = Path(server_dir).resolve()
    names: list[str] = []
    hs = step_handshake(d, names)  # run first: pytest step needs the tool names
    res = [step_lint(d), step_security(d, strict=strict), step_pytest(d, names if hs.ok else None), hs]
    if not allow_skip:
        for r in res:
            if r.skipped and r.ok:
                r.ok = False
                r.stderr = (r.stderr + "\n" if r.stderr else "") + "step skipped; rerun with --allow-skip to permit"
    return res


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    allow_skip = "--allow-skip" in argv
    strict = "--strict" in argv
    pos = [a for a in argv if not a.startswith("--")]
    if len(pos) != 1:
        print("usage: validate.py [--allow-skip] [--strict] <server_dir>", file=sys.stderr)
        return 2
    results = validate(pos[0], allow_skip=allow_skip, strict=strict)
    failed = False
    for r in results:
        status = "FAIL" if not r.ok else "SKIP" if r.skipped else "PASS"
        print(f"[{status}] {r.step}", file=sys.stderr)
        if r.stderr:
            print(r.stderr, file=sys.stderr)
        failed |= not r.ok
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
