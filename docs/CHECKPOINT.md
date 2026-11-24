# Checkpoint

## State
- Forge (Python FastMCP generator): public repo bsommers/mcp-forge, `main` pushed (last: cd325f1), 134 tests pass.
- Tracker: public repo bsommers/build-tracker (~/src/tools/build-tracker), pushed (77a442b).
- Tracker dashboard served by `serve.py` on 0.0.0.0:8791 (no auth, LAN only), serving the forge/tracker dir (gitignored in forge).
- Agents: claude, agy verified; cursor adapter unverified (cursor-agent not installed).
- Apache-2.0 LICENSE requested for both repos: NOT yet added/committed/pushed (see Open).

## Decisions (user)
- Python FastMCP stack; build with parallel subagents, cheapest adequate model (sonnet for logic, haiku only mechanical).
- Tracker public, no local paths/secrets; template wheels vendored at image-build time only.
- Dry-run by default; human gate before any PR.
- User asked: "add Apache2 License" (both public repos).

## Dead ends / gotchas
- mcp 2.x removed mcp.server.fastmcp: pin `mcp<2`, run tests with `python -m pytest`.
- agy headless silently auto-denies non-allow-listed shell commands (rc 0); allow-list uses bare command names only.
- Port 8765 was already in use by another process; tracker uses 8791.
- An auto-mode classifier denied one Bash call that combined a license download (curl) with git pushes; do not retry it verbatim.

## Open
- Add LICENSE (Apache-2.0) to forge and build-tracker, commit, push (push needs user go-ahead if blocked).
- agy e2e: lint+handshake pass, generated tests fail after 2 iterations (agent did not update tests/test_example.py).
- Unverified: real `claude -p` e2e, docker sandbox mode, PR flow on a real repo, cursor adapter. semgrep/ruff not installed here.

## Next command
cd /home/bill/src/swe/forge && uv run --with pytest --with pyyaml --with pydantic --with httpx --with 'mcp<2' python -m pytest -q tests

- 2026-11-23: ruff+semgrep installed; template lint bug fixed (unused noqa/import); agy strict e2e (no --allow-skip) converges. Docker daemon hangs on `docker create` (host issue; sandbox untested). claude e2e still needs CLAUDE_CODE_OAUTH_TOKEN/ANTHROPIC_API_KEY.
