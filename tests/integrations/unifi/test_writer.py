"""The write half of the UniFi family (stage B, clients) against the writable fake, alone and inside the executor
pipeline: allow-list below the adapter, checks the controller does not make, read-back, rollback after a partial
write, network loss mid-write, crash recovery, sessions and the sign-in cap, the connection guard."""
import json
import os
import socket
import threading
import time

import pytest

from lanloom import connguard, integrations
from lanloom.firewall import model as fwmodel
from lanloom.firewall.runner import Runner
from lanloom.integrations import transport
from lanloom.integrations.credentials import Secret
from lanloom.integrations.unifi import model, session
from lanloom.integrations.unifi import write as wmod
from lanloom.integrations.unifi.write import UnifiWriter, WriteError

from . import fixtures as fx
from .fake_writer import PW, U_GUEST, U_OFFICE, U_PHONE, U_PRINTER, USER, WritableFake

PHONE = model.oid("c", fx.MAC_PHONE)
OFFICE = model.oid("c", fx.MAC_OFFICE)
PRINTER = model.oid("c", "02:4c:4c:00:01:09")
LAN = model.oid("n", fx.O_NET_LAN)
GUEST_NET = model.oid("n", fx.O_NET_GUEST)
LAB = model.oid("n", fx.C_NET_LAB)
WAN = model.oid("n", fx.O_NET_WAN)


@pytest.fixture(params=["console", "classic"])
def fake(request):
    f = WritableFake(style=request.param)
    yield f
    f.stop()


def writer(fake, tmp_path=None, **kw):
    return UnifiWriter(url=fake.url, write_username=USER, write_password=Secret(PW), settle_s=0,
                       state_dir=str(tmp_path) if tmp_path else None, sleep=lambda s: None, **kw)


def runner(w, tmp_path):
    return Runner(w, tmp_path / "ex", target="controller", require_signature=False, watch_s=0, check_s=0, sleep=lambda s: None)


def plan_for(w, moves=None, edits=None):
    st = w.read()
    pv = fwmodel.preview(st, moves or {}, edits=edits or {})
    assert not pv["blocks"], pv["blocks"]
    return fwmodel.build_plan(st, pv, target="controller", user="admin", confirmed=pv["confirm"])


# ---------------------------------------------------------------------------- the allow-list below the adapter
def test_allow_list_holds_only_the_listed_fields():
    ok = wmod.ALLOW.check("PUT", "/proxy/network/api/s/default/rest/user/" + U_PHONE, {"name": "x"})
    assert ok.method == "PUT"
    for body in ({}, {"blocked": True}, {"name": "x", "usergroup_id": "a"}, {"fixed_ip": "not-an-ip"},
                 {"network_id": "../../x"}, {"name": "a\nb"}, {"use_fixedip": "yes"}, {"virtual_network_override_id": "x" * 24}):
        with pytest.raises(transport.Forbidden):
            wmod.ALLOW.check("PUT", "/api/s/default/rest/user/" + U_PHONE, body)
    for method, path, body in (("POST", "/api/s/default/cmd/stamgr", {"cmd": "block-sta"}), ("POST", "/api/s/default/cmd/devmgr", {}),
                               ("DELETE", "/api/s/default/rest/user/" + U_PHONE, None),
                               ("PUT", "/api/s/default/rest/networkconf/" + fx.C_NET_LAN, {"purpose": "wan"}),
                               ("PUT", "/api/s/default/rest/networkconf/" + fx.C_NET_LAN, {"vlan": 5}),
                               ("POST", "/api/s/default/rest/networkconf", {"name": "x", "purpose": "wan"}),
                               ("POST", "/api/s/default/cmd/stamgr", {"cmd": "kick-sta", "mac": fx.MAC_PHONE}),
                               ("POST", "/api/s/default/rest/portforward", {"name": "x"}),
                               ("PUT", "/api/s/default/rest/wlanconf/5f0000000000000000000601", {"channel": 6}),
                               ("PUT", "/api/s/default/rest/user/../setting", {"name": "x"}),
                               ("POST", "/api/login", {"username": "a"}),
                               ("GET", "/api/s/default/rest/setting", None),
                               ("GET", "/proxy/network/integration/v1/sites", None)):
        with pytest.raises(transport.Forbidden):
            wmod.ALLOW.check(method, path, body)


