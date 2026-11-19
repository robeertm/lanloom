"""The read half against a local fake of the box's TR-064 interface: what is sent, what is refused, digest sign-in and
its pauses, error reasons, fallbacks, the poll cadence."""
import asyncio
import re

import pytest

from lanloom.integrations import capabilities
from lanloom.integrations.fritzbox import read
from lanloom.integrations.transport import Forbidden, Transport
from lanloom.sources._common import SourceError

from . import fixtures as fx
from .fake_box import FakeBox


@pytest.fixture(autouse=True)
def fresh_ledger():
    read._reset_for_tests()
    yield
    read._reset_for_tests()


@pytest.fixture()
def box():
    srv = FakeBox()
    yield srv
    srv.stop()


def reader(srv, **kw):
    args = {"url": srv.url, "username": fx.USER, "password": fx.PASSWORD}
    args.update(kw)
    return read.FritzboxReader(**args)


def calls(srv):
    return [(r["method"], r["path"], r["action"]) for r in srv.requests if r["method"] == "POST" and r["auth"] == "Digest"]


# ---------------------------------------------------------------------------- constructor
@pytest.mark.parametrize("kw", [{"url": ""}, {"url": None}, {"url": "ftp://x"}, {"url": "https://user:pw@x"}, {"url": "http://a b"},
                                {"url": "http://x:0"}, {"url": "http://x:99999"}, {"password": ""}, {"password": None},
                                {"username": ""}, {"username": 'a"b'}, {"username": "a\nb"}])
def test_bad_settings_are_refused(kw):
    args = {"url": "http://192.0.2.1", "username": fx.USER, "password": fx.PASSWORD}
    args.update(kw)
    with pytest.raises(SourceError) as e:
        read.FritzboxReader(**args)
    assert e.value.key == "source.family.config"


@pytest.mark.parametrize("url,base", [("http://192.0.2.1", "http://192.0.2.1:49000"), ("https://192.0.2.1/", "https://192.0.2.1:49443"),
                                      ("http://fritz.box", "http://fritz.box:49000"), ("http://192.0.2.1:8080", "http://192.0.2.1:8080"),
                                      ("http://[2001:db8::1]", "http://[2001:db8::1]:49000")])
def test_the_port_defaults_to_the_one_of_the_scheme(url, base):
    assert read.FritzboxReader(url=url, username="u", password="p")._base == base


def test_repr_str_and_state_hide_the_password(box):
    r = reader(box)
    r.collect()
    for text in (repr(r), str(r), repr(r._t), repr(read._LEDGER)):
        assert fx.PASSWORD not in text


# ---------------------------------------------------------------------------- the allow-list
def test_read_allow_list_holds_soap_rows_and_two_documents_only():
    assert read.ALLOW.write is False
    posts = [r for r in read.ALLOW.rules if r.method == "POST"]
    assert posts and all(r.soap is not None for r in posts)
    rows = {(r.path.pattern, k) for r in posts for k in r.soap}
    assert len(rows) == 7
    assert all(re.fullmatch(r"(?:X_AVM-DE_)?Get\w*", action) for _, (_, action) in rows)
    assert all(not args for r in posts for args in r.soap.values()), "no call carries an argument"
    assert [r.method for r in read.ALLOW.rules if r.method != "POST"] == ["GET"]


def body(service, action, **args):
    return {"service": service, "action": action, "args": args}


HOSTS = read.HOSTS
WANIP = read.WANIP


