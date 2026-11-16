"""The AI routes in the running app: demo mode, access rules, rate limit, no secret in any answer."""
import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod
from lanloom.core import Core
from lanloom.main import create_app, load_i18n

from .fakes import KEY, Fake, FakeCore, device, ndjson, sse

HDR = {"X-Lanloom": "1"}
PW = "a-long-test-password"
PRODUCTS = ("openai", "anthropic", "gemini", "mistral", "ollama", "openrouter", "lm studio", "llama.cpp", "vllm", "gpt", "claude")


def open_app(tmp_path, monkeypatch, demo: bool):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1" if demo else "0")
    monkeypatch.setenv("LANLOOM_DEMO_SEED", "1" if demo else "0")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    if not demo:
        async def no_loops(self):
            return None
        monkeypatch.setattr(Core, "start", no_loops)
    return create_app(config_mod.load())


def sign_up(c, tmp_path):
    code = (Path(tmp_path) / "setup-code.txt").read_text().strip()
    r = c.post("/api/setup/account", json={"code": code, "username": "admin", "password": PW}, headers=HDR)
    assert r.status_code == 200, r.text


@pytest.fixture()
def demo(tmp_path, monkeypatch):
    app = open_app(tmp_path, monkeypatch, True)
    with TestClient(app, base_url="http://lanloom.test") as c:
        sign_up(c, tmp_path)
        c.ll = app.state.ll
        c.data_dir = tmp_path
        yield c


@pytest.fixture()
def live(tmp_path, monkeypatch):
    app = open_app(tmp_path, monkeypatch, False)
    with TestClient(app, base_url="http://lanloom.test") as c:
        sign_up(c, tmp_path)
        c.ll = app.state.ll
        c.data_dir = tmp_path
        yield c


def poll(c, job_id, want=("done", "error")):
    for _ in range(200):
        r = c.get("/api/ai/jobs/" + job_id)
        assert r.status_code == 200, r.text
        if r.json()["state"] in want:
            return r.json()
        time.sleep(0.02)
    raise AssertionError("job did not finish")


# ---------------------------------------------------------------------- access
def test_ai_routes_need_a_session(demo):
    demo.cookies.clear()
    gets = ["/api/ai", "/api/ai/local", "/api/ai/jobs/x"]
    posts = ["/api/ai/models", "/api/ai/test", "/api/ai/explain", "/api/ai/local/discover", "/api/ai/local/install", "/api/ai/local/use",
             "/api/ai/local/remove", "/api/ai/pairing"]
    for p in gets:
        assert demo.get(p).status_code == 401, p
    for p in posts:
        assert demo.post(p, json={}, headers=HDR).status_code == 401, p
    assert demo.put("/api/ai", json={"settings": {}}, headers=HDR).status_code == 401
    assert demo.get("/api/state").json()["ai"] == {}


def test_state_changing_ai_requests_need_the_header(demo):
    assert demo.put("/api/ai", json={"settings": {}}).status_code == 403
    assert demo.post("/api/ai/explain", json={}).status_code == 403


# ---------------------------------------------------------------------- demo
def test_demo_lists_only_sample_services_without_product_names(demo):
    r = demo.get("/api/ai")
    assert r.status_code == 200
    data = r.json()
    assert {p["kind"] for p in data["providers"]} == {"demo", "demo_cloud"} and data["demo"] is True
    low = r.text.lower()
    for name in PRODUCTS:
        assert name not in low, name
    assert data["settings"]["local"]["enabled"] is True and data["settings"]["public"]["enabled"] is False
    assert data["ready"] == {"device": True, "finding": True, "rule": True}


def test_state_carries_the_readiness(demo):
    ai = demo.get("/api/state").json()["ai"]
    assert set(ai) == {"device", "finding", "rule"} and all(isinstance(v, bool) for v in ai.values())