def test_a_refused_request_never_leaves_the_process(fake):
    w = writer(fake)
    w.read()
    before = len(fake.requests)
    with pytest.raises(WriteError) as exc:
        w._call("PUT", "api/s/{site}/rest/user/" + U_PHONE, {"blocked": True})
    assert exc.value.key == "unifi.refused_by_allow_list" and len(fake.requests) == before and w._t.refused == 1


def test_secrets_never_show(fake, caplog):
    w = writer(fake)
    w.read()
    for text in (repr(w), str(w), repr(w._session), repr(w._t), repr(w._ledger)):
        assert PW not in text and "sid" not in text
    assert PW not in caplog.text
    with pytest.raises(TypeError):
        json.dumps({"p": w._password})


def test_unknown_options_and_missing_credentials_are_refused(fake):
    with pytest.raises(ValueError, match="unknown option"):
        UnifiWriter(url=fake.url, write_username=USER, write_password=PW, pasword="typo")
    with pytest.raises(ValueError):
        UnifiWriter(url=fake.url, write_username=USER)
    with pytest.raises(ValueError):
        UnifiWriter(url="ftp://x", write_username=USER, write_password=PW)


def test_capabilities_and_endpoints(fake):
    w = writer(fake)
    caps = w.capabilities()
    assert {k: v for k, v in caps["write"].items() if k.startswith(("move", "client."))} == \
        {"move": "B", "client.set.name": "B", "client.set.fixed_ip": "B", "client.set.network": "B", "client.block": "B*"}
    assert caps["write"]["network.create"] == "B" and caps["write"]["forward.create"] == "B*" and "network.update.vlan" not in caps["write"]
    assert caps["limits"]["logins_per_hour"] == 4 and w.endpoints() == ["127.0.0.1"]


# ---------------------------------------------------------------------------- reading
def test_read_gives_the_neutral_state_with_offline_configured_clients(fake):
    st = writer(fake).read()
    devs = {d["id"]: d for d in st["devices"]}
    assert devs[PHONE]["online"] is True and devs[PHONE]["network"] == LAN
    assert devs[PRINTER]["online"] is False and devs[PRINTER]["fixed_ip"] == "192.168.50.40" and devs[PRINTER]["network"] == LAN
    assert st["controller"]["kind"] == "controller" and st["side_effects"]["wireless_move"] == "reprovision_all"
    assert fake.methods() <= {"GET", "POST"}


# ---------------------------------------------------------------------------- single steps
def test_move_a_wireless_client(fake):
    w = writer(fake)
    st = w.read()
    pv = fwmodel.preview(st, {PHONE: LAB})
    step = pv["steps"][0]
    assert step["kind"] == "move" and step["after"]["ip"].startswith("203.0.113.")
    w.apply(step)
    u = fake.user(U_PHONE)
    assert u["virtual_network_override_enabled"] is True and u["virtual_network_override_id"] == fx.C_NET_LAB
    assert u["use_fixedip"] is True and u["fixed_ip"] == step["after"]["ip"] and u["network_id"] == fx.C_NET_LAB
    n = len(fake.puts)
    w.apply(step)                                       # idempotent: already as planned, nothing written
    assert len(fake.puts) == n


def test_rename_and_fixed_address(fake):
    w = writer(fake)
    st = w.read()
    pv = fwmodel.preview(st, {}, edits={PHONE: {"name": "Kitchen tablet", "fixed_ip": "192.168.50.60"}})
    assert not pv["blocks"] and [s["kind"] for s in pv["steps"]] == ["client.set"]
    w.apply(pv["steps"][0])
    u = fake.user(U_PHONE)
    assert u["name"] == "Kitchen tablet" and u["use_fixedip"] is True and u["fixed_ip"] == "192.168.50.60" and u["network_id"] == fx.C_NET_LAN
    st = w.read()
    pv = fwmodel.preview(st, {}, edits={PHONE: {"fixed_ip": ""}})
    w.apply(pv["steps"][0])
    assert fake.user(U_PHONE)["use_fixedip"] is False


