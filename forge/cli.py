"""forge CLI: `forge create --spec api.yaml [--out DIR] [--execute]`.

Dry-run is the default: nothing is scaffolded, no agent runs, and docker/gh
appear only as planned commands. --execute runs the full pipeline.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import TextIO

AGENT_CMD = ["claude", "-p"]
MAX_ITERS = 3


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="forge", description="MCP Server Forge")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("create", help="generate, validate and package an MCP server from a spec")
    c.add_argument("--spec", required=True, help="OpenAPI/GraphQL/SQL/Markdown input file")
    c.add_argument("--out", default=None, help="output root (default: ./forged)")
    mode = c.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true",
                      help="plan only; nothing is written or executed (default)")
    mode.add_argument("--execute", dest="dry_run", action="store_false",
                      help="run agent generation, docker build and PR opening")
    c.add_argument("--repo", default=None,
                   help="monorepo path/URL the PR targets (required with --execute)")
    c.add_argument("--yes", action="store_true",
                   help="skip the type-the-server-name confirmation for --execute")
    c.add_argument("--model", default=None, help="model name for the commit trailer")
    c.add_argument("--agent", choices=["claude", "agy", "cursor"], default="claude",
                   help="agent CLI for generation (default: claude)")
    c.set_defaults(dry_run=True)
    return p


def _show(argv: list[str], out: TextIO) -> None:
    print("  $ " + " ".join(shlex.quote(a) for a in argv), file=out)


def _confirm(name: str, destructive: list[str], out: TextIO, yes: bool, input_fn) -> bool:
    print(f"  destructive tools in {name}:" if destructive else f"  no destructive tools in {name}", file=out)
    for t in destructive:
        print(f"    - {t}", file=out)
    if yes:
        return True
    try:
        answer = input_fn(f"  type the server name ({name}) to build, push and open the PR: ")
    except EOFError:
        return False
    return answer.strip() == name


def create(spec: str, out: str | None = None, *, dry_run: bool = True, stdout: TextIO | None = None,
           repo: str | None = None, yes: bool = False, model: str | None = None,
           agent: str = "claude", input_fn=input) -> int:
    # Lazy imports: keep `forge --help` cheap and let sibling modules load late.
    from forge import package
    from forge.ingest import ingest

    stdout = stdout or sys.stdout
    out_root = Path(out) if out else Path("forged")
    manifests = ingest(spec)
    if not any(package.manifest_dict(m).get("tools") for m in manifests):
        print("forge: no tools found in spec", file=sys.stderr)
        return 1
    if not dry_run and not repo:
        print("forge: --execute requires --repo <monorepo path or URL>", file=sys.stderr)
        return 2

    for raw in manifests:
        m = package.manifest_dict(raw)
        server_dir = out_root / f"forged-mcp-{m['name']}"
        print(f"[{m['name']}] {server_dir}  ({'dry-run' if dry_run else 'execute'})", file=stdout)

        if dry_run:
            print("  plan: scaffold -> run_loop(max_iters=3) -> validate -> build_image -> open_pr", file=stdout)
        else:
            if not _confirm(m["name"], package.destructive_tools(m), stdout, yes, input_fn):
                print("  aborted: confirmation not given", file=sys.stderr)
                return 1
            from forge.harness import run_loop, scaffold
            from forge.validate import validate

            scaffold(raw, server_dir)
            if agent == "claude":
                result = run_loop(server_dir, AGENT_CMD, max_iters=MAX_ITERS, validator=validate)
            else:
                result = run_loop(server_dir, None, max_iters=MAX_ITERS, validator=validate, agent=agent)
            if not result.ok:
                print(f"  generation failed after {result.iterations} iteration(s)", file=sys.stderr)
                for v in result.violations:
                    print(f"  violation: {v}", file=sys.stderr)
                return 1

        print("  client configs (stdio + docker; fill in <PLACEHOLDER> env values):", file=stdout)
        snippet = package.client_configs(raw, server_dir=str(server_dir.resolve()))
        print(json.dumps(snippet, indent=2), file=stdout)

        print("  image build:", file=stdout)
        for argv in package.build_image(server_dir, dry_run=dry_run):
            _show(argv, stdout)

        print("  pull request:", file=stdout)
        for argv in package.open_pr(server_dir, repo=repo, dry_run=dry_run, model=model,
                                   workdir=out_root / ".pr-work" / m["name"]):
            _show(argv, stdout)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "create":
        try:
            return create(args.spec, args.out, dry_run=args.dry_run,
                          repo=args.repo, yes=args.yes, model=args.model, agent=args.agent)
        except Exception as exc:  # top-level CLI boundary: report, don't traceback
            print(f"forge: error: {exc}", file=sys.stderr)
            return 1
    return 2
