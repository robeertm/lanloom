"""The KI2 routes in the running app: access, jobs, preview, audit without the question, bad input."""
import json

import pytest

from lanloom import auth
from lanloom.firewall.adapter import sample_state

from .fakes import KEY, Fake, FakeCore, device, sse
from .test_api import HDR, PW, demo, live, poll  # noqa: F401  (fixtures)
from .test_features import FakeFirewall, FakeReports, make_report

SECRET_QUESTION = "Why is the printer in the attic so quiet (zebra-4711)?"
POSTS = [("/api/ai/ask", {"question": "hi"}), ("/api/ai/report", {"day": "2026-11-15"}), ("/api/ai/identify", {"id": "dev1"}),
         ("/api/ai/firewall", {})]


def as_role(c, role):
    uid = c.ll.accounts.create("kia-" + role, PW, role=role)
    token = c.ll.sessions.create(uid, ip="127.0.0.1", agent="test")
    c.cookies.clear()
    c.cookies.set(auth.SESSION_COOKIE, token)


@pytest.fixture()
def wired(live):
    ai = live.ll.ai
    ai.opener = Fake().on("/chat/completions", sse({"choices": [{"delta": {"content": "[D1] is fine. [view:paths] [D99]"}}]}))
    ai.core = FakeCore([device("dev1", "192.168.1.77")])
    ai.core.reports = FakeReports(make_report())
    ai.core.ai = ai
    ai.firewall = FakeFirewall(sample_state())
    r = live.put("/api/ai", json={"settings": {"public": {"kind": "openai", "model": "m", "enabled": True},
                                               "allow": {f: True for f in ("ask", "report_plain", "identify", "suggest_firewall")},
                                               "daily_limit": 50}, "secrets": {"public_key": KEY}}, headers=HDR)
    assert r.status_code == 200, r.text
    live.fake = ai.opener
    return live


def test_new_routes_need_a_session(demo):
    demo.cookies.clear()
    for path, body in POSTS:
        assert demo.post(path, json=body, headers=HDR).status_code == 401, path
    assert demo.get("/api/ai/hints").status_code == 401
    assert demo.post("/api/ai/hints/dev1", json={"action": "dismiss"}, headers=HDR).status_code == 401


def test_new_routes_need_the_header(wired):
    for path, body in POSTS:
        assert wired.post(path, json=body).status_code == 403, path


def test_viewer_gets_403_everywhere(wired):
    as_role(wired, "viewer")
    for path, body in POSTS + [("/api/ai/hints/dev1", {"action": "dismiss"})]:
        r = wired.post(path, json=body, headers=HDR)
        assert r.status_code == 403 and r.json()["error"] == "auth.err.role", path
    assert wired.get("/api/ai/hints").status_code == 403
    assert wired.fake.calls == []


def test_operator_may_use_them(wired):
    as_role(wired, "operator")
    assert wired.get("/api/ai/hints").status_code == 200
    for path, body in POSTS:
        r = wired.post(path, json=body, headers=HDR)
        assert r.status_code == 200 and r.json()["id"], (path, r.text)


@pytest.mark.parametrize("path,body,kind", [("/api/ai/ask", {"question": "hi"}, "ask"), ("/api/ai/report", {"day": "2026-11-15"}, "report_plain"),
                                            ("/api/ai/identify", {"id": "dev1"}, "identify"), ("/api/ai/firewall", {}, "firewall")])
def test_routes_return_a_job_that_finishes(wired, path, body, kind):
    r = wired.post(path, json=body, headers=HDR)
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["id"] and job["kind"] == kind
    done = poll(wired, job["id"])
    assert done["state"] == "done", done


def test_ask_answer_links_through_the_api(wired):
    job = wired.post("/api/ai/ask", json={"question": "how is it?", "lang": "en"}, headers=HDR).json()
    out = poll(wired, job["id"])["result"]
    assert {l["href"] for l in out["links"]} == {"#/devices/dev1", "#/paths"}
    assert "D99" not in out["text"]


@pytest.mark.parametrize("path,body", [("/api/ai/ask", {"question": SECRET_QUESTION}), ("/api/ai/report", {"day": "2026-11-15"}),
                                       ("/api/ai/identify", {"id": "dev1"}), ("/api/ai/firewall", {})])
def test_preview_returns_routes_and_sends_nothing(wired, path, body):
    before_jobs, before_calls = len(wired.ll.ai._jobs), len(wired.fake.calls)
    r = wired.post(path, json=dict(body, preview=True), headers=HDR)
    assert r.status_code == 200, r.text
    pv = r.json()
    assert pv["routes"] and pv["routes"][0]["scope"] == "public" and "id" not in pv
    assert len(wired.ll.ai._jobs) == before_jobs and len(wired.fake.calls) == before_calls
    assert "192.168.1.77" not in json.dumps(pv)