@pytest.mark.parametrize("ip,key", [("10.9.9.9", "unifi.address_outside"), ("192.168.50.1", "unifi.address_outside"),
                                    ("192.168.50.40", "unifi.address_taken"), ("192.168.50.20", "unifi.address_taken")])
def test_addresses_the_controller_would_take_are_refused_before_writing(fake, ip, key):
    w = writer(fake)
    w.read()
    step = {"kind": "client.set", "object": PHONE, "before": {"fixed_ip": ""}, "after": {"fixed_ip": ip}}
    with pytest.raises(WriteError) as exc:
        w.apply(step)
    assert exc.value.key == key and not fake.puts


def test_unknown_or_non_local_networks_are_refused(fake):
    w = writer(fake)
    st = w.read()
    for net, key in ((WAN, "unifi.network_not_local"), ("n000000000000", "unifi.network_unknown")):
        step = {"kind": "move", "device": PHONE, "before": {"network": LAN, "ip": "192.168.50.30"}, "after": {"network": net, "ip": "10.0.0.5"}}
        with pytest.raises(WriteError) as exc:
            w.apply(step)
        assert exc.value.key == key
    assert w.lockout(st, {"kind": "move", "device": PHONE, "after": {"network": WAN}}) == "fw.block.network_kind"
    assert not fake.puts


def test_a_wired_client_is_not_moved_by_a_client_setting(fake):
    w = writer(fake)
    st = w.read()
    step = {"kind": "move", "device": OFFICE, "before": {"network": LAN, "ip": "192.168.50.20"}, "after": {"network": LAB, "ip": "203.0.113.50"}}
    assert w.lockout(st, step) == "fw.block.wired_move"
    with pytest.raises(WriteError):
        w.apply(step)
    assert not fake.puts


def test_a_write_that_did_not_take_is_an_error(fake):
    w = writer(fake)
    w.read()
    fake.ignore_put = True
    with pytest.raises(WriteError) as exc:
        w.apply({"kind": "client.set", "object": PHONE, "before": {"name": "Phone"}, "after": {"name": "Tablet"}})
    assert exc.value.key == "unifi.not_applied"


def test_the_controllers_own_refusal_is_passed_on(fake):
    w = writer(fake)
    w.read()
    fake.user(U_GUEST).update(use_fixedip=True, fixed_ip="192.168.50.77", network_id=fx.C_NET_LAN)   # unknown to the last read
    with pytest.raises(WriteError) as exc:
        w.apply({"kind": "client.set", "object": PHONE, "before": {"fixed_ip": ""}, "after": {"fixed_ip": "192.168.50.77"}})
    assert exc.value.key == "unifi.write_refused" and exc.value.detail == "api.err.DuplicateFixedIP"


# ---------------------------------------------------------------------------- inside the executor pipeline
def test_a_plan_runs_and_its_inverse_puts_everything_back(fake, tmp_path):
    w = writer(fake)
    r = runner(w, tmp_path)
    plan = plan_for(w, {PHONE: LAB}, {PRINTER: {"name": "Office printer"}})
    assert plan["v"] == 2
    res = r.run(plan)
    assert res["status"] == "done", res
    assert fake.user(U_PRINTER)["name"] == "Office printer" and fake.user(U_PHONE)["virtual_network_override_id"] == fx.C_NET_LAB
    st = w.read()
    back = {"v": 2, "steps": res["inverse"]}
    for s in back["steps"]:
        if s["kind"] == "move":
            s["before"]["ip"] = next(d["ip"] for d in st["devices"] if d["id"] == s["device"])
    w.restore(json.loads((tmp_path / "ex" / "backup" / plan["id"] / "before.json").read_text())["snapshot"])
    assert fake.user(U_PRINTER)["name"] == "Printer" and fake.user(U_PHONE)["virtual_network_override_enabled"] is False
    assert fake.user(U_PHONE)["use_fixedip"] is False


