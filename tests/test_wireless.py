"""Wireless networks (SSIDs) and blocked clients in the firewall editor (stage B, U6): the pure rules of
``firewall.wireless`` on the sample network, the gate, the simulation through the API (draft, preview, confirmation, plan,
approval, undo), roles and the nightly section "Wireless networks" with a fixed clock in every language of the app.
Nothing here touches a network."""
import asyncio
import copy
import time

import pytest

from lanloom.firewall import adapter, canon, model, wireless
from lanloom.firewall import report as _fw_report  # noqa: F401 - registers the nightly sections

from tests.test_portconf import HDR, PW, Clock, at, c, make, sample, section, texts, wait_done  # noqa: F401 - fixture c

GUARD = {"addresses": sorted(adapter.SAMPLE_CRITICAL)}


def ssid(s, sid):
    return next(w for w in s["ssids"] if w["id"] == sid)


def pv_of(s, ops, **kw):
    return wireless.preview(s, wireless.clean_ops(s, ops), **kw)


def upd(sid, **after):
    return {"kind": "ssid.update", "object": sid, "after": after}


# ---------------------------------------------------------------------------- the sample
def test_sample_ssids_clients_and_info():
    s = sample()
    assert [w["name"] for w in s["ssids"]] == ["Home", "Home Media", "Office", "Things", "Cameras", "Guest"]
    assert [d["name"] for d in wireless.clients_on(s, ssid(s, "s_home"))] == ["Phone 1", "Kitchen tablet"]
    assert wireless.is_guest_network(s, "n_guest") and not wireless.is_guest_network(s, "n_main")
    info = wireless.info(s, GUARD)
    assert info["ssids"]["s_home"] == {"n": 2, "locked": None, "editable": True}
    assert info["clients"]["d06"] == "ws.block.protected" and "d10" not in info["clients"]
    assert next(d for d in s["devices"] if d["id"] == "d27")["blocked"] is True       # the visitor's phone
    assert {n["id"] for n in info["networks"]} >= {"n_main", "n_guest", "n_mgmt"}


def test_clean_ops_keeps_real_changes_with_their_before():
    s = sample()
    ops = wireless.clean_ops(s, [upd("s_home", name="Home", hidden=True), upd("s_guest", bands=["5ghz", "2.4ghz"]),
                             {"kind": "client.block", "object": "d10", "after": {"blocked": True}},
                             {"kind": "client.block", "object": "d27", "after": {"blocked": True}},      # already blocked
                             {"kind": "ssid.create", "object": "new:0000abcd", "after": {"name": "Lab", "network": "n_work"}},
                             {"kind": "ssid.create", "object": "bad", "after": {"name": "x", "network": "n_work"}},
                             upd("nope", name="x"), {"kind": "ssid.update", "object": "s_home", "after": {"channel": 6}}])
    assert sorted(ops) == ["client.block|d10", "ssid.create|new:0000abcd", "ssid.update|s_home"]
    assert ops["ssid.update|s_home"] == {"kind": "ssid.update", "object": "s_home", "before": {"hidden": False}, "after": {"hidden": True}}
    assert ops["ssid.create|new:0000abcd"]["after"] == {"name": "Lab", "network": "n_work", "bands": ["2.4ghz", "5ghz"],
                                                       "security": "wpa2_wpa3_personal", "hidden": False, "enabled": True}