def test_demo_explain_device(demo):
    dev = demo.get("/api/devices").json()["devices"][0]
    r = demo.post("/api/ai/explain", json={"subject": "device", "ref": {"id": dev["id"]}, "lang": "en"}, headers=HDR)
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["state"] in ("queued", "running", "sending", "answering", "done") and job["id"] and job["kind"] == "explain"
    done = poll(demo, job["id"])
    assert done["state"] == "done" and done["result"]["text"] and done["result"]["route"] == "local" and done["result"]["pseudonymised"] is False
    assert done["result"]["model"] == "sample-medium"


def test_demo_explain_in_german(demo):
    dev = demo.get("/api/devices").json()["devices"][0]
    job = demo.post("/api/ai/explain", json={"subject": "device", "ref": {"id": dev["id"]}, "lang": "de"}, headers=HDR).json()
    text = poll(demo, job["id"])["result"]["text"]
    assert text and text != load_i18n("en")["ai.demo.answer.device"]


def test_preview_shows_the_text_and_starts_no_job(demo):
    dev = demo.get("/api/devices").json()["devices"][0]
    before = len(demo.ll.ai._jobs)
    r = demo.post("/api/ai/explain", json={"subject": "device", "ref": {"id": dev["id"]}, "preview": True}, headers=HDR)
    assert r.status_code == 200, r.text
    pv = r.json()
    assert "routes" in pv and pv["routes"][0]["scope"] == "local" and "Subject: device" in pv["routes"][0]["user"]
    assert len(demo.ll.ai._jobs) == before and "id" not in pv


def test_explain_rejects_unknown_things(demo):
    for body in ({"subject": "nonsense"}, {"subject": "device", "ref": {"id": "missing"}}, {"subject": "finding", "ref": {"day": "2020-01-01", "item": "x"}},
                 {"subject": "rule", "ref": {"source": "k", "id": "1"}}, {}):
        r = demo.post("/api/ai/explain", json=body, headers=HDR)
        assert r.status_code == 400 and r.json()["error"] == "ai.err.subject", body
    assert demo.get("/api/ai/jobs/nope").status_code == 404
    assert demo.get("/api/ai/jobs/nope").json()["error"] == "ai.err.job"


def test_explain_a_finding_and_a_rule(demo, monkeypatch):
    core = demo.ll.core
    core.reports.save({"day": "2026-11-15", "created": 1.0, "since": 0.0, "until": 1.0, "points": 1, "level": "warn",
                       "sections": [{"id": "s", "title": "report.sec.lanloom", "items": [
                           {"id": "x1", "key": "report.self.started", "vars": {"at_at": 1700000000}, "level": "warn", "point": True}]}]})
    r = demo.post("/api/ai/explain", json={"subject": "finding", "ref": {"day": "2026-11-15", "item": "x1"}}, headers=HDR)
    assert r.status_code == 200, r.text
    assert poll(demo, r.json()["id"])["state"] == "done"
    monkeypatch.setattr(core, "live_configs", lambda: [{"key": "ctl", "state": {"policies": [
        {"id": "p1", "name": "Block guests", "action": "block", "src": {"zone": "z1"}, "dst": {"zone": "z2"}, "enabled": True}],
        "zones": [{"id": "z1", "name": "Guests"}, {"id": "z2", "name": "Internal"}]}}])
    r = demo.post("/api/ai/explain", json={"subject": "rule", "ref": {"source": "ctl", "id": "p1"}}, headers=HDR)
    assert r.status_code == 200, r.text
    assert poll(demo, r.json()["id"])["state"] == "done"
    pv = demo.post("/api/ai/explain", json={"subject": "rule", "ref": {"source": "ctl", "id": "p1"}, "preview": True}, headers=HDR).json()
    assert "Rule: Block guests" in pv["routes"][0]["user"] and "From: Guests" in pv["routes"][0]["user"]


