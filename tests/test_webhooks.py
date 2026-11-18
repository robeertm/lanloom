"""Webhooks (lanloom/webhooks.py): signature, protection against replays, retries with a growing pause, the delivery log,
redirects never followed, events from the journal / the notification rules / the nightly report, secrets never shown
again, and the routes of Settings > API (administrators only, step-up for adding and new secrets)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from lanloom import access, auth, webhooks
from lanloom import config as config_mod
from lanloom.main import create_app
from lanloom.store import Store

HDR = {"X-Lanloom": "1"}


# ---------------------------------------------------------------- a real receiver on the loopback interface
class Receiver:
    def __init__(self):
        self.got = []
        self.answers = []                       # status codes to answer, in order; then 204
        rec = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                rec.got.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": self.rfile.read(n)})
                code = rec.answers.pop(0) if rec.answers else 204
                self.send_response(code)
                if 300 <= code < 400:
                    self.send_header("Location", "http://127.0.0.1:%d/elsewhere" % rec.port)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.url = "http://127.0.0.1:%d/hook" % self.port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def rx():
    r = Receiver()
    yield r
    r.close()


class Vault:
    def __init__(self):
        self.d = {}

    def put(self, k, v):
        self.d[k] = v

    def get(self, k):
        return self.d.get(k)

    def has(self, k):
        return k in self.d

    def delete(self, k):
        self.d.pop(k, None)


class Core:
    demo = None


@pytest.fixture()
def wh():
    store = Store(":memory:")
    clock = [1_800_000_000.0]
    w = webhooks.Webhooks(Core(), store, Vault(), clock=lambda: clock[0])
    w.clock_ = clock
    store.set_setting("setup_done", True)
    return w


def add(w, url, events=("device.new",)):
    return w.save({"name": "Receiver", "url": url, "events": list(events)}, by="admin")


def test_signature_and_replay_protection():
    body = b'{"id":"dlv_1","event":"ping"}'
    sig = webhooks.sign("whsec_x", 1_800_000_000, body)
    assert sig.startswith("v1=") and len(sig) == 3 + 64
    assert webhooks.verify("whsec_x", "1800000000", body, sig, now=1_800_000_100)
    assert not webhooks.verify("whsec_y", "1800000000", body, sig, now=1_800_000_100)              # other secret
    assert not webhooks.verify("whsec_x", "1800000000", body + b" ", sig, now=1_800_000_100)       # changed body
    assert not webhooks.verify("whsec_x", "1800000000", body, sig, now=1_800_000_000 + 301)        # replayed too late
    assert not webhooks.verify("whsec_x", "1800000001", body, sig, now=1_800_000_100)              # other timestamp
    assert not webhooks.verify("whsec_x", "nope", body, sig)


def test_delivery_is_signed_and_the_receiver_can_check_it(wh, rx):
    h = add(wh, rx.url)
    secret = h["secret"]
    assert secret.startswith("whsec_")
    wh.on_journal("dev_new", {"id": "d-1", "title": "Printer", "ip": "192.0.2.7", "kind": "printer", "vendor": "Example"}, wh.clock_[0])
    assert wh.deliver_due() == 1
    got = rx.got[0]
    hd = got["headers"]
    assert hd["x-lanloom-event"] == "device.new" and hd["content-type"] == "application/json"
    assert webhooks.verify(secret, hd["x-lanloom-timestamp"], got["body"], hd["x-lanloom-signature"], now=wh.clock_[0])
    payload = json.loads(got["body"])
    assert payload["event"] == "device.new" and payload["id"] == hd["x-lanloom-delivery"]
    assert payload["data"]["device"] == {"id": "d-1", "title": "Printer", "ip": "192.0.2.7", "kind": "printer", "vendor": "Example"}
    row = wh.deliveries()[0]
    assert row["status"] == "ok" and row["last_code"] == 204 and row["attempts"] == 1
    assert wh.list()[0]["last_ok"] == wh.clock_[0] and wh.list()[0]["fails"] == 0


def test_retries_with_a_growing_pause_then_failed(wh, rx):
    add(wh, rx.url)
    rx.answers = [500] * 20
    wh.emit("device.new", {"device": {"id": "d"}})
    pauses = []
    for i in range(webhooks.MAX_ATTEMPTS):
        assert wh.deliver_due() == 1
        row = wh.store.one("SELECT * FROM webhook_deliveries")
        if row["status"] == "pending":
            pauses.append(row["next_at"] - wh.clock_[0])
            assert wh.deliver_due() == 0                 # not due yet
            wh.clock_[0] = row["next_at"]
    assert pauses == list(webhooks.BACKOFF_S)
    row = wh.store.one("SELECT * FROM webhook_deliveries")
    assert row["status"] == "failed" and row["attempts"] == webhooks.MAX_ATTEMPTS and row["last_code"] == 500
    assert len(rx.got) == webhooks.MAX_ATTEMPTS
    ids = {g["headers"]["x-lanloom-delivery"] for g in rx.got}
    assert len(ids) == 1                                 # the same delivery id on every retry
    stamps = [int(g["headers"]["x-lanloom-timestamp"]) for g in rx.got]
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)       # a fresh timestamp on every attempt
    assert wh.redeliver(row["id"]) and wh.store.one("SELECT status FROM webhook_deliveries")["status"] == "pending"


def test_success_after_a_retry_and_410_stops_at_once(wh, rx):
    add(wh, rx.url)
    rx.answers = [503, 200]
    wh.emit("device.new", {})
    wh.deliver_due()
    wh.clock_[0] += webhooks.BACKOFF_S[0]
    wh.deliver_due()
    assert wh.store.one("SELECT status, attempts FROM webhook_deliveries") == {"status": "ok", "attempts": 2}
    rx.answers = [410]
    wh.emit("device.new", {})
    wh.deliver_due()
    assert wh.store.one("SELECT status, attempts FROM webhook_deliveries ORDER BY id DESC") == {"status": "failed", "attempts": 1}


def test_redirects_are_never_followed(wh, rx):
    add(wh, rx.url)
    rx.answers = [302]
    wh.emit("device.new", {})
    wh.deliver_due()
    assert [g["path"] for g in rx.got] == ["/hook"]          # never went to /elsewhere
    row = wh.deliveries()[0]
    assert row["status"] == "pending" and row["last_key"] == "webhooks.send.redirect" and row["last_code"] == 302


def test_unreachable_receiver_is_retried(wh):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    add(wh, "http://127.0.0.1:%d/x" % port)
    wh.emit("device.new", {})
    wh.deliver_due()
    row = wh.deliveries()[0]
    assert row["status"] == "pending" and row["last_key"] == "webhooks.send.unreachable"


def test_only_wanted_enabled_events_are_queued_and_old_rows_are_no_news(wh, rx):
    h = add(wh, rx.url, events=("wan.down", "wan.up"))
    t = wh.clock_[0]
    wh.on_journal("dev_new", {"id": "d"}, t)                          # not subscribed
    wh.on_journal("wan_down", {"name": "WAN1"}, t - 3600)             # about the past
    wh.on_journal("wan_down", {"name": "WAN1"}, t)
    wh.on_journal("wan_up", {"name": "WAN1"}, t)
    assert [r["event"] for r in wh.deliveries()] == ["wan.up", "wan.down"]
    wh.save({"id": h["id"], "name": "Receiver", "events": ["wan.down"], "enabled": False})
    assert wh.emit("wan.down", {}) == 0
    wh.store.set_setting("setup_done", False)
    wh.save({"id": h["id"], "name": "Receiver", "events": ["device.new"], "enabled": True})
    wh.on_journal("dev_new", {"id": "d"}, t)                          # the first scan finds everything: no news
    assert wh.emit("device.new", {}) == 1


def test_events_from_the_notification_rules_and_the_report(wh, rx):
    add(wh, rx.url, events=("backup.stale", "incident.opened", "incident.closed", "report.ready"))
    t = wh.clock_[0]
    wh.on_notify("backup_old", "backup:1", {"age_dur": 90000}, False, t)
    wh.on_notify("incident", "inc:1", {"title": "Uplink unstable", "severity": "warning"}, False, t)
    wh.on_notify("incident", "inc:2", {}, True, t)                    # never opened: no closed event
    wh.on_notify("incident", "inc:1", {}, True, t)
    wh.on_report({"day": "2027-01-15", "level": "warn", "points": 3, "created": t})
    evs = [r["event"] for r in reversed(wh.deliveries())]
    assert evs == ["backup.stale", "incident.opened", "incident.closed", "report.ready"]


def test_notifier_tap_fires_once_per_new_event(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    st = app.state.ll
    seen = []
    st.real_core.notify.taps.append(lambda *a: seen.append(a[:2] + (a[3],)))
    n = st.real_core.notify
    n.emit("backup_old", "backup:5", {"age_dur": 1}, once=True)
    n.emit("backup_old", "backup:5", {"age_dur": 1}, once=True)       # same key: no second event
    assert seen == [("backup_old", "backup:5", False)]
    assert st.webhooks.on_notify in n.taps and st.webhooks.on_journal in st.real_core.reports.journal.listeners
    assert st.webhooks.on_report in st.real_core.reports.report_listeners


def test_test_ping_secret_rotation_and_masked_address(wh, rx):
    h = add(wh, rx.url + "/s3cr3t-path?k=v")
    view = wh.list()[0]
    assert view["target"] == "http://127.0.0.1:%d/…" % rx.port and "s3cr3t" not in json.dumps(view) and "secret" not in view
    res = wh.test(h["id"], by="admin")
    assert res["ok"] and res["code"] == 204
    assert json.loads(rx.got[0]["body"])["event"] == "ping"
    old = h["secret"]
    new = wh.rotate(h["id"])["secret"]
    assert new != old and wh.vault.get("webhook:%s:secret" % h["id"]) == new
    rx.answers = [500]
    assert not wh.test(h["id"])["ok"]
    assert wh.store.one("SELECT COUNT(*) AS n FROM webhook_deliveries WHERE status='pending'")["n"] == 0     # tests are never retried


def test_bad_addresses_and_limits(wh):
    for url in ("", "ftp://x.example/", "http:///nohost", "https://user:pw@x.example/", "https://x.example/a b", "x" * 600):
        with pytest.raises(ValueError):
            add(wh, url)
    with pytest.raises(ValueError):
        wh.save({"name": "x", "url": "https://x.example/", "events": ["nope"]})
    with pytest.raises(ValueError):
        wh.save({"name": "", "url": "https://x.example/", "events": ["device.new"]})


def test_backlog_is_bounded_and_deleting_removes_secrets_and_log(wh, monkeypatch):
    monkeypatch.setattr(webhooks, "MAX_PENDING", 5)
    h = add(wh, "https://receiver.example/")
    for _ in range(8):
        wh.emit("device.new", {})
    st = {r["status"] for r in wh.store.query("SELECT status FROM webhook_deliveries")}
    assert wh.store.one("SELECT COUNT(*) AS n FROM webhook_deliveries WHERE status='pending'")["n"] == 5 and "dropped" in st
    assert wh.delete(h["id"])
    assert not wh.vault.d and not wh.store.query("SELECT * FROM webhook_deliveries")


def test_prune_keeps_a_month(wh):
    add(wh, "https://receiver.example/")
    wh.emit("device.new", {})
    wh.store.execute("UPDATE webhook_deliveries SET status='ok', created=?", (wh.clock_[0] - 40 * 86400,))
    wh.emit("device.new", {})
    wh.prune()
    assert wh.store.one("SELECT COUNT(*) AS n FROM webhook_deliveries")["n"] == 1


def test_sample_webhooks_of_the_demo_never_send(wh):
    webhooks.seed_demo(wh, now=wh.clock_[0])
    assert len(wh.list()) == 2 and all(h["sample"] for h in wh.list())
    assert wh.emit("device.new", {}) == 0


# ---------------------------------------------------------------- routes
@pytest.fixture()
def cl(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        c.st = app.state.ll
        c.uids = {r: c.st.accounts.create("user-" + r, "a-long-test-password", role=r) for r in access.ROLES}
        yield c


def sign_in(c, role, fresh=True):
    token = c.st.sessions.create(c.uids[role], ip="127.0.0.1", agent="test")
    if not fresh:
        c.st.store.execute("UPDATE sessions SET stepup_until=0 WHERE token_hash=?", (auth._hash_token(token),))
    c.cookies.clear()
    c.cookies.set(auth.SESSION_COOKIE, token)


def test_routes_admin_only_secret_once_and_audited(cl, rx):
    sign_in(cl, "operator")
    assert cl.get("/api/webhooks").status_code == 403
    sign_in(cl, "admin", fresh=False)
    body = {"name": "Hub", "url": rx.url, "events": ["device.new"]}
    assert cl.post("/api/webhooks", json=body, headers=HDR).json()["error"] == "auth.err.stepup"
    sign_in(cl, "admin")
    r = cl.post("/api/webhooks", json=body, headers=HDR)
    assert r.status_code == 200, r.text
    h = r.json()
    assert h["secret"].startswith("whsec_")
    listed = cl.get("/api/webhooks").json()
    assert h["secret"] not in json.dumps(listed) and rx.url not in json.dumps(listed)
    assert [e["id"] for e in listed["events"]][:2] == ["device.new", "device.gone"]
    t = cl.post("/api/webhooks/%s/test" % h["id"], json={}, headers=HDR).json()
    assert t["ok"] and rx.got
    r = cl.put("/api/webhooks/%s" % h["id"], json={"name": "Hub 2", "events": ["wan.down"], "enabled": False}, headers=HDR)
    assert r.status_code == 200 and r.json()["name"] == "Hub 2" and not r.json()["enabled"] and "secret" not in r.json()
    assert cl.get("/api/webhooks/%s/deliveries" % h["id"]).json()["deliveries"][0]["event"] == "ping"
    assert cl.post("/api/webhooks/%s/secret" % h["id"], json={}, headers=HDR).json()["secret"] != h["secret"]
    assert cl.delete("/api/webhooks/%s" % h["id"], headers=HDR).status_code == 200
    assert cl.delete("/api/webhooks/%s" % h["id"], headers=HDR).status_code == 404
    acts = [r["action"] for r in cl.st.store.query("SELECT action FROM audit WHERE action LIKE 'webhooks.%' ORDER BY id")]
    assert acts == ["webhooks.add", "webhooks.test", "webhooks.change", "webhooks.rotate", "webhooks.delete", "webhooks.delete"]
    blob = json.dumps(cl.st.store.query("SELECT * FROM audit"))
    assert h["secret"] not in blob and "/hook" not in blob


def test_webhook_log_and_settings_are_in_the_right_backup_parts(cl):
    from lanloom import backup
    assert "webhook_deliveries" in backup.INSTANCE_TABLES and "api_tokens" in backup.CONFIG_TABLES and "api_ingest" in backup.HISTORY_TABLES
