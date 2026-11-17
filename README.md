# MCP Server Forge

Turns an API definition (OpenAPI 3, GraphQL, SQL DDL or a Markdown PRD) into a tested, secure
Python FastMCP server: ingest -> golden template -> coding-agent loop -> static/security audit ->
protocol handshake -> packaging (Docker image, client config snippets, draft PR).

    uv run --with pytest --with pyyaml --with pydantic --with httpx --with 'mcp<2' python -m pytest -q tests
    python -m forge create --spec api.yaml --agent claude   # dry-run by default; --execute to act

- Agents: `claude`, `agy` (verified); `cursor` (unverified, flags from docs).
- `mcp` is pinned `<2`: 2.x removed `mcp.server.fastmcp`.
- Safe by default: dry-run, locked transport files, a human gate before any PR; see `CONTRACT.md` and `REVIEW.md`.
- Docker wheels are vendored at image-build time (`template/scripts/vendor_wheels.sh`).