def test_put_rejects_a_wrong_kind(demo):
    for settings in ({"local": {"kind": "openai"}}, {"public": {"kind": "ollama"}}, {"local": {"kind": "nope"}}, {"public": {"kind": "openai"}}):
        r = demo.put("/api/ai", json={"settings": settings}, headers=HDR)
        assert r.status_code == 400 and r.json()["error"] == "ai.err.provider", settings


def test_put_keeps_valid_changes(demo):
    r = demo.put("/api/ai", json={"settings": {"daily_limit": 7, "allow": {"explain_device": True}, "order": "public_first"}}, headers=HDR)
    assert r.status_code == 200
    s = demo.get("/api/ai").json()["settings"]
    assert s["daily_limit"] == 7 and s["allow"]["explain_device"] is True and s["order"] == "public_first"
    r = demo.put("/api/ai", json={"settings": {"daily_limit": "x"}}, headers=HDR)
    assert r.status_code == 400 and r.json()["error"] == "ai.err.limit"
    assert demo.put("/api/ai", json={"settings": "junk", "secrets": 5}, headers=HDR).status_code == 200


def test_internal_errors_have_no_detail(demo, monkeypatch):
    def boom(*a):
        raise ValueError("secret detail " + KEY)
    monkeypatch.setattr(demo.ll.ai, "update_settings", boom)
    r = demo.put("/api/ai", json={"settings": {}}, headers=HDR)
    assert r.status_code == 400 and r.json()["error"] == "ai.err.internal" and KEY not in r.text


def test_local_endpoints_in_demo(demo):
    r = demo.get("/api/ai/local")
    assert r.status_code == 200 and set(r.json()) >= {"setup", "sidecar", "suggestions", "discovered"}
    d = demo.post("/api/ai/local/discover", json={}, headers=HDR).json()
    assert d["found"] and "sample" in json.dumps(d).lower()
    r = demo.post("/api/ai/local/install", json={}, headers=HDR)
    assert r.status_code == 200 and r.json()["state"] in ("installing", "pulling")
    for _ in range(100):
        st = demo.get("/api/ai/local").json()["setup"]
        if st["state"] == "ready":
            break
        time.sleep(0.1)
    assert st["state"] == "ready"
    assert demo.post("/api/ai/local/remove", json={}, headers=HDR).json()["state"] == "idle"


# ---------------------------------------------------------------------- pairing endpoints
def new_code(c):
    r = c.post("/api/ai/pairing", json={}, headers=HDR)
    assert r.status_code == 200
    return r.json()["code"]


def test_installer_download(demo):
    code = new_code(demo)
    demo.cookies.clear()                                   # the other computer has no session
    r = demo.get("/api/ai/installer/linux", params={"code": code})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "attachment" in r.headers["content-disposition"] and code in r.text and "http://lanloom.test" in r.text
    assert not re.search(r"__[A-Z_]+__", r.text)
    assert demo.get("/api/ai/installer/windows", params={"code": code}).text.count(code) >= 1
    assert demo.get("/api/ai/installer/beos", params={"code": code}).status_code == 400


def test_installer_with_a_wrong_code_is_refused(demo):
    new_code(demo)
    demo.cookies.clear()
    r = demo.get("/api/ai/installer/linux?code=falsch")
    assert r.status_code == 403 and r.json()["error"] == "ai.err.code"
    assert demo.get("/api/ai/installer/linux").status_code == 403


def test_pair_endpoints_refuse_a_wrong_code(demo):
    new_code(demo)
    demo.cookies.clear()
    for path, body in (("/api/ai/pair/hello", {"code": "AAAA-BBBB", "hw": {}}), ("/api/ai/pair", {"code": "AAAA-BBBB", "model": "m"}),
                       ("/api/ai/pair/status", {"code": "AAAA-BBBB", "job": "x"})):
        r = demo.post(path, json=body, headers=HDR)
        assert r.status_code == 403 and r.json()["error"] == "ai.err.code", path