@pytest.mark.parametrize("method,path,payload", [
    # actions that change something, on the right path, with the right service
    ("POST", HOSTS[0], body(HOSTS[1], "SetHostName")),
    ("POST", HOSTS[0], body(HOSTS[1], "X_AVM-DE_SetHostNameByMACAddress")),
    ("POST", HOSTS[0], body(HOSTS[1], "X_AVM-DE_WakeOnLANByMACAddress")),
    ("POST", WANIP[0], body(WANIP[1], "ForceTermination")),
    ("POST", WANIP[0], body(WANIP[1], "RequestConnection")),
    ("POST", "/upnp/control/deviceconfig", body("urn:dslforum-org:service:DeviceConfig:1", "Reboot")),
    ("POST", "/upnp/control/deviceconfig", body("urn:dslforum-org:service:DeviceConfig:1", "GetInfo")),
    ("POST", "/upnp/control/wlanconfig1", body("urn:dslforum-org:service:WLANConfiguration:1", "SetEnable")),
    ("POST", "/upnp/control/x_contact", body("urn:dslforum-org:service:X_AVM-DE_OnTel:1", "GetCallList")),
    ("POST", "/upnp/control/x_auth", body("urn:dslforum-org:service:X_AVM-DE_Auth:1", "GetInfo")),
    ("POST", "/upnp/control/x_myfritz", body("urn:dslforum-org:service:X_AVM-DE_MyFritz:1", "GetInfo")),
    # a read action, but with another service type, with an argument, or without the three fields
    ("POST", HOSTS[0], body(WANIP[1], "X_AVM-DE_GetHostListPath")),
    ("POST", HOSTS[0], body(HOSTS[1], "X_AVM-DE_GetHostListPath", NewIndex="1")),
    ("POST", HOSTS[0], body(HOSTS[1], "GetGenericHostEntry", NewIndex="1")),
    ("POST", HOSTS[0], {"service": HOSTS[1], "action": "X_AVM-DE_GetHostListPath"}),
    ("POST", HOSTS[0], {"service": HOSTS[1], "action": "X_AVM-DE_GetHostListPath", "args": {}, "extra": 1}),
    ("POST", HOSTS[0], {"service": 1, "action": 2, "args": {}}),
    ("POST", HOSTS[0], {"service": HOSTS[1], "action": "X_AVM-DE_GetHostListPath", "args": []}),
    ("POST", HOSTS[0], None), ("POST", HOSTS[0], "<xml/>"), ("POST", HOSTS[0], b"<xml/>"),
    ("PUT", HOSTS[0], body(HOSTS[1], "X_AVM-DE_GetHostListPath")), ("DELETE", HOSTS[0], None), ("PATCH", HOSTS[0], {}),
    # paths
    ("POST", "/upnp/control/hosts/../deviceconfig", body(HOSTS[1], "X_AVM-DE_GetHostListPath")),
    ("POST", "/upnp/control/hosts?x=1", body(HOSTS[1], "X_AVM-DE_GetHostListPath")),
    ("GET", "/tr64desc.xml", None), ("GET", "/", None), ("GET", "/login_sid.lua", None),
    ("GET", "/hostlist.lua", None), ("GET", "/hostlist.lua?sid=0123", None), ("GET", "/hostlist.lua?sid=0123456789abcdef&x=1", None),
    ("GET", "/hostlist.lua?sid=0123456789ABCDEF", None), ("GET", "/calllist.lua?sid=0123456789abcdef", None),
    ("GET", "/../hostlist.lua?sid=0123456789abcdef", None), ("GET", "/a/b/hostlist.lua?sid=0123456789abcdef", None),
    ("GET", "/hostlist.lua?sid=0123456789abcdef", {"a": 1}),
    ("GET", "/data.lua?sid=0123456789abcdef", None),
])
def test_everything_but_the_table_is_refused_before_a_byte_is_sent(box, method, path, payload):
    t = Transport(box.url, read.ALLOW, digest=lambda: (fx.USER, fx.PASSWORD))
    with pytest.raises(Forbidden):
        t.request(method, path, payload)
    assert box.requests == [] and t.refused == 1 and t.sent == 0


def test_the_allowed_rows_are_sent_as_checked_envelopes(box):
    t = Transport(box.url, read.ALLOW, digest=lambda: (fx.USER, fx.PASSWORD))
    status, _, data = t.request("POST", HOSTS[0], body(HOSTS[1], "X_AVM-DE_GetHostListPath"))
    assert status == 200 and b"hostlist.lua?sid=" in data
    rec = [r for r in box.requests if r["auth"] == "Digest"][0]
    assert rec["headers"]["SOAPACTION"] == '"%s#X_AVM-DE_GetHostListPath"' % HOSTS[1]
    assert rec["headers"]["Content-Type"].startswith("text/xml")
    assert rec["body"].count(b"<u:X_AVM-DE_GetHostListPath ") == 1 and b"<s:Body>" in rec["body"]


def test_a_forbidden_request_in_the_reader_is_a_reason_key(box):
    r = reader(box)
    with pytest.raises(SourceError) as e:
        r._send("POST", "/upnp/control/deviceconfig", body("urn:dslforum-org:service:DeviceConfig:1", "Reboot"))
    assert e.value.key == "source.family.refused" and box.requests == []


def test_a_list_path_that_does_not_match_is_never_fetched(box):
    r = reader(box)
    for path in (None, "", "http://198.51.100.1/hostlist.lua?sid=0123456789abcdef", "/data.lua?sid=0123456789abcdef", "/hostlist.lua?sid=x"):
        assert r._document(path, "text/xml") is None
    assert box.requests == []