# ---------------------------------------------------------------------------- the rules
@pytest.mark.parametrize("op,why", [
    (upd("s_things", enabled=False), "ws.block.protected"),           # the hub and the broker are wired, but the thermostat ...
    (upd("s_home", security="open"), "ws.block.open_not_guest"),
    (upd("s_guest", security="open"), None),
    (upd("s_home", name="guest"), "ws.block.name_taken"),
    (upd("s_home", bands=["6ghz"]), "ws.block.6ghz_wpa3"),
    (upd("s_work", bands=["5ghz", "6ghz"]), None),                    # the office SSID is WPA3 already
    ({"kind": "ssid.delete", "object": "s_home", "after": {}}, "fw.block.not_own"),
    ({"kind": "ssid.create", "object": "new:0000000a", "after": {"name": "Open", "network": "n_main", "security": "open"}},
     "ws.block.open_not_guest"),
    ({"kind": "ssid.create", "object": "new:0000000a", "after": {"name": "Admin", "network": "n_mgmt", "security": "open"}},
     "ws.block.open_management"),
    ({"kind": "client.block", "object": "d06", "after": {"blocked": True}}, "fw.block.protected"),
    ({"kind": "client.block", "object": "d10", "after": {"blocked": True}}, None),
    ({"kind": "client.block", "object": "d27", "after": {"blocked": False}}, None),
])
def test_rules(op, why):
    s = sample()
    if op["object"] == "s_things":                                       # a protected device on the smart-home SSID
        for d in s["devices"]:
            if d["id"] == "d20":
                d["protected"] = "fw.reason.hub"
    pv = pv_of(s, [op], guard=GUARD)
    assert (pv["blocks"][0][0] if pv["blocks"] else None) == why, pv


def test_the_approver_and_lanloom_keep_their_ssid_and_are_never_blocked():
    s = sample()
    mine = {"devices": ["d10"], "addresses": []}
    assert pv_of(s, [upd("s_home", enabled=False)], guard=mine)["blocks"][0][0] == "ws.block.self_path"
    assert pv_of(s, [upd("s_home", hidden=True)], guard=mine)["blocks"] == []          # hiding drops nobody
    assert pv_of(s, [{"kind": "client.block", "object": "d10", "after": {"blocked": True}}], guard=mine)["blocks"][0][0] == "ws.block.self_block"
    by_addr = {"devices": [], "addresses": ["192.168.50.23"]}                         # Lanloom runs on the kitchen tablet
    assert pv_of(s, [upd("s_home", name="Home 2")], guard=by_addr)["blocks"][0][0] == "ws.block.self_path"
    assert pv_of(s, [{"kind": "client.block", "object": "d11", "after": {"blocked": True}}], guard=by_addr)["blocks"][0][0] == "ws.block.self_path"


def test_confirmations_and_warnings():
    s = sample()
    pv = pv_of(s, [upd("s_guest", security="open"), {"kind": "client.block", "object": "d10", "after": {"blocked": True}},
                   {"kind": "ssid.create", "object": "new:0000000c", "after": {"name": "Admin", "network": "n_mgmt"}}], guard=GUARD,
               deps={"d10": [{"by": "Automation hub", "via": "tracker"}]})
    assert sorted(pv["confirm"]) == ["confirm:client.block:d10", "confirm:ssid.create:new:0000000c", "confirm:ssid.update:s_guest"]
    keys = [w[0] for w in pv["warnings"]]
    assert {"ws.warn.open", "ws.warn.block", "ws.warn.to_management", "ws.warn.create", "ws.warn.password_new"} <= set(keys)
    assert any(w[1].get("by") == "Automation hub" and w[1].get("name") == "Phone 1" for w in pv["warnings"])
    pv = pv_of(s, [upd("s_home", name="Home 2", bands=["5ghz"])], guard=GUARD)
    assert not pv["confirm"]
    rename = next(w for w in pv["warnings"] if w[0] == "ws.warn.rename")
    assert rename[1]["n"] == 2 and any(w[0] == "ws.warn.bands_drop" for w in pv["warnings"])


