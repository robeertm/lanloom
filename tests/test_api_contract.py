"""Contract of the public API (lanloom/api_v1.py): the description says exactly what the code does.

* The routes of the app under /api/v1, the rights table (lanloom/access.py) and the OpenAPI description name the same
  routes; each operation's scope is the table's role.
* The documented parameters are the parameters of the route (path and query), nothing more, nothing less.
* Real answers of the sample network match the documented schema - objects of the API do not carry undocumented fields
  (``additionalProperties: false``), so a field added in the code without a description turns this test red.
* docs/API.md lists every route and every webhook event.
"""
import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from lanloom import access, api_v1, auth, webhooks
from lanloom import config as config_mod
from lanloom.main import create_app

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("LANLOOM_DATA", str(tmp_path_factory.mktemp("contract")))
    mp.setenv("LANLOOM_DEMO", "1")
    mp.setenv("LANLOOM_DEMO_SEED", "1")
    mp.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        st = app.state.ll
        uid = st.accounts.create("contract", "a-long-test-password", role="admin")
        c.cookies.set(auth.SESSION_COOKIE, st.sessions.create(uid, ip="127.0.0.1", agent="test"))
        c.app_ = app
        c.spec = c.get("/api/v1/openapi.json").json()
        yield c
    mp.undo()


def v1_routes(app):
    for r in app.routes:
        if isinstance(r, APIRoute) and (r.path == api_v1.BASE or r.path.startswith(api_v1.BASE + "/")):
            for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
                yield m, r.path, r


def spec_ops(spec):
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            yield method.upper(), path, op


def norm(path):
    return re.sub(r"\{([^}:]+)(:[^}]+)?\}", r"{\1}", path)


def test_routes_table_and_description_name_the_same_routes(env):
    routes = {(m, norm(p)) for m, p, _ in v1_routes(env.app_)}
    table = {(m, norm(p)) for (m, p) in access.RULES if p == api_v1.BASE or p.startswith(api_v1.BASE + "/")}
    described = {(m, p) for m, p, _ in spec_ops(env.spec)}
    assert routes and routes == table == described, (routes ^ described, routes ^ table)


def test_scope_of_each_operation_is_the_rights_table(env):
    for m, p, op in spec_ops(env.spec):
        tmpl = next(rp for (rm, rp) in access.RULES if rm == m and norm(rp) == p)
        rule = access.RULES[(m, tmpl)]
        want = "ingest" if (m, tmpl) == api_v1.INGEST_ROUTE else api_v1.SCOPE_OF_ROLE[rule.role]
        assert op["x-lanloom-scope"] == want, (m, p)
        assert not rule.stepup, "a route of the public API cannot need a step-up (tokens have none): %s %s" % (m, p)
        assert op["summary"] and op["operationId"] and op["tags"]
        assert set(op["responses"]) >= {"200", "401", "403", "429"}


def test_documented_parameters_are_the_route_parameters(env):
    ops = {(m, p): op for m, p, op in spec_ops(env.spec)}
    for m, p, route in v1_routes(env.app_):
        op = ops[(m, norm(p))]
        doc = {(x["name"], x["in"]) for x in op.get("parameters", [])}
        code = {(q.name, "query") for q in route.dependant.query_params} | {(q.name, "path") for q in route.dependant.path_params}
        assert doc == code, (m, p, doc ^ code)
        for x in op.get("parameters", []):
            if x["in"] == "path":
                assert x["required"] is True


# ---------------------------------------------------------------- a small JSON Schema check (the subset the API uses)
def check(value, schema, where="$"):
    types = schema.get("type")
    if types is not None:
        types = types if isinstance(types, list) else [types]
        ok = any(_is(value, t) for t in types)
        assert ok, "%s: %r is not %s" % (where, value, types)
    if "enum" in schema:
        assert value in schema["enum"], "%s: %r not in %s" % (where, value, schema["enum"])
    if isinstance(value, dict) and "properties" in schema or (isinstance(value, dict) and "additionalProperties" in schema):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            assert req in value, "%s: missing %s" % (where, req)
        extra = schema.get("additionalProperties", True)
        for k, v in value.items():
            if k in props:
                check(v, props[k], "%s.%s" % (where, k))
            elif extra is False:
                raise AssertionError("%s: field %r is not described" % (where, k))
            elif isinstance(extra, dict) and extra:
                check(v, extra, "%s.%s" % (where, k))
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            check(v, schema["items"], "%s[%d]" % (where, i))