# ---------------------------------------------------------------------------- one full read
def test_collect_reads_hosts_mesh_and_the_box_with_get_calls_only(box):
    raw = reader(box).collect()
    assert [h["name"] for h in raw["hosts"]][:2] == ["Repeater upstairs", "NAS"]
    assert raw["mesh"]["nodes"][0]["uid"] == "n-box"
    assert raw["info"] == {"model": "FRITZ!Box 7590", "firmware": "7.57", "uptime_s": 864000}
    assert raw["lan"]["ip"] == fx.BOX_IP and raw["lan"]["dhcp"] is True and raw["lan_mac"] == fx.BOX
    assert [(w["kind"], w["status"]) for w in raw["wans"]] == [("ip", "Connected"), ("ppp", "Unconfigured")]
    assert {(m, p) for m, p, _ in calls(box)} == {("POST", p) for p in (HOSTS[0], read.DEVICEINFO[0], read.LAN[0], read.LANIF[0], read.WANIP[0], read.WANPPP[0])}
    assert all(a.startswith(("Get", "X_AVM-DE_Get")) for _, _, a in calls(box))
    assert {r["method"] for r in box.requests} == {"GET", "POST"}
    assert "SERIAL-DO-NOT-KEEP" not in repr(raw) and "log line" not in repr(raw)


def test_the_digest_challenge_is_answered_once_and_reused(box):
    r = reader(box)
    r.collect()
    first = [x for x in box.requests if x["method"] == "POST"]
    assert first[0]["auth"] == "" and all(x["auth"] == "Digest" for x in first[1:]), "one challenge, then every call carries the answer"
    assert r._t.challenges == 1 and box.bad_attempts == 0 and box.good_calls == 7


def test_a_stale_nonce_is_answered_again_without_a_refusal(box):
    r = reader(box)
    r.collect(light=True)
    box.stale_next = True
    r.collect(light=True)
    assert r._t.challenges == 2 and box.bad_attempts == 0
    assert read._LEDGER[r._lkey][1] == 0.0, "no pause: the box only wanted a fresh nonce"


def test_the_light_poll_asks_only_hosts_and_mesh_after_the_first_full_one(box):
    r = reader(box)
    r.collect(light=True)                        # nothing static yet: reads everything once
    box.requests.clear()
    raw = r.collect(light=True)
    paths = {x["path"].split("?")[0] for x in box.requests}
    assert paths == {HOSTS[0], "/hostlist.lua", "/meshlist.lua"}
    assert raw["info"]["model"] == "FRITZ!Box 7590" and raw["lan_mac"] == fx.BOX, "the static part comes from the last full poll"
    box.requests.clear()
    r.collect(light=False)
    assert read.DEVICEINFO[0] in {x["path"] for x in box.requests}


# ---------------------------------------------------------------------------- sign-in problems
def test_a_wrong_password_is_one_refused_sign_in_then_a_pause(box):
    r = reader(box, password="wrong")
    with pytest.raises(SourceError) as e:
        r.collect()
    assert e.value.key == "source.family.auth_failed"
    sent = len(box.requests)
    assert box.bad_attempts == 1
    for _ in range(3):
        with pytest.raises(SourceError) as e:
            r.collect()
        assert e.value.key == "source.family.paused"
    assert len(box.requests) == sent, "paused: not one more request, so the box never starts delaying logins"
    assert box.bad_attempts == 1


def test_the_pause_is_shared_by_every_reader_of_the_same_account_and_ends(box, monkeypatch):
    with pytest.raises(SourceError):
        reader(box, password="wrong").collect()
    other = reader(box, password="wrong")
    with pytest.raises(SourceError) as e:
        other.collect()
    assert e.value.key == "source.family.paused"
    ok = reader(box)                              # another password = another account key: not paused
    assert ok.collect()["hosts"]
    t0 = __import__("time").time()
    monkeypatch.setattr(read.time, "time", lambda: t0 + read.PAUSE_AFTER_REFUSAL_S + 5)
    with pytest.raises(SourceError) as e:
        other.collect()
    assert e.value.key == "source.family.auth_failed"


def test_fresh_sign_ins_are_capped_per_hour(box, monkeypatch):
    monkeypatch.setattr(read, "LOGINS_PER_HOUR", 2)
    r = reader(box)
    r.collect(light=True)
    for _ in range(2):
        box.stale_next = True
        try:
            r.collect(light=True)
        except SourceError as exc:
            assert exc.key == "source.family.paused"
    with pytest.raises(SourceError) as e:
        r.collect(light=True)
    assert e.value.key == "source.family.paused"


