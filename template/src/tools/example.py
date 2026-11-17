"""Example tool. Agents add sibling modules in this directory."""
from typing import Any

from pydantic import Field


def register(mcp: Any) -> None:
    @mcp.tool()
    def echo_message(
        message: str = Field(description="Text to echo back", min_length=1, max_length=1000),
    ) -> str:
        """Echo a message back to the caller."""
        return f"echo: {message}"