def test_partial_write_is_rolled_back(fake, tmp_path):
    w = writer(fake)
    r = runner(w, tmp_path)
    plan = plan_for(w, {}, {PHONE: {"name": "Tablet"}, PRINTER: {"name": "Office printer"}})
    fake.fail_put = 2                                    # the second write breaks
    res = r.run(plan)
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.apply_failed"
    assert fake.user(U_PHONE)["name"] == "Phone" and fake.user(U_PRINTER)["name"] == "Printer"
    assert not (tmp_path / "ex" / "active.json").exists()


def test_network_loss_mid_write_is_rolled_back(fake, tmp_path):
    w = writer(fake)
    r = runner(w, tmp_path)
    plan = plan_for(w, {}, {PHONE: {"name": "Tablet", "fixed_ip": "192.168.50.61"}})
    fake.drop_put = 1                                    # stored, but the reply never arrives
    res = r.run(plan)
    assert res["status"] == "rolled_back"
    u = fake.user(U_PHONE)
    assert u["name"] == "Phone" and u.get("use_fixedip") is False


def test_a_crash_mid_plan_is_put_back_on_the_next_start(fake, tmp_path):
    w = writer(fake)
    plan = plan_for(w, {}, {PHONE: {"name": "Tablet"}, PRINTER: {"name": "Office printer"}})

    class Crash(BaseException):
        pass

    real_put = w._put
    calls = []

    def put_then_die(uid, body):
        calls.append(uid)
        real_put(uid, body)
        if len(calls) == 1:
            raise Crash()                                # the process dies after the first write

    w._put = put_then_die
    with pytest.raises(Crash):
        runner(w, tmp_path).run(plan)
    assert (tmp_path / "ex" / "active.json").exists() and fake.user(U_PHONE)["name"] == "Tablet"
    fresh = writer(fake)                                 # a new process: new adapter, same state directory
    fresh.read()
    res = runner(fresh, tmp_path).recover()
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.recovered"
    assert fake.user(U_PHONE)["name"] == "Phone" and fake.user(U_PRINTER)["name"] == "Printer"
    assert not (tmp_path / "ex" / "active.json").exists()


def test_a_moved_client_that_stays_in_its_old_network_is_put_back(fake, tmp_path):
    w = writer(fake)
    r = Runner(w, tmp_path / "ex", target="controller", require_signature=False, watch_s=3, check_s=1,
               sleep=lambda s: None, clock=iter(range(1000, 2000)).__next__)
    plan = plan_for(w, {PHONE: LAB})
    plan["created"], plan["expires"] = 1000, 1300
    fake.stuck = True
    fake.classic["sta"]["data"][3]["network_id"] = fx.C_NET_LAN
    real_put = fake.put_user

    def keep_network(uid, body):
        status, reply = real_put(uid, body)
        fake.sta(fx.MAC_PHONE)["network_id"] = fx.C_NET_LAN      # the phone reconnects in the old network
        return status, reply

    fake.put_user = keep_network
    res = r.run(plan)
    assert res["status"] == "rolled_back" and res["reason"] == "fw.res.rolled_back_device"
    assert fake.user(U_PHONE)["virtual_network_override_enabled"] is False


def test_the_gate_refuses_a_plan_when_the_client_changed_meanwhile(fake, tmp_path):
    w = writer(fake)
    plan = plan_for(w, {}, {PHONE: {"name": "Tablet"}})
    fake.user(U_PHONE)["name"] = "Renamed elsewhere"
    fake.sta(fx.MAC_PHONE)["name"] = "Renamed elsewhere"
    res = runner(w, tmp_path).run(plan)
    assert res["status"] == "refused" and res["reason"] == "fw.block.conflict" and not fake.puts


# ---------------------------------------------------------------------------- sessions and the sign-in cap
def test_one_new_sign_in_after_401_then_the_session_is_given_up(fake):
    w = writer(fake)
    w.read()
    logins = fake.logins
    fake.expire_next = 1
    w.read()
    assert fake.logins == logins + 1
    fake.always_401 = True
    with pytest.raises(WriteError) as exc:
        w.read()
    assert exc.value.key == "unifi.session_refused" and w._dead
    n = fake.logins
    with pytest.raises(WriteError):
        w.read()
    assert fake.logins == n                               # no further sign-in through this session
    assert w.health() == (False, "session refused")


