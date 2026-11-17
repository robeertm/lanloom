"""Notification rules with a fixed clock: default calm, immediate/hourly, quiet hours, planned maintenance, flap
protection (hold + cool-down), all-clear only after an announced outage, daily limit, personal events, channel fakes,
web push (RFC 8291 test vector, VAPID token, subscription checks) and the heartbeat.

Never the wall clock; names and addresses are sample data (192.168.50.0/24).
"""
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from lanloom import core as core_mod
from lanloom.crypto import KEY_BYTES, Vault
from lanloom.nightly import Reports
from lanloom.notify import Notifier, in_span, webpush
from lanloom.store import Store

TZ = "Europe/Amsterdam"
Z = ZoneInfo(TZ)


def at(y, mo, d, hh=0, mm=0, ss=0) -> float:
    return datetime(y, mo, d, hh, mm, ss, tzinfo=Z).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Fake:
    """Channel fake: records every message; ``fail`` makes a channel refuse."""

    def __init__(self):
        self.sent = []
        self.fail = set()

    def __call__(self, ch, user, msg):
        self.sent.append((ch, user["id"], msg))
        return (False, "report.send.unreachable") if ch in self.fail else (True, "report.send.ok")


def make(tmp_path, t):
    store = Store(":memory:")
    cfg = SimpleNamespace(data_dir=Path(tmp_path), host="127.0.0.1", port=0, update_check=False, demo=False, probe=False,
                          release_feed="", image_repo="", cookie_secure="never", trusted_origins=(), trusted_proxies=())
    vault = Vault(store, b"k" * KEY_BYTES)
    c = core_mod.Core(cfg, store, vault)
    clock = Clock(t)
    c.reports = Reports(c, Path(tmp_path), clock=clock)
    c.reports.cert_check = lambda: []
    store.set_setting("report_cfg", {"time": "06:30", "tz": TZ, "keep_days": 30, "link": "https://lanloom.example"})
    store.set_setting("setup_done", True)
    n = Notifier(c, clock=clock)
    c.notify = n
    fake = Fake()
    n.sender = fake
    # the webhook is the one shared channel set up; web push needs a subscription (faked as usable below)
    vault.put("report:webhook:url", "https://hooks.example/abc")
    return c, n, clock, fake


def add_user(c, name, role="viewer"):
    cur = c.store.execute("INSERT INTO users(username, pw_hash, user_handle, created, role) VALUES(?,?,?,?,?)",
                          (name, "!x", b"h" + name.encode(), 0, role))
    return cur.lastrowid


def rule(n, uid, eid, mode, chans=("webhook",), mn="info"):
    n.update_prefs(uid, {"rules": {eid: {"mode": mode, "channels": list(chans), "min": mn}}})


def status(c, occ, uid):
    row = c.store.one("SELECT outcome FROM notify_log WHERE id=?", (occ,))
    out = json.loads(row["outcome"])
    return out.get(str(uid)) or out.get("*")