def _is(v, t):
    return {"string": isinstance(v, str), "integer": isinstance(v, int) and not isinstance(v, bool),
            "number": isinstance(v, (int, float)) and not isinstance(v, bool), "boolean": isinstance(v, bool),
            "array": isinstance(v, list), "object": isinstance(v, dict), "null": v is None}[t]


def test_the_checker_finds_undescribed_fields_and_wrong_types():
    sch = api_v1.s_obj({"a": api_v1.s_int()})
    check({"a": 1}, sch)
    with pytest.raises(AssertionError):
        check({"a": 1, "b": 2}, sch)
    with pytest.raises(AssertionError):
        check({"a": "1"}, sch)
    with pytest.raises(AssertionError):
        check({}, sch)


def concrete(env, path):
    dev = env.get("/api/v1/devices").json()["devices"]
    day = env.get("/api/v1/reports").json()["reports"][0]["day"]
    return (path.replace("{device_id}", next(d["id"] for d in dev if d["ip"]))
            .replace("{day}", day).replace("{subject}", "dev:" + next(d["id"] for d in dev if d["ip"])))


def test_real_answers_match_the_description(env):
    checked = 0
    for m, p, op in spec_ops(env.spec):
        if m != "GET":
            continue
        url = concrete(env, p)
        r = env.get(url)
        assert r.status_code == 200, (url, r.text[:300])
        check(r.json(), op["responses"]["200"]["content"]["application/json"]["schema"], m + " " + p)
        checked += 1
    assert checked >= 12
    # the sample network fills what the schemas describe (not only empty lists)
    assert env.get("/api/v1/devices").json()["count"] > 10
    assert env.get("/api/v1/ingest").json()["reports"]
    for lang in ("en", "de"):
        rep = env.get(concrete(env, "/api/v1/reports/{day}") + "?lang=" + lang).json()
        assert rep["sections"] and all(not s["title"].startswith("report.") for s in rep["sections"])


def test_the_ingest_answer_and_the_examples_match(env):
    ops = {(m, p): op for m, p, op in spec_ops(env.spec)}
    op = ops[("POST", "/api/v1/ingest")]
    r = env.post("/api/v1/ingest", json={"source": "contract", "title": "Contract test", "items": [{"text": "line", "level": "warn"}]},
                 headers={"X-Lanloom": "1"})
    assert r.status_code == 200, r.text
    check(r.json(), op["responses"]["200"]["content"]["application/json"]["schema"])
    check({"source": "a", "title": "b"}, op["requestBody"]["content"]["application/json"]["schema"])
    for m, p, op in spec_ops(env.spec):
        ex = op["responses"]["200"]["content"]["application/json"].get("example")
        if ex is not None:
            check(ex, op["responses"]["200"]["content"]["application/json"]["schema"], "example of %s %s" % (m, p))


def test_docs_list_every_route_and_every_webhook_event(env):
    text = (ROOT / "docs" / "API.md").read_text(encoding="utf-8")
    for m, p, _ in spec_ops(env.spec):
        assert "`%s %s`" % (m, p) in text, "docs/API.md misses %s %s" % (m, p)
    for ev in webhooks.EVENTS:
        assert "`%s`" % ev in text, "docs/API.md misses the event %s" % ev
    assert "X-Lanloom-Signature" in text and "X-Lanloom-Timestamp" in text
    manual = (ROOT / "docs" / "manual" / "api.md").read_text(encoding="utf-8")
    assert "Settings" in manual and "API.md" in manual