def test_403_never_signs_in_again(fake):
    w = writer(fake)
    w.read()
    n = fake.logins
    fake.fail[r"stat/sysinfo"] = 403
    with pytest.raises(WriteError) as exc:
        w._get("sysinfo")
    assert exc.value.key == "unifi.forbidden" and fake.logins == n


def test_console_writes_carry_the_csrf_token():
    f = WritableFake(style="console")
    try:
        w = writer(f)
        w.read()
        w.apply({"kind": "client.set", "object": PHONE, "before": {"name": "Phone"}, "after": {"name": "Tablet"}})
        put = [r for r in f.requests if r[0] == "PUT"][0]
        assert put[2].get("x-csrf-token") == "csrf-for-tests" and "token=" in put[2].get("cookie", "").lower()
    finally:
        f.stop()


def test_csrf_from_the_token_cookie():
    import base64
    payload = base64.urlsafe_b64encode(json.dumps({"csrfToken": "abc-123"}).encode()).decode().rstrip("=")
    assert session.csrf_from_token("h.%s.s" % payload) == "abc-123"
    assert session.csrf_from_token("nonsense") is None and session.csrf_from_token(None) is None
    s = session.Session("k")
    s.absorb([("Set-Cookie", "TOKEN=h.%s.s; Path=/; Partitioned" % payload)])
    assert s.headers()["X-CSRF-Token"] == "abc-123"


def test_sign_in_cap_holds_across_processes(tmp_path):
    clock = [10000.0]
    a = session.FileLedger(tmp_path / "cap.json", per_hour=4, clock=lambda: clock[0])
    b = session.FileLedger(tmp_path / "cap.json", per_hour=4, clock=lambda: clock[0])
    for led in (a, b, a, b):
        led.book()
    with pytest.raises(session.LoginPaused):
        a.book()
    with pytest.raises(session.LoginPaused):
        b.book()
    clock[0] += 3601
    a.book()
    b.refused()
    with pytest.raises(session.LoginPaused):
        a.book()
    clock[0] += 901
    a.book()
    assert oct(os.stat(tmp_path / "cap.json").st_mode & 0o777) == "0o600"
    (tmp_path / "cap.json").write_text("{broken")
    with pytest.raises(session.LoginPaused):              # tampered: wait rather than flood
        a.book()


def test_a_refused_sign_in_pauses_the_writer(fake, tmp_path):
    w = writer(fake, tmp_path)
    fake.login_refused = 401
    with pytest.raises(WriteError) as exc:
        w.read()
    assert exc.value.key == "unifi.sign_in_refused"
    n = fake.logins
    fake.login_refused = 0
    w2 = writer(fake, tmp_path)                           # another process, same state directory
    with pytest.raises(WriteError) as exc:
        w2.read()
    assert exc.value.key == "unifi.sign_in_paused" and fake.logins == n
    assert w2.health() == (False, "sign-in paused")


def test_health_refuses_a_read_only_account(fake):
    fake.self_reply = fx.classic_wrap([{"name": USER, "site_role": "readonly"}])
    w = writer(fake)
    assert w.health() == (False, "account cannot write")


def test_health_is_fine_with_a_site_admin(fake):
    assert writer(fake).health() == (True, "")


# ---------------------------------------------------------------------------- certificate pin
def test_certificate_pin_must_be_a_sha256():
    with pytest.raises(transport.TransportError):
        transport.Transport("https://127.0.0.1", wmod.ALLOW, pin_sha256="abc")
    t = transport.Transport("https://127.0.0.1", wmod.ALLOW, pin_sha256="AB:" * 31 + "AB")
    assert t._pin == "ab" * 32


# ---------------------------------------------------------------------------- the executor's connection guard
def _listener():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    threading.Thread(target=lambda: [srv.accept() for _ in range(2)], daemon=True).start()
    return srv


