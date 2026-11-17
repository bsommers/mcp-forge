import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json

import pytest
import yaml

from forge.ingest import ingest, write_manifest
from forge.manifest import Manifest


def _spec(n=3):
    paths = {}
    for i in range(n):
        paths[f"/things{i}"] = {
            "get": {"operationId": f"listThings{i}", "summary": "List", "parameters": [
                {"name": "limit", "in": "query", "schema": {"type": "integer"}}]},
            "post": {"operationId": f"createThing{i}", "requestBody": {"content": {"application/json": {
                "schema": {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}}}}},
        }
    paths["/things0/{id}"] = {"parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                              "delete": {"operationId": "deleteThing"}}
    return {"openapi": "3.0.0", "info": {"title": "My API", "description": "d"},
            "servers": [{"url": "https://x.test"}],
            "components": {"securitySchemes": {"b": {"type": "http", "scheme": "bearer"}}},
            "paths": paths}


def test_openapi_json_and_yaml(tmp_path):
    s = _spec()
    (tmp_path / "a.json").write_text(json.dumps(s))
    (tmp_path / "a.yaml").write_text(yaml.safe_dump(s))
    for f in ("a.json", "a.yaml"):
        ms = ingest(tmp_path / f)
        assert len(ms) == 1
        m = ms[0]
        assert m.name == "my-api" and m.base_url == "https://x.test"
        assert m.auth.type == "bearer" and m.auth.env_var in m.env_vars
        names = {t.name: t for t in m.tools}
        assert names["delete_thing"].destructive and not names["list_things0"].destructive
        assert names["delete_thing"].parameters[0].in_ == "path"
        assert any(p.name == "a" and p.required for p in names["create_thing0"].parameters)


def test_split_over_15(tmp_path):
    (tmp_path / "big.json").write_text(json.dumps(_spec(10)))  # 21 tools
    ms = ingest(tmp_path / "big.json")
    assert len(ms) == 2 and all(len(m.tools) <= 15 for m in ms)
    assert sum(len(m.tools) for m in ms) == 21
    assert ms[0].name == "my-api-1"


def test_missing_operation_id_and_refs(tmp_path):
    s = {"openapi": "3.0.0", "info": {"title": "T"}, "paths": {"/pets/{id}": {"get": {
        "parameters": [{"$ref": "#/components/parameters/Id"}]}}},
        "components": {"parameters": {"Id": {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}}}}
    (tmp_path / "t.json").write_text(json.dumps(s))
    t = ingest(tmp_path / "t.json")[0].tools[0]
    assert t.name == "get_pets" and t.parameters[0].type == "integer"


def test_graphql(tmp_path):
    (tmp_path / "s.graphql").write_text(
        "type Query { user(id: ID!): User\n listUsers: [User] }\ntype Mutation { createUser(name: String!): User }")
    m = ingest(tmp_path / "s.graphql")[0]
    d = {t.name: t for t in m.tools}
    assert d["list_users"].method == "GET"
    assert d["create_user"].destructive


def test_sql(tmp_path):
    (tmp_path / "db.sql").write_text("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, age INT);")
    m = ingest(tmp_path / "db.sql")[0]
    assert {t.name for t in m.tools} == {"list_users", "get_user", "create_user", "update_user", "delete_user"}


def test_markdown(tmp_path):
    (tmp_path / "p.md").write_text("# Pet Store\n- `GET /pets` - list pets\n- POST /pets: add\n- DELETE /pets/{id}\n")
    m = ingest(tmp_path / "p.md")[0]
    assert m.name == "pet-store" and len(m.tools) == 3
    assert any(t.method == "DELETE" and t.destructive for t in m.tools)


def test_errors(tmp_path):
    (tmp_path / "bad.json").write_text("{nope")
    with pytest.raises(ValueError):
        ingest(tmp_path / "bad.json")
    (tmp_path / "x.txt").write_text("x")
    with pytest.raises(ValueError):
        ingest(tmp_path / "x.txt")


def test_manifest_validation_and_write(tmp_path):
    with pytest.raises(Exception):
        Manifest(name="Bad Name", source_type="sql")
    with pytest.raises(Exception):
        Manifest(name="ok", source_type="sql", tools=[{"name": "bad", "method": "GET", "path": "/"}])
    m = Manifest(name="ok", source_type="sql", tools=[{"name": "get_x", "method": "GET", "path": "/"}])
    p = write_manifest(m, tmp_path / "out")
    data = json.loads(p.read_text())
    assert data["tools"][0]["destructive"] is False and p.name == "forge-manifest.json"


def _w(tmp_path, spec, name="s.json"):
    (tmp_path / name).write_text(json.dumps(spec))
    return tmp_path / name


def _api(paths, **extra):
    return {"openapi": "3.0.0", "info": {"title": "T"}, "paths": paths, **extra}


def test_empty_returns_no_manifests(tmp_path):
    assert ingest(_w(tmp_path, _api({}))) == []
    (tmp_path / "e.sql").write_text("-- nothing")
    assert ingest(tmp_path / "e.sql") == []


def test_operation_id_sanitized(tmp_path):
    ms = ingest(_w(tmp_path, _api({"/a": {"post": {"operationId": "2fa_enable"}}, "/b": {"get": {"operationId": "___"}}})))
    names = {t.name for t in ms[0].tools}
    assert "create_2fa_enable" in names and len(names) == 2


def test_param_without_name_header_and_duplicates_warn(tmp_path):
    spec = _api({"/a": {"get": {"operationId": "get_a", "parameters": [
        {"in": "query"}, {"name": "X-H", "in": "header"}, {"name": "c", "in": "cookie"}]},
        "post": {"operationId": "get_a"}}})
    with pytest.warns(UserWarning) as w:
        ms = ingest(_w(tmp_path, spec))
    msgs = " ".join(str(x.message) for x in w)
    assert "without a name" in msgs and "header parameter" in msgs and "cookie parameter" in msgs and "duplicate" in msgs
    assert ms[0].tools[0].parameters == []


def test_parameter_name_sanitized_keeps_wire_name():
    from forge.manifest import Parameter
    p = Parameter(name="class")
    assert p.name == "class_" and p.wire_name == "class"
    p = Parameter(name="a-b")
    assert p.name == "a_b" and p.wire_name == "a-b"
    assert Parameter(name="ok").wire_name == ""


def test_validators():
    from forge.manifest import Auth, Tool
    with pytest.raises(Exception):
        Auth(type="api_key", header="X\nEvil: 1")
    Auth(type="api_key", header="X-API-Key")
    for bad in ("/a b", "no-slash", "/a?x=1", "/a\n"):
        with pytest.raises(Exception):
            Tool(name="get_x", path=bad)
    for bad in ("http://x.test", "https://u:p@x.test", "ftp://x"):
        with pytest.raises(Exception):
            Manifest(name="ok", source_type="sql", base_url=bad)
    long = Tool(name="get_x", description="d" * 5000)
    assert len(long.description) <= 500


def test_unsafe_server_and_path_skipped(tmp_path):
    spec = _api({"/ok": {"get": {"operationId": "get_ok"}}, "/bad path": {"get": {"operationId": "get_bad"}}},
                servers=[{"url": "http://insecure.test"}])
    with pytest.warns(UserWarning):
        ms = ingest(_w(tmp_path, spec))
    assert ms[0].base_url == "" and [t.name for t in ms[0].tools] == ["get_ok"]


def test_sql_table_crud_stays_together(tmp_path):
    sql = "".join(f"CREATE TABLE t{i} (id INTEGER PRIMARY KEY, n TEXT);\n" for i in range(4))  # 20 tools
    (tmp_path / "db.sql").write_text(sql)
    ms = ingest(tmp_path / "db.sql")
    assert len(ms) == 2
    for m in ms:
        tabs = {}
        for t in m.tools:
            tabs.setdefault(t.path.split("/")[1], []).append(t)
        assert all(len(v) == 5 for v in tabs.values())


def test_yaml_limits(tmp_path):
    (tmp_path / "a.yaml").write_text("openapi: 3.0.0\npaths: {}\nx: &a [1]\ny: *a\n")
    with pytest.raises(ValueError, match="alias"):
        ingest(tmp_path / "a.yaml")
    (tmp_path / "big.yaml").write_text("openapi: 3.0.0\n#" + "x" * (5 * 1024 * 1024))
    with pytest.raises(ValueError, match="exceeds"):
        ingest(tmp_path / "big.yaml")