def test_pair_endpoints_work_with_the_code_and_without_a_session(demo):
    code = new_code(demo)
    demo.cookies.clear()
    r = demo.post("/api/ai/pair/hello", json={"code": code, "hw": {"ram_gb": 16, "cores": 8, "gpu": "", "arch": "x86_64"}}, headers=HDR)
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["model"]
    # the test client is not a private address: Lanloom refuses to reach out to it
    r = demo.post("/api/ai/pair", json={"code": code, "model": "llama3.1:8b"}, headers=HDR)
    assert r.status_code == 400 and r.json()["error"] == "ai.err.peer"


def test_repeated_wrong_codes_are_rate_limited(demo):
    code = new_code(demo)
    demo.cookies.clear()
    codes = [demo.post("/api/ai/pair/hello", json={"code": "AAAA-BBBB"}, headers=HDR).status_code for _ in range(6)]
    assert codes[:5] == [403] * 5 and codes[5] == 429
    r = demo.post("/api/ai/pair/hello", json={"code": code}, headers=HDR)          # even the right code waits
    assert r.status_code == 429 and r.json()["error"] == "auth.err.rate" and r.json()["wait"] > 0
    assert demo.get("/api/ai/installer/linux", params={"code": code}).status_code == 429
    assert demo.post("/api/ai/pair/status", json={"code": code, "job": "x"}, headers=HDR).status_code == 429
    assert demo.post("/api/ai/pair", json={"code": code, "model": "m"}, headers=HDR).status_code == 429


def test_installer_failures_count_too(demo):
    demo.cookies.clear()
    codes = [demo.get("/api/ai/installer/linux?code=falsch").status_code for _ in range(6)]
    assert codes[:5] == [403] * 5 and codes[5] == 429


def test_pair_status_never_shows_a_job_of_another_kind(demo):
    dev = demo.get("/api/devices").json()["devices"][0]
    job = demo.post("/api/ai/explain", json={"subject": "device", "ref": {"id": dev["id"]}}, headers=HDR).json()
    code = new_code(demo)
    demo.cookies.clear()
    assert demo.post("/api/ai/pair/status", json={"code": code, "job": job["id"]}, headers=HDR).status_code == 403


def test_pairing_jobs_are_not_readable_through_the_job_route(live):
    ai = live.ll.ai
    fake = Fake()
    ai.opener = fake
    fake.on("/api/version", {"version": "0.5.1"})
    fake.on("/api/tags", {"models": [{"name": "llama3.1:8b"}]})
    fake.on("/api/chat", ndjson({"message": {"content": "OK"}, "done": True}))
    code = ai.new_pairing()["code"]
    job = ai.pair(code, "192.168.1.50", {"model": "llama3.1:8b"})
    assert live.get("/api/ai/jobs/" + job["id"]).status_code == 404


