# MCP Forge build contract (Python / FastMCP). Shared by all agents. Do not edit.
Python >=3.11, stdlib + `mcp` (FastMCP via `from mcp.server.fastmcp import FastMCP`), `pydantic`, `pyyaml`, `httpx`, `pytest`. Run tests with `cd <repo> && uv run --with pytest --with pyyaml --with pydantic --with httpx --with mcp pytest -q tests/<yourfile>`.
Package `forge/` (importable as `forge`). Each agent owns ONLY its files.

## forge-manifest.json (dict; pydantic model `forge.manifest.Manifest` — owned by A)
{ "name": "kebab-case", "description": str, "source_type": "openapi|graphql|sql|markdown",
  "base_url": str, "auth": {"type":"none|api_key|bearer|oauth2","env_var":"UPPER_SNAKE","header":"Authorization"},
  "env_vars":[str], "tools":[{"name":"verb_noun","description":str,"method":"GET|POST|PUT|PATCH|DELETE","path":"/x/{id}",
     "parameters":[{"name":str,"in":"path|query|body","type":"string|integer|number|boolean|object|array","required":bool,"description":str}],
     "destructive":bool}] }
Max 15 tools per manifest; ingest splits larger specs into multiple manifests (groups) -> list[Manifest]. destructive = method in POST/PUT/PATCH/DELETE.

## Modules / owners
A `forge/manifest.py` + `forge/ingest.py` + `tests/test_ingest.py`: `ingest(path)->list[Manifest]` (OpenAPI 3 json/yaml full; graphql/sql/markdown basic), `write_manifest(m, dir)`. Tool names verb_noun snake_case.
B `template/**` (golden template, a runnable FastMCP server): `template/src/index.py` (stdio + streamable-http via `--transport`), `auth.py` (env-var loader, never logs secrets), `logging_util.py` (JSON logs to stderr ONLY), `errors.py`, `http_client.py` (httpx retry/timeout), `src/tools/__init__.py` with `register(mcp)` that auto-imports sibling modules each exposing `register(mcp)`, `Dockerfile` (distroless nonroot), `pyproject.toml`, one example tool + `tests/test_example.py`.
C `forge/validate.py` + `validate-mcp.sh` + `tests/test_validate.py`: `validate(server_dir)->list[StepResult(step,ok,stderr)]` running 1 ruff+mypy(skip gracefully if missing, mark skipped), 2 custom AST security linter (no hardcoded secrets, no eval/exec/os.system/shell=True, no print() to stdout / writes to sys.stdout in src, no prompt-injection phrases in tool docstrings like "ignore previous"), + semgrep if installed, 3 pytest, 4 protocol handshake (spawn server over stdio, initialize, tools/list valid JSON Schema, one tools/call with timeout, using the mcp python client). Exit non-zero on fail; stderr summary.
D `forge/harness.py` + `forge/prompt.py` + `tests/test_harness.py`: `scaffold(manifest, out_dir)` copies `template/` and writes forge-manifest.json; `run_loop(out_dir, agent_cmd:list[str], max_iters=3, validator=validate)` invokes agent (default `claude -p` with system prompt from prompt.py) with scoped-write rules (writes only src/tools/, tests/, README.md — enforce by checksumming locked files before/after and reverting violations), feeds failing stderr back up to 3 times. Agent runner is injectable for tests (fake agent).
E `forge/package.py` + `forge/cli.py` + `tests/test_package.py`: `client_configs(manifest)->dict` for claude_desktop/cursor/claude_code mcpServers snippets; `build_image(dir)` (docker, `--network=none` build flag where valid; dry-run mode); `open_pr(dir, dry_run=True)` via gh with label needs-security-review, never promotes w/o human gate; CLI `forge create --spec api.yaml [--out DIR] [--dry-run]` wiring ingest->scaffold->run_loop->package using the signatures above. `python -m forge`.
Imports across modules use exactly the names above; if a sibling module isn't present yet, import lazily inside functions.
Report format: status · files · test output last line · blockers (<=8 lines).
