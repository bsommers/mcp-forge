# Forge review (spec: spec/buildspec_mcp-forge.md)

Each case below was tried with scripts in throwaway adversarial scripts. Nothing in the forge was edited.

## CRITICAL

1. **Validation passes when the agent produces nothing, crashes, or is broken (false pass).**
   - Template ships `src/tools/example.py` and `tests/test_example.py`, so a bare scaffold is green. Tried: a no-op agent gives `ok=True` on all 4 steps; `run_loop(['false'])` (agent exit 1) gives `ok=True` after 1 iteration.
   - harness.py:190-217: the agent returncode, stdout and stderr are ignored.
   - validate.py:433-436: the handshake calls only `tools[0]` and never checks `result.isError`. A tool that raises still passes; tried `boom()`.
   - Nothing compares `tools/list` to `forge-manifest.json`: names, count, or that the `echo_message` placeholder is gone.
   - Fix:
     - Check `proc.returncode` and raise or record it.
     - After the handshake, require that the tool-name set equals the manifest names.
     - Fail if `echo_message` is present.
     - Call every tool with dummy args and fail on `isError`. A 4xx/5xx from the mock upstream may be acceptable, but an unhandled exception is not.
     - Require `len(tools) <= 15` (spec 15-tool cap; tried: 20 tools registered pass).

2. **Locked-file enforcement can be bypassed and writes outside the sandbox.**
   - harness.py:136-147 `snapshot_locked` skips symlinks, `.git` and `.venv`.
   - harness.py:159-163 `enforce_locks` restores with `write_bytes()` through a symlink. Tried: the agent replaces `src/auth.py` with a symlink to `/x/victim.txt`; the revert overwrote the victim file outside the tree and the symlink stayed in place.
   - An agent-created `.git/hooks/pre-commit` is not flagged (`viol=[]`). The later `git commit` in package.open_pr runs it on the host.
   - `src/tools/__init__.py` is writable (`WRITABLE_DIRS`), but it is the bootstrap that `index.py` imports. The "locked transport" can therefore be altered.
   - Fix:
     - Snapshot with `os.walk(followlinks=False)` and treat any symlink or non-regular file anywhere in the tree as a violation. Remove it with `unlink()`, never write through it.
     - Do not exempt `.git`, `.venv` or `__pycache__` outside `src/tools` and `tests`; delete them before validate and packaging.
     - Move `tools/__init__.py` out of the writable dir, or add it to the lock list explicitly.
     - Re-hash after the whole loop as well, not only per iteration.

3. **No sandbox: agent, pytest and handshake all run on the host (spec section 3).**
   - harness.py:190 runs `claude -p` with `subprocess.run` using the full `os.environ` and no permission flags, so the agent has the user's cwd, HOME, `GH_TOKEN` and so on.
   - validate.py:387, 420 run the agent-written tests and server on the host.
   - There is no docker, no `--network=none`, and no mock-only network.
   - Fix: run agent and validator via `docker run --rm --network=none --read-only -v <dir>:/work --cap-drop=ALL --user nonroot`, with a scrubbed env. Pass `--allowedTools`/`--permission-mode` to claude and limit writes with `--add-dir`.

## HIGH

4. **`docker build --network=none` cannot succeed, and the image would not run.** (package.py:580, template/Dockerfile:4)
   - The Dockerfile runs `pip install` in a RUN step, which needs network.
   - The build stage is python:3.12 and the runtime is distroless `python3-debian12`, which is 3.11. The compiled wheels (pydantic-core cp312) will not import on 3.11.
   - Fix: pre-vendor wheels (`pip download` in a separate networked step, then `pip install --no-index`), or build with network and run with none. Use the same Python minor version in both stages (`python:3.11-slim`, or `gcr.io/distroless/python3` pinned). Docker is installed here; the image build is untested.