def test_ask_preview_shows_the_question_and_facts(wired):
    pv = wired.post("/api/ai/ask", json={"question": SECRET_QUESTION, "preview": True}, headers=HDR).json()
    assert "zebra-4711" in pv["routes"][0]["user"] and "Network overview" in pv["routes"][0]["user"]


def test_ask_audit_entry_has_no_question_text(wired):
    hist = [{"role": "user", "text": "earlier-secret-9999"}]
    r = wired.post("/api/ai/ask", json={"question": SECRET_QUESTION, "history": hist}, headers=HDR)
    poll(wired, r.json()["id"])
    wired.post("/api/ai/ask", json={"question": SECRET_QUESTION, "preview": True}, headers=HDR)
    rows = [e for e in wired.ll.audit.query({}, 50) if e["action"] == "ai.ask"]
    assert len(rows) == 2
    blob = json.dumps(rows)
    assert "zebra" not in blob and "earlier-secret" not in blob and "printer" not in blob.lower()
    assert any(e["detail"].get("turns") == 1 for e in rows)


def test_no_question_is_a_clean_error(wired):
    r = wired.post("/api/ai/ask", json={"question": "  "}, headers=HDR)
    assert r.status_code == 400 and r.json()["error"] == "ai.err.question"


def test_not_ready_without_permission(wired):
    wired.put("/api/ai", json={"settings": {"allow": {"ask": False}}}, headers=HDR)
    r = wired.post("/api/ai/ask", json={"question": "hi"}, headers=HDR)
    assert r.status_code in (400, 409) and r.json()["error"] == "ai.err.not_ready"


def test_hint_actions_and_bad_action(wired):
    wired.ll.ai.hints.put("dev1", {"state": "open", "kind": "camera", "what": "c", "reason": "r", "t": 5})
    hints = wired.get("/api/ai/hints").json()["hints"]
    assert [h["device"] for h in hints] == ["dev1"]
    r = wired.post("/api/ai/hints/dev1", json={"action": "explode"}, headers=HDR)
    assert r.status_code == 400 and r.json()["error"] == "ai.err.hint_action"
    r = wired.post("/api/ai/hints/dev1", json={}, headers=HDR)
    assert r.status_code == 400
    assert wired.post("/api/ai/hints/dev1", json={"action": "accept"}, headers=HDR).json()["state"] == "accepted"
    assert wired.get("/api/ai/hints").json()["hints"] == []
    r = wired.post("/api/ai/hints/ghost", json={"action": "accept"}, headers=HDR)
    assert r.status_code == 400 and r.json()["error"] == "ai.err.hint_missing"
    assert wired.post("/api/ai/hints/dev1", json={"action": "forget"}, headers=HDR).json() == {"state": "none"}


def test_hint_audit_entry_names_device_and_action(wired):
    wired.ll.ai.hints.put("dev1", {"state": "open", "kind": "camera", "t": 5})
    wired.post("/api/ai/hints/dev1", json={"action": "dismiss"}, headers=HDR)
    row = next(e for e in wired.ll.audit.query({}, 20) if e["action"] == "ai.hint")
    assert row["detail"]["device"] == "dev1" and row["detail"]["action"] == "dismiss"


def test_firewall_route_refuses_without_a_firewall(wired):
    wired.ll.ai.firewall = None
    r = wired.post("/api/ai/firewall", json={}, headers=HDR)
    assert r.status_code == 400 and r.json()["error"] == "ai.err.no_firewall"


def test_ai_never_touches_the_firewall_through_the_api(wired):
    fw = wired.ll.ai.firewall
    for path, body in POSTS:
        poll(wired, wired.post(path, json=body, headers=HDR).json()["id"])
    assert fw.other_calls == []


def test_state_carries_the_new_readiness_flags(wired):
    ai = wired.get("/api/state").json()["ai"]
    assert {"ask", "report", "identify", "firewall"} <= set(ai) and all(ai[k] is True for k in ("ask", "report", "identify", "firewall"))


def test_no_key_in_any_new_answer(wired):
    texts = []
    for path, body in POSTS:
        r = wired.post(path, json=body, headers=HDR)
        texts += [r.text, json.dumps(poll(wired, r.json()["id"]))]
    texts.append(wired.get("/api/ai/hints").text)
    assert all(KEY not in t for t in texts)
    assert all(KEY not in json.dumps(c.body) for c in wired.fake.calls)