def test_connection_guard_allows_only_its_list():
    srv = _listener()
    port = srv.getsockname()[1]
    try:
        connguard.install(["127.0.0.2"])
        with pytest.raises(connguard.Blocked):
            socket.create_connection(("127.0.0.1", port), timeout=2)
        connguard.install(["127.0.0.1"], denied=["127.0.0.1"])     # the denied list wins
        with pytest.raises(connguard.Blocked):
            socket.create_connection(("127.0.0.1", port), timeout=2)
        connguard.install(["localhost"])
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    finally:
        connguard.uninstall()
        srv.close()
    assert not connguard.active()


def test_executor_builds_the_unifi_writer_with_its_state_dir(tmp_path, monkeypatch):
    from lanloom import executor as ex
    monkeypatch.setitem(integrations._EXECUTOR, "on", True)
    (tmp_path / "armed").write_text("")
    opt = tmp_path / "opt.json"
    opt.write_text(json.dumps({"url": "https://192.0.2.10", "write_username": "lanloom-writer", "write_password": "x" * 12,
                               "cert_sha256": "00" * 32}))
    os.chmod(opt, 0o600)
    env = {"LANLOOM_EXECUTOR_STATE": str(tmp_path), "LANLOOM_EXECUTOR_TARGET": "controller", "LANLOOM_EXECUTOR_ARM": "I-UNDERSTAND",
           "LANLOOM_EXECUTOR_ADAPTER": "unifi", "LANLOOM_EXECUTOR_ADAPTER_OPTIONS": str(opt),
           "LANLOOM_EXECUTOR_DENY_HOSTS": "192.0.2.99, 192.0.2.98"}
    s = ex.Settings(env)
    a = ex.build_adapter(s)
    assert isinstance(a, UnifiWriter) and a._ledger.path == str(tmp_path / "unifi-signins.json")
    assert ex.guard_hosts(s, a) == ["127.0.0.1", "192.0.2.10"] and s.deny == ("192.0.2.99", "192.0.2.98")
    info = ex.executor_info(s, a)
    assert info["family"] == "unifi" and "client.set.fixed_ip" in info["capabilities"]["write"]
    opt.write_text(json.dumps({"url": "https://192.0.2.10", "write_username": "u", "write_password": "p", "state_dir": "/tmp"}))
    with pytest.raises(SystemExit, match="state_dir"):
        ex.build_adapter(s)
    opt.write_text(json.dumps({"url": "https://192.0.2.10", "write_username": "u", "write_password": "p", "pasword": "x"}))
    with pytest.raises(SystemExit, match="unknown option"):
        ex.build_adapter(s)


def test_executor_status_of_a_v1_executor_keeps_client_edits_out(tmp_path):
    """An executor of an earlier release publishes no capabilities: the editor offers moves only (no client settings)."""
    from lanloom.integrations import capabilities as capmod
    assert capmod.stage_for(capmod.V1_DEFAULT, {"kind": "client.set", "object": "x", "before": {"name": "a"}, "after": {"name": "b"}}) is None


def test_write_half_has_a_single_send_path():
    src = (wmod.__file__ and open(wmod.__file__, encoding="utf-8").read())
    assert "http.client" not in src and "socket" not in src and "urllib.request" not in src
    assert src.count("self._t.request(") == 1


def test_timeouts_are_bounded():
    t = time.time()
    w = UnifiWriter(url="http://127.0.0.1:9", write_username=USER, write_password=PW, timeout=1, sleep=lambda s: None)
    with pytest.raises(WriteError) as exc:
        w.read()
    assert exc.value.key == "integration.err.unreachable" and time.time() - t < 10


def test_model_marks_wired_clients_and_uses_the_last_network_of_offline_ones():
    um = model
    raw = fx.classic()
    raw["user"]["data"].append({"mac": "02:4c:4c:00:01:0a", "name": "Old tablet", "is_wired": False, "last_connection_network_id": fx.C_NET_LAB})
    devs = {d["name"]: d for d in um.to_neutral(raw)["devices"]}
    assert devs["Office PC"]["network_by_port"] is True and devs["Phone"]["network_by_port"] is False
    assert devs["Old tablet"]["online"] is False and devs["Old tablet"]["network"] == um.oid("n", fx.C_NET_LAB)