5. **`open_pr` stages and pushes the wrong things.** (package.py:606-613)
   - It runs `git add -A`, `git checkout -b` and `git push` in `server_dir` with no `git init`. When `forged/` is inside another repo (tried), `git add -A` staged the unrelated `unrelated.txt` from the repo root. It would also commit `.env`, `.venv` and `__pycache__`; the template has no `.gitignore`.
   - There is no configurable monorepo target; it pushes to whatever `origin` is.
   - Re-runs fail on the existing branch.
   - Fix:
     - Require an explicit `--repo`/`--monorepo` path.
     - Clone it, copy the server into `servers/<name>/`, and `git add -- servers/<name>`.
     - Add a template `.gitignore`.
     - Use `checkout -B`.
     - Verify the `needs-security-review` label exists before the push.

6. **Security linter is trivially bypassed (validate.py:312-362).**
   - All 22 of these were MISSED:
     - `from os import system`; `__import__('os').system`; `shell=1`; `shell=var`
     - `getattr(builtins,'eval')`; `compile`/`pickle`
     - `from sys import stdout; print(file=stdout)`; `p=print`; `os.write(1,..)`; `sys.__stdout__`; `sys.stdout=...`; `logging.basicConfig(stream=sys.stdout)`
     - `c['password']='...'`; `def f(api_key='long...')`; `f(token='long...')`; `API_KEY='abc123'` (under 8 chars)
     - an injection phrase in `Field(description=)` or `@mcp.tool(description=)`; `ignore  previous` (two spaces); `Ignore the previous`
   - Semgrep is silently skipped when it is not installed (validate.py:377), so step 2 passes with only the weak AST check.
   - Fix:
     - Resolve import aliases (track `ImportFrom` and `Import ... as`).
     - Flag any `shell=` that is not literal `False`.
     - Ban `__import__`, `getattr` on builtins, `os.write`, `sys.__stdout__`, `pickle`, `marshal`, `compile`, `ctypes`.
     - Flag any reference to `sys.stdout`/`stdout` other than inside the locked files.
     - Scan every string constant for the injection regex, and tolerate `\s+`, `the` and `above`.
     - Also scan dict-subscript and keyword secrets, and default args.
     - Make a missing semgrep a failure in `--strict` mode and add `--config` rules offline (`--config auto` needs network).
     - Lint `tests/` as well.

7. **Stdout cleanliness is not verified at runtime.** (validate.py:413-439)
   - Tried: a module doing `os.write(1, b"HELLO NOT JSON\n")` at import passes the handshake.
   - Fix: run the server through a wrapper that captures raw stdout bytes (or a pty) and fails if any non-JSON-RPC line appears. Run `index.py` under `subprocess` and parse every line of stdout.

8. **Lint and semgrep are skipped silently, and a skipped step counts as passing.** (validate.py:298, 463-479; harness.py:223)
   - `ruff`/`mypy`/`semgrep` missing gives `skipped=True`, `ok=True`, and `validate.py` exits 0 ("SKIP").
   - Fix: `skipped` means exit non-zero by default. Allow it only with `--allow-skip`. Do the same in `run_loop`.

## MEDIUM

9. **Empty spec is not rejected.** (cli.py:47, ingest.py:58)
   - `_chunk` returns `[[]]`, so `manifests` is always truthy and `forge: no tools found` never fires. Tried: an empty-paths spec returns rc 0 and the plan shows a server with zero tools.
   - Fix: `if not any(m.tools for m in manifests)`.

10. **Ingest crashes with an unhelpful error on valid input.**
    - `operationId: 2fa_enable` gives a pydantic ValidationError (ingest.py:30-39). Sanitize it: strip leading digits, or prefix with the verb.
    - A parameter without `name` raises `KeyError 'name'` (ingest.py:128-133). Skip or report it.
    - `in: header/cookie` parameters are dropped silently (ingest.py:130). Warn.
    - Duplicate operationIds are renamed `_2` without a warning.

11. **Unvalidated spec values flow into the manifest, the prompt, the PR body and the README.**
    - `Auth.header` accepts `"X\nEvil: 1"` (tried; manifest.py:20). Validate it with an RFC 7230 token regex.
    - `Tool.path`, `Parameter.name` and `base_url` are unvalidated. Parameter names such as `class` or `a-b` produce broken or injected identifiers.
    - `_pr_body` interpolates the description raw (package.py:585). Escape it. Treat spec text as untrusted data in the system prompt, delimited.
    - A prompt-injection phrase in the spec description reaches the agent via `json.dumps(manifest)` (prompt.py:41).
    - Fix: restrict parameter names to `^[A-Za-z_][A-Za-z0-9_]*$`, restrict paths to `^/[\w\-./{}]*$`, require `base_url` to be https with no userinfo, and truncate descriptions.