# ---------------------------------------------------------------------- no secret in any answer
def test_no_answer_ever_holds_a_key(live):
    ai = live.ll.ai
    fake = Fake()
    ai.opener = fake
    ai.core = FakeCore([device("dev1")])
    fake.on("/models", {"data": [{"id": "gpt-4.1-mini"}]})
    fake.on("/chat/completions", sse({"choices": [{"delta": {"content": "Looks fine."}}]}))
    # the key goes in through the settings route once and never comes back
    r = live.put("/api/ai", json={"settings": {"public": {"kind": "openai", "model": "gpt-4.1-mini", "enabled": True},
                                               "allow": {"explain_device": True}, "daily_limit": 5},
                                  "secrets": {"public_key": KEY}}, headers=HDR)
    assert r.status_code == 200 and r.json()["settings"]["public"]["has_key"] is True and KEY not in r.text
    texts = []

    def get(path):
        r = live.get(path)
        texts.append(r.text)
        return r
    for path in ("/api/ai", "/api/ai/local", "/api/state", "/api/ai/jobs/none", "/api/settings", "/api/integrations", "/api/update"):
        get(path)
    r = live.post("/api/ai/models", json={"scope": "public", "kind": "openai"}, headers=HDR)
    assert r.json()["models"] == ["gpt-4.1-mini"]
    texts.append(r.text)
    pv = live.post("/api/ai/explain", json={"subject": "device", "ref": {"id": "dev1"}, "preview": True}, headers=HDR)
    assert pv.status_code == 200 and pv.json()["routes"][0]["scope"] == "public"
    texts.append(pv.text)
    r = live.post("/api/ai/explain", json={"subject": "device", "ref": {"id": "dev1"}}, headers=HDR)
    texts.append(r.text)
    done = poll(live, r.json()["id"])
    assert done["state"] == "done" and done["result"]["pseudonymised"] is True
    texts.append(json.dumps(done))
    r = live.post("/api/ai/test", json={"scope": "public", "kind": "openai", "model": "gpt-4.1-mini"}, headers=HDR)
    texts.append(r.text)
    poll(live, r.json()["id"])
    texts.append(live.get("/api/ai/jobs/" + r.json()["id"]).text)
    # a key typed into the test form is used but not stored or returned
    typed = "sk-typed-ONLY-for-this-call-0987"
    r = live.post("/api/ai/models", json={"scope": "public", "kind": "mistral", "key": typed}, headers=HDR)
    texts.append(r.text)
    for t in texts:
        assert KEY not in t and typed not in t
    # what the services received carries the key only in its header
    for call in fake.calls:
        assert KEY not in call.url and KEY not in json.dumps(call.body)
    # nothing readable on disk
    raw = b"".join(p.read_bytes() for p in Path(live.data_dir).glob("lanloom.db*"))
    assert KEY.encode() not in raw and typed.encode() not in raw


def test_the_real_address_never_reaches_a_public_service_through_the_api(live):
    ai = live.ll.ai
    fake = Fake().on("/chat/completions", sse({"choices": [{"delta": {"content": "ok"}}]}))
    ai.opener = fake
    ai.core = FakeCore([device("dev1", "192.168.1.77")])
    live.put("/api/ai", json={"settings": {"public": {"kind": "openai", "model": "m", "enabled": True}, "allow": {"explain_device": True}},
                              "secrets": {"public_key": KEY}}, headers=HDR)
    pv = live.post("/api/ai/explain", json={"subject": "device", "ref": {"id": "dev1"}, "preview": True}, headers=HDR).json()
    assert "192.168.1.77" not in json.dumps(pv)
    r = live.post("/api/ai/explain", json={"subject": "device", "ref": {"id": "dev1"}}, headers=HDR)
    poll(live, r.json()["id"])
    sent = fake.calls[-1].body["messages"][1]["content"]
    assert "192.168.1.77" not in sent and "aa:bb:cc:dd:ee:01" not in sent and sent == pv["routes"][0]["user"]


def test_ready_flags_follow_the_permissions(live):
    assert live.get("/api/state").json()["ai"] == {"device": False, "finding": False, "rule": False}
    live.put("/api/ai", json={"settings": {"public": {"kind": "openai", "model": "m", "enabled": True}, "allow": {"explain_rule": True}},
                              "secrets": {"public_key": KEY}}, headers=HDR)
    assert live.get("/api/state").json()["ai"] == {"device": False, "finding": False, "rule": True}


# ---------------------------------------------------------------------- texts
def test_every_error_and_hint_key_in_the_code_is_translated():
    en, de = load_i18n("en"), load_i18n("de")
    root = Path(__file__).resolve().parents[2] / "lanloom"
    keys = set()
    for f in list((root / "ai").glob("*.py")) + [root / "main.py"]:
        keys |= set(re.findall(r"""["'](ai\.(?:err|hw|report|demo\.answer)\.[a-z_.]+)["']""", f.read_text(encoding="utf-8")))
    keys |= {"ai.demo.answer." + s for s in ("device", "finding", "rule")}
    missing = sorted(k for k in keys if k not in en or k not in de)
    assert not missing, missing
    assert len(keys) > 30