def test_gate_refuses_what_the_preview_refuses():
    s = sample()
    st = {"kind": "client.block", "object": "d10", "before": {"blocked": False}, "after": {"blocked": True}}
    pv = {"base": canon.fingerprint(s), "steps": [st], "warnings": [], "costs": {"objects": 1}}
    plan = model.build_plan(s, pv, target="simulation", user="admin", confirmed=[])
    caps = {"family": "simulation", "api": {"kind": "simulation"}, "write": {"client.block": "B*"}}
    with pytest.raises(model.Refused) as e:
        model.gate(s, plan, target="simulation", now=time.time(), quiet=False, busy=False, healthy=(True, ""), caps=caps)
    assert e.value.key == "fw.block.confirm"
    plan = model.build_plan(s, pv, target="simulation", user="admin", confirmed=["confirm:client.block:d10"])
    plan["requester"]["device"] = "d10"
    with pytest.raises(model.Refused) as e:
        model.gate(s, plan, target="simulation", now=time.time(), quiet=False, busy=False, healthy=(True, ""), caps=caps)
    assert e.value.key == "ws.block.self_block"


def test_one_change_per_object():
    s = sample()
    a = {"kind": "ssid.update", "object": "s_home", "before": {"hidden": False}, "after": {"hidden": True}}
    assert wireless.plan_lockout(s, [a, dict(a)]) == "ws.block.one_per_object"
    assert wireless.plan_lockout(s, [a]) is None


def test_after_state_and_matches():
    s = sample()
    steps_ = [{"kind": "ssid.create", "object": "new:0000000d", "before": {}, "after": {"name": "Lab", "network": "n_work"}},
              {"kind": "client.block", "object": "d10", "before": {"blocked": False}, "after": {"blocked": True}},
              {"kind": "ssid.delete", "object": "s_cams", "before": {"name": "Cameras"}, "after": {}}]
    new = wireless.after_state(s, steps_)
    assert "Lab" in [w["name"] for w in new["ssids"]] and "Cameras" not in [w["name"] for w in new["ssids"]]
    assert next(d for d in new["devices"] if d["id"] == "d10")["blocked"] is True
    assert wireless.matches(ssid(s, "s_home"), {"bands": ["5ghz", "2.4ghz"]}) and not wireless.matches(ssid(s, "s_home"), {"hidden": True})


# ---------------------------------------------------------------------------- through the API
def test_editor_offers_wireless_with_locks(c):
    v = c.get("/api/firewall?source=editor").json()
    assert {"ssid.create", "ssid.update", "client.block"} <= set(v["capabilities"]["write"])
    wf = v["wireless"]
    assert wf["ssid_write"] and wf["block_write"] and wf["ssids"]["s_home"]["n"] == 2
    assert wf["clients"]["d06"] == "ws.block.protected"


def test_ssid_change_and_block_through_the_api_then_undo(c):
    ops = [upd("s_guest", name="Visitors", hidden=True), {"kind": "client.block", "object": "d10", "after": {"blocked": True}},
           {"kind": "ssid.create", "object": "new:00001234", "after": {"name": "Lab", "network": "n_work", "security": "wpa3_personal"}}]
    d = c.put("/api/firewall/draft", json={"ops": ops}, headers=HDR).json()
    assert sorted(d["ops"]) == ["client.block|d10", "ssid.create|new:00001234", "ssid.update|s_guest"]
    pv = c.post("/api/firewall/preview", json={}, headers=HDR).json()
    assert not pv["blocks"], pv["blocks"]
    assert pv["confirm"] == ["confirm:client.block:d10"]
    r = c.post("/api/firewall/plan", json={"confirmed": []}, headers=HDR)
    assert r.status_code == 409 and r.json()["error"] == "fw.block.confirm"
    p = c.post("/api/firewall/plan", json={"confirmed": pv["confirm"]}, headers=HDR).json()
    assert c.post("/api/firewall/approve", json={"plan": p["plan"]["id"]}, headers=HDR).json()["ok"]
    v = wait_done(c)
    job = v["history"][0]
    assert job["status"] == "done", job
    kinds = sorted(x["kind"] for x in job["summary"])
    assert kinds == ["client.block", "ssid.create", "ssid.update"]
    assert next(x for x in job["summary"] if x["kind"] == "client.block") == {"name": "Phone 1", "kind": "client.block", "fields": ["blocked"], "on": True}
    names = [w["name"] for w in v["state"]["ssids"]]
    assert "Visitors" in names and "Lab" in names and "Guest" not in names
    assert next(x for x in v["state"]["devices"] if x["id"] == "d10")["blocked"] is True
    d = c.post("/api/firewall/undo", json={"plan": job["plan"]}, headers=HDR).json()
    assert d["ops"]["client.block|d10"]["after"] == {"blocked": False}
    assert d["ops"]["ssid.update|s_guest"]["after"] == {"name": "Guest", "hidden": False}
    assert any(k.startswith("ssid.delete|") for k in d["ops"])
    entries = c.get("/api/audit?limit=30").json()["entries"]
    assert any(e["action"].startswith("firewall.") for e in entries)