12. **The human gate relies on the draft flag only.** (cli.py:75-80)
    - `--execute` does build, push and `gh pr create --draft` in one go, with no confirmation prompt.
    - A draft PR is not an enforced gate. Merging depends on branch protection or CODEOWNERS in the target repo. Nothing checks for that.
    - Fix: add an interactive `--yes` or "type the server name" confirm before push. Print the destructive-tool list first. Document or require a CODEOWNERS rule.

13. **`scaffold` into an existing dir overwrites.** (harness.py:129)
    - `dirs_exist_ok=True` leaves stale agent files from earlier runs in `src/tools` and `tests`, and clobbers locked files.
    - `--out` pointing inside the template recurses.
    - Fix: refuse a non-empty target, or add `--force`.

14. **Agent timeout and failure paths are unhandled.** (harness.py:190)
    - `subprocess.TimeoutExpired` (1800s) propagates out of `run_loop`. Locked files are not re-enforced and a partial tree is left behind. The CLI prints only the exception text.
    - Fix: catch it, record the failure as a violation, and fail the result.

15. **`.validate-stderr.log` is written into the server dir.** (validate.py:421)
    - It is deleted on normal paths, but it would be committed (via `git add -A`) if the process is killed in between, and it feeds server stderr back to the agent unfiltered. Write it to a `tempfile` outside the tree and redact env values.

## LOW

16. **Template http_client and errors** (template/src/http_client.py:162, errors.py:129)
    - `r.json()` on a non-JSON 200 raises a raw `ValueError`. Wrap it in `UpstreamError`.
    - There is no response-size cap despite the spec's "no multi-megabyte dumps". Stream and cap at about 1 MB.
    - `from_status` echoes 200 chars of the upstream body to the model, which could reflect a token. Redact.
    - `logging_util` redacts only `extra.fields` keys, not `msg` or `exc`.
    - The tools/call result is not checked for secrets. The check is only by convention.
17. **`client_configs` never emits the docker variant** (cli.py:71 passes no `image`), although the image is built. Env vars never appear in the stdio snippet (no `env` block with placeholders). Print both.
18. **`GIT_AUTHOR_TRAILER`** (package.py:503) hardcodes "Claude Haiku 5.5" regardless of the model used.
19. **Handshake** catches `BaseException` (validate.py:449), including `KeyboardInterrupt`. Narrow it to `Exception` and `BaseExceptionGroup`.
20. **Ingest limits.** Large specs are chunked by tag, but SQL tables are chunked per tool, so one table's CRUD can split across servers (ingest.py:245). There is no size or alias limit on YAML (`safe_load` alias expansion).
21. **No unit-test depth check.** The spec asks for happy path plus 4xx/5xx coverage. pytest only requires rc 0, so `assert True` passes. Check at least one test per tool, or parse the test files.

## Compliance matrix

| Spec item | Status |
|---|---|
| Stage 1 ingest, 15 cap, manifest | Ingest and cap OK (40 ops split 15/15/10); empty spec and bad operationId not handled; no cap check on the generated server |
| Stage 2 template | Present; the writable `tools/__init__.py` weakens the lock; no `.gitignore`; Dockerfile broken |
| Stage 3 locked files | Partial, bypassable (finding 2); no container |
| Stage 4 validation | 4 steps exist; several false-pass paths (1, 6, 7, 8); 3 retries OK (`max_iters+1` runs) |
| Stage 5 packaging | Dry-run default OK; image build fails; PR stages the wrong files; no monorepo target |
| Section 3 sandbox | Missing |
| Section 3 credential isolation | Template reads env only; no secrets logged by the forge; host env leaks to the agent |
| Human gate | Draft plus label only; no enforcement or confirm |
| stdout clean | Static check only, weak |
| Dry-run default | OK (cli.py:31); nothing written in dry-run |