# ---------------------------------------------------------------------------- reasons and fallbacks
def test_the_interface_switched_off_is_not_an_answer_the_reader_understands(box):
    box.mode = "tr064_off"
    with pytest.raises(SourceError) as e:
        reader(box).collect()
    assert e.value.key == "source.family.protocol"


def test_an_unreachable_box_is_a_reason_key():
    with pytest.raises(SourceError) as e:
        read.FritzboxReader(url="http://127.0.0.1:9", username="u", password="p", timeout=1).collect()
    assert e.value.key == "source.family.unreachable"


def test_a_box_that_answers_html_is_a_protocol_problem(box):
    box.mode = "http"
    with pytest.raises(SourceError) as e:
        reader(box).collect()
    assert e.value.key == "source.family.protocol"


def test_an_account_without_the_right_is_denied(box):
    box.faults[(HOSTS[0], "X_AVM-DE_GetHostListPath")] = 606
    with pytest.raises(SourceError) as e:
        reader(box).collect()
    assert e.value.key == "source.family.forbidden"
    box.faults.clear()
    box.faults[(read.DEVICEINFO[0], "GetInfo")] = 606
    with pytest.raises(SourceError) as e:
        reader(box).collect()
    assert e.value.key == "source.family.forbidden"


def test_a_release_without_the_mesh_list_still_shows_the_hosts(box):
    box.faults[(HOSTS[0], "X_AVM-DE_GetMeshListPath")] = 401
    r = reader(box)
    raw = r.collect()
    assert "mesh" not in raw and raw["hosts"] and "mesh" not in r.last_areas
    ev = asyncio.run(r.poll(light=False))
    assert [e["type"] for e in ev] == ["topology", "config"] and ev[1]["state"]["controller"]["mesh"] is False


def test_a_mesh_list_that_is_not_json_or_has_no_nodes_is_left_out(box):
    for doc in (b"<html/>", b"[]", b'{"nodes": 3}', b"{}"):
        box.mesh_json = doc
        assert "mesh" not in reader(box).collect()


def test_a_missing_wan_service_leaves_the_internet_state_out(box):
    box.answers.pop(("/upnp/control/wanipconnection1", "GetStatusInfo"))
    box.answers.pop(("/upnp/control/wanpppconn1", "GetStatusInfo"))
    r = reader(box)
    ev = asyncio.run(r.poll())
    cfg = ev[1]
    assert cfg["state"]["wans"] == [] and "wans" not in cfg["capabilities"]["read"]


def test_a_host_list_that_is_not_a_list_is_a_protocol_problem(box):
    box.hosts_xml = b"<html><body>nope</body></html>"
    with pytest.raises(SourceError) as e:
        reader(box).collect()
    assert e.value.key == "source.family.protocol"


def test_a_host_list_with_an_entity_bomb_is_refused(box):
    box.hosts_xml = b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa">]><List><Item><HostName>&a;</HostName></Item></List>'
    with pytest.raises(SourceError) as e:
        reader(box).collect()
    assert e.value.key == "source.family.protocol"


# ---------------------------------------------------------------------------- events, capabilities, contract
def test_poll_makes_a_topology_event_and_on_a_full_poll_a_config_event(box):
    r = reader(box)
    light = asyncio.run(r.poll(light=True))
    assert [e["type"] for e in light] == ["topology"]
    full = asyncio.run(r.poll(light=False))
    assert [e["type"] for e in full] == ["topology", "config"]
    cfg = full[1]
    assert cfg["family"] == "fritzbox" and cfg["source"] == "family" and cfg["capabilities"]["write"] == {}
    assert cfg["capabilities"]["api"]["version"] == "7.57"
    assert {"inventory", "clients", "topology", "networks", "wans", "stats"} == set(cfg["capabilities"]["read"])
    assert cfg["state"]["controller"]["class"] == "home_router"


def test_the_capabilities_only_ever_narrow():
    caps = capabilities.validate(read.CAPABILITIES, "fritzbox")
    assert caps["write"] == {} and caps["limits"]["logins_per_hour"] == 60


def test_the_connection_test_reports_a_summary(box):
    ok, key, details = asyncio.run(reader(box).test())
    assert ok and key == "source.family.ok"
    assert details == {"version": "7.57", "model": "FRITZ!Box 7590", "devices": 5, "equipment": 2, "mesh": True}
    assert "SERIAL" not in repr(details) and fx.PASSWORD not in repr(details)


def test_the_connection_test_names_a_refused_sign_in(box):
    ok, key, _ = asyncio.run(reader(box, password="wrong").test())
    assert not ok and key == "source.family.auth_failed"


def test_the_account_scope_is_unknown(box):
    assert asyncio.run(reader(box).account_scope()) == "unknown"
