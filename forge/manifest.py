"""Pydantic models for forge-manifest.json."""
from __future__ import annotations

import json
import keyword
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_TOOLS = 15
DESTRUCTIVE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_SNAKE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)+$")
_ENV = re.compile(r"^[A-Z][A-Z0-9_]*$")
_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")  # RFC 7230 token
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PATH = re.compile(r"^/[\w\-./{}]*$")
MAX_DESC = 500


def _trunc(v: str) -> str:
    v = v if isinstance(v, str) else str(v)
    return v if len(v) <= MAX_DESC else v[:MAX_DESC - 3] + "..."


def safe_identifier(name: str) -> str:
    """Turn an arbitrary wire name into a safe Python-style identifier."""
    s = re.sub(r"[^A-Za-z0-9_]", "_", str(name)).strip() or "param"
    if s[0].isdigit():
        s = "p_" + s
    if keyword.iskeyword(s) or s in ("None", "True", "False"):
        s += "_"
    return s


def check_base_url(v: str) -> str:
    if not v:
        return v
    u = urlparse(v)
    if u.scheme != "https" or not u.hostname or u.username or u.password or re.search(r"[\s\x00-\x1f]", v):
        raise ValueError("base_url must be https with a host and no userinfo")
    return v


class Auth(BaseModel):
    type: Literal["none", "api_key", "bearer", "oauth2"] = "none"
    env_var: str = ""
    header: str = "Authorization"

    @field_validator("header")
    @classmethod
    def _header(cls, v: str) -> str:
        if not _TOKEN.fullmatch(v):
            raise ValueError("header must be an RFC 7230 token")
        return v

    @field_validator("env_var")
    @classmethod
    def _env(cls, v: str) -> str:
        if v and not _ENV.fullmatch(v):
            raise ValueError("env_var must be UPPER_SNAKE")
        return v


class Parameter(BaseModel):
    name: str
    in_: Literal["path", "query", "body"] = Field(default="query", alias="in")
    type: Literal["string", "integer", "number", "boolean", "object", "array"] = "string"
    required: bool = False
    description: str = ""
    wire_name: str = ""  # original name when `name` had to be sanitized

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _safe_name(self) -> "Parameter":
        if not _IDENT.match(self.name) or keyword.iskeyword(self.name):
            if not self.wire_name:
                self.wire_name = self.name
            self.name = safe_identifier(self.name)
        return self

    @field_validator("description")
    @classmethod
    def _desc(cls, v: str) -> str:
        return _trunc(v)


class Tool(BaseModel):
    name: str
    description: str = ""
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    path: str = "/"
    parameters: list[Parameter] = Field(default_factory=list)
    destructive: bool = False

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _SNAKE.match(v):
            raise ValueError(f"tool name must be verb_noun snake_case: {v!r}")
        return v

    @field_validator("path")
    @classmethod
    def _path(cls, v: str) -> str:
        if not _PATH.fullmatch(v):
            raise ValueError(f"invalid tool path: {v!r}")
        return v

    @field_validator("description")
    @classmethod
    def _desc(cls, v: str) -> str:
        return _trunc(v)

    @model_validator(mode="after")
    def _destructive(self) -> "Tool":
        self.destructive = self.method in DESTRUCTIVE_METHODS
        return self


class Manifest(BaseModel):
    name: str
    description: str = ""
    source_type: Literal["openapi", "graphql", "sql", "markdown"]
    base_url: str = ""
    auth: Auth = Field(default_factory=Auth)
    env_vars: list[str] = Field(default_factory=list)
    tools: list[Tool] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _KEBAB.match(v):
            raise ValueError("name must be kebab-case")
        return v

    @field_validator("base_url")
    @classmethod
    def _base_url(cls, v: str) -> str:
        return check_base_url(v)

    @field_validator("description")
    @classmethod
    def _desc(cls, v: str) -> str:
        return _trunc(v)

    @field_validator("tools")
    @classmethod
    def _max(cls, v: list[Tool]) -> list[Tool]:
        if len(v) > MAX_TOOLS:
            raise ValueError(f"max {MAX_TOOLS} tools per manifest")
        return v

    def to_dict(self) -> dict:
        return self.model_dump(by_alias=True)


def write_manifest(m: Manifest, dir: str | Path) -> Path:
    d = Path(dir)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "forge-manifest.json"
    p.write_text(json.dumps(m.to_dict(), indent=2) + "\n")
    return p