# ---------------------------------------------------------------- defaults: calm
def test_default_is_nightly_and_nothing_leaves(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    uid = add_user(c, "ann")
    occ = n.emit("dev_new", "new:d1", {"title": "Laptop"})
    n.drain()
    assert fake.sent == []
    assert status(c, occ, uid) == "nightly"
    assert n.prefs(uid)["rules"]["wan_down"]["mode"] == "nightly"


def test_immediate_sends_with_text_and_link(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    uid = add_user(c, "ann")
    rule(n, uid, "dev_new", "immediate")
    occ = n.emit("dev_new", "new:d1", {"title": "Laptop", "ip": "192.168.50.7"}, device="d1")
    n.drain()
    assert len(fake.sent) == 1
    ch, who, msg = fake.sent[0]
    assert ch == "webhook" and who == uid
    assert "Laptop" in msg["text"] and msg["url"] == "https://lanloom.example/#/devices/d1"
    assert status(c, occ, uid) == "sent"


def test_min_severity_keeps_low_events_in_the_report(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    uid = add_user(c, "ann")
    rule(n, uid, "dev_offline", "immediate", mn="warning")
    n.emit("dev_offline", "off:phone", {"title": "Phone"}, severity="info")
    clock.t += 600
    n.tick()
    n.drain()
    assert fake.sent == []


def test_failed_delivery_is_marked(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    uid = add_user(c, "ann")
    rule(n, uid, "dev_new", "immediate")
    fake.fail.add("webhook")
    occ = n.emit("dev_new", "new:d1", {"title": "Laptop"})
    n.drain()
    assert status(c, occ, uid) == "failed"


def test_no_usable_channel(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    uid = add_user(c, "ann")
    rule(n, uid, "dev_new", "immediate", chans=("webpush",))      # no browser subscribed
    occ = n.emit("dev_new", "new:d1", {"title": "Laptop"})
    assert status(c, occ, uid) == "nochannel"


# ---------------------------------------------------------------- quiet hours
def test_quiet_hours_only_immediate_critical_passes(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 23, 30))
    uid = add_user(c, "ann")
    n.update_prefs(uid, {"quiet": {"enabled": True, "from": "22:00", "to": "07:00"}})
    rule(n, uid, "dev_new", "immediate")
    rule(n, uid, "dev_new_protected", "immediate")
    a = n.emit("dev_new", "new:d1", {"title": "Laptop"})
    b = n.emit("dev_new_protected", "new:d2", {"title": "Unknown"})
    n.drain()
    assert status(c, a, uid) == "quiet"
    assert status(c, b, uid) == "sent"
    assert [m[2]["event"] for m in fake.sent] == ["dev_new_protected"]


def test_quiet_span_crossing_midnight():
    assert in_span(23 * 60, "22:00", "07:00") and in_span(6 * 60 + 59, "22:00", "07:00")
    assert not in_span(7 * 60, "22:00", "07:00") and not in_span(12 * 60, "22:00", "07:00")
    assert in_span(13 * 60, "12:00", "14:00") and not in_span(14 * 60, "12:00", "14:00")


def test_quiet_hours_validation(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    uid = add_user(c, "ann")
    with pytest.raises(ValueError):
        n.update_prefs(uid, {"quiet": {"enabled": True, "from": "22:00", "to": "22:00"}})
    with pytest.raises(ValueError):
        n.update_prefs(uid, {"quiet": {"from": "25:00"}})
    with pytest.raises(ValueError):
        n.update_prefs(uid, {"rules": {"dev_new": {"mode": "loud"}}})
    with pytest.raises(ValueError):
        n.update_prefs(uid, {"rules": {"nope": {"mode": "immediate"}}})


# ---------------------------------------------------------------- hourly bundles
def test_hourly_bundle_one_message_at_the_full_hour(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12, 5))
    uid = add_user(c, "ann")
    rule(n, uid, "dev_new", "hourly")
    n.tick()                                  # this hour counts as flushed
    occs = [n.emit("dev_new", "new:d%d" % i, {"title": "Device %d" % i}) for i in range(3)]
    clock.t = at(2026, 11, 17, 12, 59)
    n.tick()
    n.drain()
    assert fake.sent == []
    clock.t = at(2026, 11, 17, 13, 0, 5)
    n.tick()
    n.drain()
    assert len(fake.sent) == 1
    msg = fake.sent[0][2]
    assert msg["event"] == "bundle" and msg["text"].count("•") == 3
    assert all(status(c, o, uid) == "sent" for o in occs)


# ---------------------------------------------------------------- flap protection
def test_hold_drops_a_flap_and_sends_a_lasting_outage(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    uid = add_user(c, "ann")
    rule(n, uid, "wan_down", "immediate")
    occ = n.emit("wan_down", "wan", {"name": "wan0"})
    clock.t += 120
    n.tick()
    n.emit("wan_down", "wan", resolve=True)            # back after two minutes: only a flap
    n.drain()
    assert fake.sent == [] and status(c, occ, uid) == "flap"
    occ2 = n.emit("wan_down", "wan", {"name": "wan0"})
    clock.t += 179
    n.tick()
    assert fake.sent == []
    clock.t += 2
    n.tick()
    n.drain()
    assert len(fake.sent) == 1 and status(c, occ2, uid) == "sent"


def test_all_clear_only_after_announced_outage_and_cooldown(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    uid = add_user(c, "ann")
    rule(n, uid, "dev_offline", "immediate")
    n.emit("dev_offline", "off:nas", {"title": "NAS"}, device="nas")
    clock.t += 200
    n.tick()
    n.drain()
    assert len(fake.sent) == 1
    clock.t += 600
    n.emit("dev_offline", "off:nas", resolve=True)
    n.drain()
    assert len(fake.sent) == 2
    assert "NAS" in fake.sent[1][2]["text"] and fake.sent[1][2]["severity"] == "info"
    # down again within the 12 h cool-down: nightly report only, and no all-clear later
    occ = n.emit("dev_offline", "off:nas", {"title": "NAS"}, device="nas")
    clock.t += 200
    n.tick()
    n.drain()
    assert status(c, occ, uid) == "cooldown" and len(fake.sent) == 2
    n.emit("dev_offline", "off:nas", resolve=True)
    n.drain()
    assert len(fake.sent) == 2
    # after the cool-down it is news again
    clock.t += 13 * 3600
    n.tick()
    n.emit("dev_offline", "off:nas", {"title": "NAS"}, device="nas")
    clock.t += 200
    n.tick()
    n.drain()
    assert len(fake.sent) == 3


def test_no_all_clear_for_an_outage_nobody_was_told(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    uid = add_user(c, "ann")                               # default: nightly
    n.emit("wan_down", "wan", {"name": "wan0"})
    clock.t += 600
    n.tick()
    n.emit("wan_down", "wan", resolve=True)
    n.drain()
    assert fake.sent == []


# ---------------------------------------------------------------- planned maintenance
def test_maintenance_window_suppresses_and_marks(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    uid = add_user(c, "ann")
    rule(n, uid, "dev_offline", "immediate")
    w = n.add_window({"kind": "once", "title": "Switch firmware", "start": clock.t - 60, "end": clock.t + 3600})
    occ = n.emit("dev_offline", "off:sw", {"title": "Switch"}, device="sw")
    clock.t += 600
    n.tick()
    n.emit("dev_offline", "off:sw", resolve=True)
    n.drain()
    assert fake.sent == []
    assert status(c, occ, uid) == "maintenance"
    assert c.store.one("SELECT maint FROM notify_log WHERE id=?", (occ,))["maint"] == w["id"]
    assert n.maintenance_at(clock.t - 300)["id"] == w["id"]
    assert n.maintenance_at(clock.t + 7200) is None


def test_window_for_some_devices_only(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    uid = add_user(c, "ann")
    rule(n, uid, "dev_offline", "immediate")
    n.add_window({"kind": "once", "start": clock.t - 60, "end": clock.t + 3600, "devices": ["sw"]})
    a = n.emit("dev_offline", "off:sw", {"title": "Switch"}, device="sw")
    b = n.emit("dev_offline", "off:nas", {"title": "NAS"}, device="nas")
    clock.t += 200
    n.tick()
    n.drain()
    assert status(c, a, uid) == "maintenance" and status(c, b, uid) == "sent"


def test_hold_that_ends_inside_a_window_is_maintenance(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    uid = add_user(c, "ann")
    rule(n, uid, "wan_down", "immediate")
    occ = n.emit("wan_down", "wan", {"name": "wan0"})
    n.add_window({"kind": "once", "start": clock.t + 30, "end": clock.t + 3600})
    clock.t += 200
    n.tick()
    n.drain()
    assert fake.sent == [] and status(c, occ, uid) == "maintenance"


def test_weekly_window_crossing_midnight(tmp_path):
    # Tuesday 17 Nov 2026; window Tuesdays 23:00-02:00
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    w = n.add_window({"kind": "weekly", "days": [1], "from": "23:00", "to": "02:00", "title": "Backups"})
    assert n.window_active(w, at(2026, 11, 17, 23, 30))
    assert n.window_active(w, at(2026, 11, 18, 1, 59))          # Wednesday morning belongs to Tuesday's window
    assert not n.window_active(w, at(2026, 11, 18, 2, 0))
    assert not n.window_active(w, at(2026, 11, 18, 23, 30))
    assert not n.window_active(w, at(2026, 11, 17, 22, 59))


def test_window_validation(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    for bad in ({"kind": "once", "start": clock.t, "end": clock.t - 1}, {"kind": "once", "start": clock.t, "end": clock.t + 20 * 86400},
                {"kind": "weekly", "days": [], "from": "01:00", "to": "02:00"}, {"kind": "weekly", "days": [9], "from": "01:00", "to": "02:00"},
                {"kind": "weekly", "days": [1], "from": "01:00", "to": "01:00"}, {"kind": "sometimes"}):
        with pytest.raises(ValueError):
            n.add_window(bad)
    w = n.add_window({"kind": "once", "start": clock.t, "end": clock.t + 60})
    assert n.remove_window(w["id"]) and not n.remove_window(w["id"])


# ---------------------------------------------------------------- daily limit
def test_daily_limit_with_one_note(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    uid = add_user(c, "ann")
    n.update_settings({"daily_cap": 5})
    rule(n, uid, "dev_new", "immediate")
    occs = [n.emit("dev_new", "new:d%d" % i, {"title": "D%d" % i}) for i in range(8)]
    n.drain()
    events = [m[2]["event"] if "event" in m[2] else m[2]["tag"] for m in fake.sent]
    assert events.count("dev_new") == 5 and events.count("cap") == 1
    assert [status(c, o, uid) for o in occs[5:]] == ["capped"] * 3
    # a new day, a new limit
    clock.t = at(2026, 11, 18, 9)
    n.emit("dev_new", "new:x", {"title": "X"})
    n.drain()
    assert fake.sent[-1][2]["event"] == "dev_new"


# ---------------------------------------------------------------- people
def test_rules_are_per_person_and_shared_channel_sends_once(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    a, b, z = add_user(c, "ann"), add_user(c, "bob"), add_user(c, "cid")
    rule(n, a, "dev_new", "immediate")
    rule(n, b, "dev_new", "immediate")
    occ = n.emit("dev_new", "new:d1", {"title": "Laptop"})
    n.drain()
    assert len([1 for m in fake.sent if m[0] == "webhook"]) == 1      # one shared webhook, one copy
    assert status(c, occ, a) == "sent" and status(c, occ, b) == "sent" and status(c, occ, z) == "nightly"


def test_disabled_account_gets_nothing(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    a = add_user(c, "ann")
    rule(n, a, "dev_new", "immediate")
    c.store.execute("UPDATE users SET disabled=1 WHERE id=?", (a,))
    n.emit("dev_new", "new:d1", {"title": "Laptop"})
    n.drain()
    assert fake.sent == []


def test_security_notice_only_to_the_person(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    a, b = add_user(c, "ann"), add_user(c, "bob", "admin")
    rule(n, a, "security_notice", "immediate")
    rule(n, b, "security_notice", "immediate")
    n.on_notice(a, "new_address", {"ip": "192.168.50.20", "agent": "Browser", "secret": "never"})
    n.drain()
    assert [m[1] for m in fake.sent] == [a]
    assert "192.168.50.20" in fake.sent[0][2]["text"] and "never" not in json.dumps(fake.sent[0][2])
    hist_b = n.history({"id": b, "role": "admin"})
    hist_a = n.history({"id": a, "role": "viewer"})
    assert hist_a and hist_b                                           # admins see the occurrence, never the address
    assert "ip" not in json.dumps(hist_a[0]["vars"])


# ---------------------------------------------------------------- journal bridge
def test_journal_rows_become_events(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    uid = add_user(c, "ann")
    n.update_settings({"protected_cidrs": ["192.168.50.0/28"]})
    rule(n, uid, "dev_new_protected", "immediate")
    rule(n, uid, "integ_auth", "immediate")
    c.reports.journal.listeners.append(n.on_journal)
    c.reports.journal.add("dev_new", {"id": "d9", "title": "Stranger", "ip": "192.168.50.9"}, clock.t)
    c.reports.journal.add("dev_new", {"id": "d99", "title": "Guest", "ip": "192.168.50.99"}, clock.t)
    c.reports.journal.add("src_fail", {"id": "int:1", "name": "Controller", "key": "integ.err.auth_failed"}, clock.t)
    clock.t += 200
    n.tick()
    n.drain()
    events = [m[2]["event"] for m in fake.sent]
    assert events == ["dev_new_protected", "integ_auth"]
    rows = c.store.query("SELECT event FROM notify_log ORDER BY id")
    assert [r["event"] for r in rows] == ["dev_new_protected", "dev_new", "integ_auth"]


def test_first_scan_is_no_news(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    c.store.set_setting("setup_done", False)
    n.on_journal("dev_new", {"id": "d1", "title": "x", "ip": "192.168.50.2"}, clock.t)
    assert c.store.one("SELECT COUNT(*) AS n FROM notify_log")["n"] == 0


def test_once_events_and_certs(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    uid = add_user(c, "ann")
    rule(n, uid, "cert_expiring", "immediate")
    n.on_certs([{"id": "int:1", "name": "Controller", "days": 3}, {"id": "int:2", "name": "Hub", "days": 90},
                {"id": "int:3", "name": "Broker", "days": -1}])
    n.on_certs([{"id": "int:1", "name": "Controller", "days": 3}])        # same day: once
    n.drain()
    assert len(fake.sent) == 2
    sev = {m[2]["text"]: m[2]["severity"] for m in fake.sent}
    assert sorted(sev.values()) == ["critical", "warning"]


def test_summary_for_the_nightly_report(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    uid = add_user(c, "ann")
    rule(n, uid, "dev_new", "immediate")
    n.emit("dev_new", "new:d1", {"title": "A"})
    n.add_window({"kind": "once", "title": "Rack work", "start": clock.t, "end": clock.t + 600})
    n.emit("dev_offline", "off:x", {"title": "X"}, device="x")
    n.drain()
    s = n.summary(clock.t - 10, clock.t + 10)
    assert s["count"]["sent"] == 1 and s["count"]["maintenance"] == 1 and s["maint_names"] == ["Rack work"]


# ---------------------------------------------------------------- web push
def test_rfc8291_test_vector():
    from cryptography.hazmat.primitives.asymmetric import ec
    d = int.from_bytes(webpush.unb64u("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big")  # secret-scan: fake (public RFC 8291 test vector)
    key = ec.derive_private_key(d, ec.SECP256R1())
    body = webpush.encrypt(b"When I grow up, I want to be a watermelon",
                           webpush.unb64u("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"),  # secret-scan: fake (public RFC 8291 test vector)
                           webpush.unb64u("BTBZMqHH6r4Tts7J_aSIgg"), salt=webpush.unb64u("DGv6ra1nlYgDCS1FRnbzlw"), as_private=key)
    assert webpush.b64u(body) == ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6"  # secret-scan: fake (public RFC 8291 test vector)
                                  "PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")  # secret-scan: fake (public RFC 8291 test vector)


def _browser_keys():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    k = ec.generate_private_key(ec.SECP256R1())
    pub = k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return k, webpush.b64u(pub), webpush.b64u(b"a" * 16)


def test_vapid_token_verifies():
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    pem, pub = webpush.new_vapid()
    header = webpush.vapid_header("https://push.example/send/abc", pem, now=1_800_000_000)
    token = header.split("t=")[1].split(",")[0]
    assert header.endswith("k=" + pub)
    head, body, sig = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=="))
    assert claims["aud"] == "https://push.example" and claims["exp"] == 1_800_000_000 + 12 * 3600
    raw = webpush.unb64u(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), webpush.unb64u(pub))
    key.verify(der, ("%s.%s" % (head, body)).encode(), ec.ECDSA(hashes.SHA256()))


def test_subscription_checks():
    _, p256, auth = _browser_keys()
    ok = webpush.clean_subscription({"endpoint": "https://push.example/send/abc", "keys": {"p256dh": p256, "auth": auth}})
    assert ok["endpoint"].startswith("https://push.example/")
    for endpoint in ("http://push.example/x", "https://127.0.0.1/x", "https://localhost/x", "https://printer.local/x",
                     "https://push/x", "https://[::1]/x"):
        with pytest.raises(ValueError):
            webpush.clean_subscription({"endpoint": endpoint, "keys": {"p256dh": p256, "auth": auth}})
    with pytest.raises(ValueError):
        webpush.clean_subscription({"endpoint": "https://push.example/x", "keys": {"p256dh": "AAAA", "auth": auth}})


def test_push_send_encrypts_for_the_browser_and_forgets_gone_ones(tmp_path):
    import urllib.error
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    uid = add_user(c, "ann")
    n.sender = None
    assert n.vapid_public(create=True)
    bk, p256, auth = _browser_keys()
    sub = n.add_push(uid, {"endpoint": "https://push.example/send/abc", "keys": {"p256dh": p256, "auth": auth}}, "Phone")
    n.add_push(uid, {"endpoint": "https://push.example/send/abc", "keys": {"p256dh": p256, "auth": auth}}, "Phone")
    assert len(n.push_subs(uid)) == 1                                  # the same browser twice: replaced, not doubled
    seen = []

    def opener(req):
        seen.append(req)

    raw = json.loads(c.vault.get("push:%d" % n.push_subs(uid)[0]["id"]))
    ok, key = webpush.send(raw, {"title": "T", "body": "B"}, c.vault.get("notify:vapid"), opener=opener)
    assert ok and seen[0].headers["Content-encoding"] == "aes128gcm"
    assert seen[0].headers["Authorization"].startswith("vapid t=")

    def gone(req):
        raise urllib.error.HTTPError(req.full_url, 410, "Gone", {}, None)

    ok, key = webpush.send(raw, {"title": "T"}, c.vault.get("notify:vapid"), opener=gone)
    assert not ok and key == "notify.push.gone"
    assert sub["id"] and n.remove_push(n.push_subs(uid)[0]["id"], user_id=uid + 1) is False


def test_push_payload_decrypts(tmp_path):
    """Decrypt what we send the way a browser does (RFC 8291 receiver side)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    bk, p256, auth = _browser_keys()
    body = webpush.encrypt(b'{"title":"Hello"}', webpush.unb64u(p256), webpush.unb64u(auth))
    salt, rs, idlen = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    as_pub = body[21:21 + idlen]
    shared = bk.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    ua_pub = bk.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    h = webpush._hmac
    ikm = h(h(webpush.unb64u(auth), shared), b"WebPush: info\x00" + ua_pub + as_pub + b"\x01")
    prk = h(salt, ikm)
    plain = AESGCM(h(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]).decrypt(h(prk, b"Content-Encoding: nonce\x00\x01")[:12],
                                                                               body[21 + idlen:], None)
    assert rs == 4096 and plain == b'{"title":"Hello"}\x02'


# ---------------------------------------------------------------- heartbeat
def test_heartbeat_needs_an_address_and_records_results(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    with pytest.raises(ValueError):
        n.update_heartbeat({"enabled": True})
    with pytest.raises(ValueError):
        n.update_heartbeat({"url": "ftp://x"})
    hb = n.update_heartbeat({"url": "https://status.example/ping/abc", "every_min": 5, "enabled": True})
    assert hb["url_set"] and "status.example" not in json.dumps(hb)
    calls = []
    n.heartbeat_open = lambda url: calls.append(url)
    assert n.beat_due(clock.t) and not n.beat_due(clock.t + 60) and n.beat_due(clock.t + 300)
    assert n.beat()["ok"] and calls == ["https://status.example/ping/abc"]

    def down(url):
        raise OSError("no route")

    n.heartbeat_open = down
    assert n.beat()["key"] == "report.send.unreachable"
    assert n.heartbeat()["fails"] == 1 and n.heartbeat()["last_ok"] == clock.t


def test_demo_never_sends(tmp_path):
    c, n, clock, fake = make(tmp_path, at(2026, 11, 17, 12))
    c.demo = object()
    n.sender = None
    uid = add_user(c, "ann")
    assert n.deliver_one("webhook", {"id": uid}, {"title": "x", "text": "y"}) == (True, "report.send.ok")
    assert not n.beat_due(clock.t)
