"""Stage 1: ingest API definitions into forge manifests."""
from __future__ import annotations

import json
import re
import warnings
from pathlib import Path

import yaml

from .manifest import _SNAKE, _TOKEN, MAX_TOOLS, Auth, Manifest, Parameter, Tool, check_base_url, write_manifest

__all__ = ["ingest", "write_manifest"]

_VERBS = {"get", "list", "create", "update", "delete", "search", "add", "remove",
          "set", "find", "fetch", "patch", "replace", "run", "send", "upload", "download"}
_METHOD_VERB = {"GET": "get", "POST": "create", "PUT": "update", "PATCH": "update", "DELETE": "delete"}
MAX_SPEC_BYTES = 5 * 1024 * 1024
_HTTP = ("get", "post", "put", "patch", "delete")


def _snake(s: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    return s


def _kebab(s: str) -> str:
    return _snake(s).replace("_", "-") or "api"


def _valid_name(n: str, method: str) -> str:
    """Coerce a snake string into a valid verb_noun tool name ('' if impossible)."""
    n = re.sub(r"_+", "_", n).strip("_")
    if not n:
        return ""
    verb = _METHOD_VERB.get(method, "get")
    if not n[0].isalpha():
        n = f"{verb}_{n}"
    elif "_" not in n:
        n = f"{verb}_{n}"
    return n if _SNAKE.match(n) else ""


def _verb_noun(raw: str, method: str, path: str = "") -> str:
    n = _valid_name(_snake(raw), method) if raw else ""
    if raw and not n:
        warnings.warn(f"unusable operationId {raw!r}; deriving name from path", stacklevel=2)
    if n:
        parts = n.split("_")
        if len(parts) == 1:
            n = f"{_METHOD_VERB.get(method, 'get')}_{n}"
        elif parts[0] not in _VERBS and parts[0] not in _METHOD_VERB.values():
            # e.g. pets_list -> keep as-is if it already has an underscore
            pass
        return n
    return _derive(method, path)


def _derive(method: str, path: str) -> str:
    segs = [_snake(p) for p in path.split("/") if p and not p.startswith("{")]
    noun = "_".join(segs[-2:]) or "resource"
    verb = _METHOD_VERB.get(method, "get")
    if method == "GET" and not path.rstrip("/").endswith("}"):
        verb = "list"
    return _valid_name(f"{verb}_{noun}", method) or f"{verb}_resource"


def _unique(name: str, seen: set[str]) -> str:
    base, i = name, 2
    if name in seen:
        warnings.warn(f"duplicate tool name {name!r}; renaming", stacklevel=2)
    while name in seen:
        name = f"{base}_{i}"
        i += 1
    seen.add(name)
    return name


def _chunk(tools: list[Tool], base_name: str, groups: list[str] | None = None, **kw) -> list[Manifest]:
    """Pack tools into manifests of <= MAX_TOOLS, keeping each group together when it fits."""
    if not tools:
        return []
    groups = groups or [""] * len(tools)
    by: dict[str, list[Tool]] = {}
    for g, t in zip(groups, tools):
        by.setdefault(g, []).append(t)
    units: list[list[Tool]] = []
    for ts in by.values():
        units += [ts[i:i + MAX_TOOLS] for i in range(0, len(ts), MAX_TOOLS)]
    packed: list[list[Tool]] = []
    for u in units:
        if packed and len(packed[-1]) + len(u) <= MAX_TOOLS:
            packed[-1] += u
        else:
            packed.append(list(u))
    out = []
    for i, g in enumerate(packed, 1):
        name = base_name if len(packed) == 1 else f"{base_name}-{i}"
        out.append(Manifest(name=name, tools=g, **kw))
    return out


def _env_name(base: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", base.upper()).strip("_") + "_API_KEY"


# ---------------- OpenAPI ----------------
def _resolve(doc: dict, node, depth=0):
    if isinstance(node, dict) and "$ref" in node and depth < 10:
        ref = node["$ref"]
        if isinstance(ref, str) and ref.startswith("#/"):
            cur = doc
            for part in ref[2:].split("/"):
                cur = cur.get(part.replace("~1", "/").replace("~0", "~"), {}) if isinstance(cur, dict) else {}
            return _resolve(doc, cur, depth + 1)
    return node


def _ptype(schema: dict) -> str:
    t = schema.get("type") if isinstance(schema, dict) else None
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    return t if t in ("string", "integer", "number", "boolean", "object", "array") else (
        "object" if isinstance(schema, dict) and "properties" in schema else "string")


def _auth(doc: dict, base: str) -> Auth:
    schemes = (doc.get("components") or {}).get("securitySchemes") or doc.get("securityDefinitions") or {}
    for s in schemes.values():
        s = _resolve(doc, s)
        t = s.get("type")
        if t == "http" and str(s.get("scheme", "")).lower() == "bearer":
            return Auth(type="bearer", env_var=_env_name(base).replace("_API_KEY", "_TOKEN"), header="Authorization")
        if t == "apiKey":
            hdr = s.get("name", "X-API-Key")
            if not isinstance(hdr, str) or not _TOKEN.fullmatch(hdr):
                warnings.warn(f"invalid apiKey header name {hdr!r}; using X-API-Key", stacklevel=2)
                hdr = "X-API-Key"
            return Auth(type="api_key", env_var=_env_name(base), header=hdr)
        if t in ("oauth2", "openIdConnect"):
            return Auth(type="oauth2", env_var=_env_name(base).replace("_API_KEY", "_TOKEN"))
        if t == "http":
            return Auth(type="bearer", env_var=_env_name(base).replace("_API_KEY", "_TOKEN"))
    return Auth(type="none")


def _from_openapi(doc: dict, fallback: str) -> list[Manifest]:
    info = doc.get("info") or {}
    title = info.get("title") or fallback
    base = _kebab(title)
    servers = doc.get("servers") or []
    base_url = servers[0].get("url", "") if servers and isinstance(servers[0], dict) else ""
    if not base_url and doc.get("host"):
        base_url = f"https://{doc['host']}{doc.get('basePath', '')}"
    if base_url:
        try:
            check_base_url(base_url)
        except ValueError:
            warnings.warn(f"ignoring unsafe/non-https server url {base_url!r}", stacklevel=2)
            base_url = ""
    auth = _auth(doc, base)
    seen: set[str] = set()
    entries: list[tuple[str, Tool]] = []
    for path, item in (doc.get("paths") or {}).items():
        item = _resolve(doc, item)
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters") or []
        for method in _HTTP:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            m = method.upper()
            params: dict[tuple[str, str], Parameter] = {}
            for p in list(shared) + list(op.get("parameters") or []):
                p = _resolve(doc, p)
                if not isinstance(p, dict):
                    continue
                if p.get("in") in ("header", "cookie"):
                    warnings.warn(f"{m} {path}: {p.get('in')} parameter {p.get('name')!r} ignored", stacklevel=2)
                    continue
                if p.get("in") not in ("path", "query"):
                    continue
                if not isinstance(p.get("name"), str) or not p["name"]:
                    warnings.warn(f"{m} {path}: parameter without a name skipped", stacklevel=2)
                    continue
                sch = _resolve(doc, p.get("schema") or {})
                params[(p["name"], p["in"])] = Parameter(
                    name=p["name"], **{"in": p["in"]}, type=_ptype(sch),
                    required=bool(p.get("required") or p["in"] == "path"),
                    description=str(p.get("description") or ""))
            rb = _resolve(doc, op.get("requestBody") or {})
            if rb:
                content = rb.get("content") or {}
                media = content.get("application/json") or next(iter(content.values()), {})
                sch = _resolve(doc, (media or {}).get("schema") or {})
                props = sch.get("properties") if isinstance(sch, dict) else None
                if props:
                    req = set(sch.get("required") or [])
                    for pn, ps in props.items():
                        ps = _resolve(doc, ps)
                        params[(pn, "body")] = Parameter(
                            name=pn, **{"in": "body"}, type=_ptype(ps), required=pn in req,
                            description=str(ps.get("description") or ""))
                else:
                    params[("body", "body")] = Parameter(
                        name="body", **{"in": "body"}, type=_ptype(sch) if sch else "object",
                        required=bool(rb.get("required")), description="Request body")
            name = _unique(_verb_noun(op.get("operationId", ""), m, path), seen)
            desc = str(op.get("summary") or op.get("description") or f"{m} {path}").strip()
            tags = op.get("tags") or []
            group = tags[0] if tags else ""
            try:
                tool = Tool(name=name, description=desc, method=m, path=path,
                            parameters=list(params.values()))
            except ValueError as e:
                warnings.warn(f"{m} {path}: skipped ({str(e).splitlines()[0]})", stacklevel=2)
                continue
            entries.append((str(group), tool))
    # keep tag groups together when splitting
    entries.sort(key=lambda e: e[0])
    tools = [t for _, t in entries]
    groups = [g for g, _ in entries]
    desc = str(info.get("description") or title).strip().split("\n")[0]
    return _chunk(tools, base, groups, description=desc, source_type="openapi", base_url=base_url,
                  auth=auth, env_vars=[auth.env_var] if auth.env_var else [])


# ---------------- GraphQL ----------------
def _gql_type(t: str) -> str:
    t = t.replace("!", "").strip()
    if t.startswith("["):
        return "array"
    return {"Int": "integer", "Float": "number", "Boolean": "boolean", "ID": "string",
            "String": "string"}.get(t, "object" if t[:1].isupper() and t not in ("ID",) else "string")


def _from_graphql(text: str, fallback: str) -> list[Manifest]:
    text = re.sub(r"#.*", "", text)
    tools: list[Tool] = []
    seen: set[str] = set()
    for kind, method in (("Query", "GET"), ("Mutation", "POST")):
        for blk in re.finditer(r"(?:extend\s+)?type\s+%s\s*\{([^}]*)\}" % kind, text):
            body = blk.group(1)
            for f in re.finditer(r"(\w+)\s*(?:\(([^)]*)\))?\s*:\s*([\[\]\w!]+)", body):
                fname, args, _ret = f.groups()
                params = []
                for a in re.finditer(r"(\w+)\s*:\s*([\[\]\w!]+)", args or ""):
                    params.append(Parameter(name=a.group(1), **{"in": "body"}, type=_gql_type(a.group(2)),
                                            required=a.group(2).endswith("!"), description=""))
                raw = _snake(fname)
                if "_" not in raw:
                    raw = f"{'get' if method == 'GET' else 'run'}_{raw}"
                raw = _valid_name(raw, method) or _derive(method, f"/{fname}")
                name = _unique(raw, seen)
                tools.append(Tool(name=name, description=f"GraphQL {kind.lower()} {fname}",
                                  method=method, path="/graphql", parameters=params))
    return _chunk(tools, _kebab(fallback), description=f"GraphQL API {fallback}",
                  source_type="graphql", auth=Auth(type="bearer", env_var=_env_name(fallback).replace("_API_KEY", "_TOKEN")),
                  env_vars=[_env_name(fallback).replace("_API_KEY", "_TOKEN")])


# ---------------- SQL ----------------
_SQL_TYPES = [("int", "integer"), ("serial", "integer"), ("bool", "boolean"), ("float", "number"),
              ("double", "number"), ("real", "number"), ("numeric", "number"), ("decimal", "number")]


def _sql_type(t: str) -> str:
    t = t.lower()
    return next((v for k, v in _SQL_TYPES if k in t), "string")


def _from_sql(text: str, fallback: str) -> list[Manifest]:
    text = re.sub(r"--[^\n]*", "", text)
    tools: list[Tool] = []
    groups: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?(\w+)[`\"\]]?\s*\((.*?)\)\s*;",
                         text, re.I | re.S):
        table, body = m.groups()
        cols = []
        pk = "id"
        for line in re.split(r",\s*(?![^()]*\))", body):
            line = line.strip()
            cm = re.match(r"[`\"\[]?(\w+)[`\"\]]?\s+(\w+)(.*)", line, re.S)
            if not cm or cm.group(1).upper() in ("PRIMARY", "FOREIGN", "UNIQUE", "CONSTRAINT", "KEY", "CHECK", "INDEX"):
                continue
            c, t, rest = cm.groups()
            if re.search(r"primary\s+key", rest, re.I):
                pk = c
            cols.append((c, _sql_type(t), bool(re.search(r"not\s+null", rest, re.I)) and "default" not in rest.lower()))
        noun = _snake(table)
        sing = noun[:-1] if noun.endswith("s") and len(noun) > 1 else noun
        path = f"/{noun}"
        pkt = next((t for c, t, _ in cols if c == pk), "string")
        idp = Parameter(name=pk, **{"in": "path"}, type=pkt, required=True, description=f"{sing} {pk}")
        bodyp = lambda req: [Parameter(name=c, **{"in": "body"}, type=t, required=req and r, description="")
                             for c, t, r in cols if c != pk]
        for verb, meth, p, params, d in [
            ("list", "GET", path, [Parameter(name="limit", **{"in": "query"}, type="integer", description="Max rows")], f"List {noun}"),
            ("get", "GET", f"{path}/{{{pk}}}", [idp], f"Get one {sing}"),
            ("create", "POST", path, bodyp(True), f"Create {sing}"),
            ("update", "PUT", f"{path}/{{{pk}}}", [idp] + bodyp(False), f"Update {sing}"),
            ("delete", "DELETE", f"{path}/{{{pk}}}", [idp], f"Delete {sing}"),
        ]:
            tools.append(Tool(name=_unique(_valid_name(f"{verb}_{sing if verb != 'list' else noun}", meth)
                                           or f"{verb}_table", seen),
                              description=d, method=meth, path=p, parameters=params))
            groups.append(noun)
    return _chunk(tools, _kebab(fallback), groups, description=f"SQL schema {fallback}", source_type="sql",
                  auth=Auth(type="api_key", env_var=_env_name(fallback)), env_vars=[_env_name(fallback)])


# ---------------- Markdown ----------------
_MD_OP = re.compile(r"(GET|POST|PUT|PATCH|DELETE)\s+(/\S*?)[`\s:]*(?:[-:—]\s*(.*))?$")


def _from_markdown(text: str, fallback: str) -> list[Manifest]:
    title = fallback
    tools: list[Tool] = []
    seen: set[str] = set()
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# ") and title == fallback:
            title = s[2:].strip() or fallback
        s2 = re.sub(r"^[#>*\-\s]+", "", s).replace("`", "").replace("**", "")
        m = _MD_OP.match(s2)
        if not m:
            continue
        meth, path, desc = m.group(1), m.group(2).rstrip(":"), (m.group(3) or "").strip()
        params = [Parameter(name=p, **{"in": "path"}, type="string", required=True, description="")
                  for p in re.findall(r"\{(\w+)\}", path)]
        try:
            tools.append(Tool(name=_unique(_verb_noun("", meth, path), seen),
                              description=desc or f"{meth} {path}", method=meth, path=path, parameters=params))
        except ValueError as e:
            warnings.warn(f"{meth} {path}: skipped ({str(e).splitlines()[0]})", stacklevel=2)
    return _chunk(tools, _kebab(title), description=f"{title} (from markdown PRD)", source_type="markdown",
                  auth=Auth(type="none"))


# ---------------- entry ----------------
def ingest(path: str | Path) -> list[Manifest]:
    p = Path(path)
    if p.stat().st_size > MAX_SPEC_BYTES:
        raise ValueError(f"{p} exceeds {MAX_SPEC_BYTES} bytes")
    text = p.read_text(encoding="utf-8")
    ext = p.suffix.lower()
    stem = p.stem
    if ext in (".json", ".yaml", ".yml"):
        try:
            if ext != ".json":
                for ev in yaml.parse(text, Loader=yaml.SafeLoader):
                    if isinstance(ev, yaml.AliasEvent):
                        raise ValueError("YAML aliases are not allowed")
            doc = json.loads(text) if ext == ".json" else yaml.safe_load(text)
        except (json.JSONDecodeError, yaml.YAMLError) as e:
            raise ValueError(f"invalid spec syntax in {p}: {e}") from e
        if not isinstance(doc, dict) or not ("openapi" in doc or "swagger" in doc or "paths" in doc):
            raise ValueError(f"{p} is not an OpenAPI document")
        return _from_openapi(doc, stem)
    if ext in (".graphql", ".gql", ".graphqls"):
        return _from_graphql(text, stem)
    if ext == ".sql":
        return _from_sql(text, stem)
    if ext in (".md", ".markdown"):
        return _from_markdown(text, stem)
    raise ValueError(f"unsupported input type: {ext}")