def test_a_protected_device_is_refused_and_viewers_cannot_block(c):
    c.put("/api/firewall/draft", json={"ops": [{"kind": "client.block", "object": "d06", "after": {"blocked": True}}]}, headers=HDR)
    pv = c.post("/api/firewall/preview", json={}, headers=HDR).json()
    assert pv["blocks"][0][0] == "fw.block.protected" and pv["blocks"][0][1]["name"] == "Home server"
    c.ll.accounts.create("vic", PW, role="viewer")
    c.cookies.clear()
    assert c.post("/api/login", json={"username": "vic", "password": PW}, headers=HDR).status_code == 200
    r = c.put("/api/firewall/draft", json={"ops": [{"kind": "client.block", "object": "d10", "after": {"blocked": True}}]}, headers=HDR)
    assert r.status_code == 403


# ---------------------------------------------------------------------------- nightly section "Wireless networks"
def test_nightly_wireless_baseline_changes_and_calm(tmp_path):
    cur = {"st": copy.deepcopy(sample())}
    clock = Clock(at(2026, 11, 19, 6, 30))
    core, rep = make(tmp_path, clock, lambda: [cur["st"]])
    r1 = asyncio.run(rep.run_due(clock.t))
    s1 = section(r1, "wireless")
    assert s1["summary"]["key"] == "report.ws.baseline" and s1["summary"]["vars"] == {"n": 6, "blocked": 1} and not s1["items"]
    t1 = texts(r1, s1)
    assert "6 SSIDs" in t1["en"][0] and "1 gesperrtes Gerät" in t1["de"][0]
    st = copy.deepcopy(cur["st"])
    st = wireless.after_state(st, [{"kind": "ssid.update", "object": "s_guest", "after": {"security": "open"}},
                               {"kind": "ssid.update", "object": "s_cams", "after": {"enabled": False}},
                               {"kind": "client.block", "object": "d10", "after": {"blocked": True}},
                               {"kind": "client.block", "object": "d27", "after": {"blocked": False}}])
    st["ssids"].append({"id": "s_new", "name": "Lab", "network": "n_work", "bands": ["5ghz"], "security": "wpa3_personal",
                        "hidden": False, "enabled": True})
    cur["st"] = st
    clock.t = at(2026, 11, 20, 6, 30)
    r2 = asyncio.run(rep.run_due(clock.t))
    s2 = section(r2, "wireless")
    keys = sorted(i["key"] for i in s2["items"])
    assert keys == ["report.ws.blocked", "report.ws.new", "report.ws.off", "report.ws.opened", "report.ws.unblocked"]
    assert s2["level"] == "danger"
    t2 = texts(r2, s2)
    assert any("Guest has no password any more" in x for x in t2["en"])
    assert any("Phone 1 ist gesperrt" in x for x in t2["de"])
    clock.t = at(2026, 11, 21, 6, 30)
    r3 = asyncio.run(rep.run_due(clock.t))
    s3 = section(r3, "wireless")
    assert s3["summary"]["key"] == "report.ws.calm" and not s3["items"]


def test_nightly_section_only_with_wireless_networks(tmp_path):
    clock = Clock(at(2026, 11, 19, 6, 30))
    st = sample()
    st.pop("ssids")
    core, rep = make(tmp_path, clock, lambda: [st])
    assert section(asyncio.run(rep.run_due(clock.t)), "wireless") is None
