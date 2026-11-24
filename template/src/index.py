"""Locked server bootstrap: stdio (default) or streamable-http."""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from logging_util import get_logger
from tools import register

log = get_logger()


def build_server(name: str = "forged-mcp-server") -> FastMCP:
    mcp = FastMCP(name)
    register(mcp)
    return mcp


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    a = p.parse_args(argv)
    mcp = build_server()
    log.info("starting", extra={"fields": {"transport": a.transport}})
    if a.transport == "streamable-http":
        mcp.settings.host = a.host
        mcp.settings.port = a.port
    mcp.run(transport=a.transport)


if __name__ == "__main__":
    main()
