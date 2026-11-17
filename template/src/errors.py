"""Typed error wrappers with LLM-friendly messages."""
import re

_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"),
    re.compile(r"(?i)\b(token|api[_-]?key|apikey|secret|password|authorization)\b(\s*[\"']?\s*[:=]\s*)[\"']?[^\s\"',;&]+"),
    re.compile(r"\b(?:sk|pk|ghp|gho|ghs|xox[abp])[-_][A-Za-z0-9_-]{8,}"),
    re.compile(r"\b[A-Za-z0-9_-]{32,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"),
]


def redact(text: str) -> str:
    """Mask bearer tokens, key=value secrets and long opaque strings."""
    for pat in _SECRET_PATTERNS:
        if pat.groups >= 2:
            text = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)
        elif pat.groups == 1:
            text = pat.sub(lambda m: f"{m.group(1)} [REDACTED]", text)
        else:
            text = pat.sub("[REDACTED]", text)
    return text


class ToolError(Exception):
    """Base error; message is safe to show to the model."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class UpstreamError(ToolError):
    """Upstream API returned an error or was unreachable."""


class AuthError(ToolError):
    """Missing or invalid credentials."""


def from_status(status: int, body: str = "") -> ToolError:
    snippet = redact(body[:200].replace("\n", " "))
    if status in (401, 403):
        return AuthError(f"Authentication failed (HTTP {status}).", status)
    if status == 404:
        return UpstreamError("Resource not found (HTTP 404).", status)
    if status == 429:
        return UpstreamError("Rate limited by upstream (HTTP 429).", status)
    return UpstreamError(f"Upstream error HTTP {status}: {snippet}", status)
