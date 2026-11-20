"""Several sites in one instance (M1, lanloom/sites.py).

* Migration: an installation from before sites becomes the site "home" without losing anything.
* Visibility: every route a viewer or operator can call, the live socket and every static file are asked as a person
  who may see only one site - names and addresses of the other site never appear in an answer (leak test).
* Remote sites: no interfaces of this host, no scan without consent, host agents pair into the site their code was
  made in, the firewall editor stays at home, notifications go only to people who may see the site.
* Reports: the combined report names other sites only for readers who may see them; backups carry every site.
"""
import json
import os
import re
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lanloom import access, auth, sites as sites_mod
from lanloom import config as config_mod
from lanloom.main import create_app

HDR = {"X-Lanloom": "1"}
WEB = Path(__file__).resolve().parent.parent / "lanloom" / "web"
HOME_NAME = "Headquarters Zebra"
# markers that belong to one site only (addresses, names of devices and connections, the site name)
HOME_MARKERS = ("192.168.50.", "gateway.lan", "core-switch.lan", "Automation hub", "Message broker", HOME_NAME)
BRANCH_MARKERS = ("10.44.7.", "branch-gw", "printer-frontdesk", "camera-entrance", "Branch controller", "Branch office", "branch-server")


@pytest.fixture()
def two_sites(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_DEMO_SEED", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        st = app.state.ll
        branch = [r["id"] for r in st.sites.rows() if r["id"] != "home"][0]
        bcore = st.sites.core(branch)
        bcore.demo.seed(bcore)
        bcore.demo.tick(bcore, time.time())
        st.sites.rename("home", HOME_NAME)
        c.st, c.branch, c.app = st, branch, app
        c.uids = {
            "admin": st.accounts.create("site-admin", "a-long-test-password", role="admin"),
            "home_viewer": st.accounts.create("home-viewer", "a-long-test-password", role="viewer"),
            "branch_viewer": st.accounts.create("branch-viewer", "a-long-test-password", role="viewer"),
            "branch_operator": st.accounts.create("branch-operator", "a-long-test-password", role="operator"),
        }
        st.sites.set_user_sites(c.uids["home_viewer"], ["home"])
        st.sites.set_user_sites(c.uids["branch_viewer"], [branch])
        st.sites.set_user_sites(c.uids["branch_operator"], [branch])
        yield c


def sign_in(c, who):
    c.cookies.clear()
    c.cookies.set(auth.SESSION_COOKIE, c.st.sessions.create(c.uids[who], ip="127.0.0.1", agent="test"))


def found(text: str, markers) -> list:
    return [m for m in markers if m in text]


# ---------------------------------------------------------------------------------------------------------- migration
def test_an_installation_from_before_sites_becomes_home_without_loss(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_DEMO_SEED", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    st = app.state.ll
    uid = st.accounts.create("old-admin", "a-long-test-password", role="viewer")
    devices = st.store.query("SELECT id, ip, title FROM devices ORDER BY id")
    integrations = st.store.query("SELECT id, kind, name FROM integrations ORDER BY id")
    assert devices and integrations
    st.store.close()
    # the database as an older Lanloom left it: no site list, no site column on accounts
    db = sqlite3.connect(str(tmp_path / "lanloom.db"))
    db.execute("DROP TABLE sites")
    db.execute("ALTER TABLE users DROP COLUMN sites")
    db.execute("DELETE FROM settings WHERE key=?", ("demo_sites_seeded",))
    db.commit()
    db.close()
    monkeypatch.setenv("LANLOOM_DEMO_SEED", "0")
    app2 = create_app(config_mod.load())
    st2 = app2.state.ll
    assert [r["id"] for r in st2.sites.rows()] == ["home"]
    assert st2.sites.rows()[0]["kind"] == "local"
    assert st2.store.query("SELECT id, ip, title FROM devices ORDER BY id") == devices
    assert st2.store.query("SELECT id, kind, name FROM integrations ORDER BY id") == integrations
    assert sites_mod.parse_user_sites(st2.store.one("SELECT sites FROM users WHERE id=?", (uid,))["sites"]) == "*"
    assert st2.sites.allowed({"id": uid, "role": "viewer"}) == ["home"]
    assert st2.real_core.site_id == "home" and not st2.real_core.remote
    assert not (tmp_path / "sites").exists()            # nothing moved, no folder for a site that does not exist


# ---------------------------------------------------------------------------------------------------------- visibility
def concrete(path: str, c, foreign: str) -> str:
    st = c.st
    home_dev = st.store.one("SELECT id FROM devices ORDER BY id LIMIT 1")["id"]
    branch_dev = st.sites.core(c.branch).store.one("SELECT id FROM devices ORDER BY id LIMIT 1")["id"]
    dev = home_dev if foreign == "home" else branch_dev        # ask for an object of the site one may NOT see
    day = (st.real_core.reports.latest() or {}).get("day") or "2026-01-01"
    values = {"device_id": dev, "comp_id": dev, "subject": "dev:" + dev, "integ_id": "1", "entity_id": "light.kitchen",
              "agent_id": "ag-demo0storage" if foreign == "home" else "ag-demo0branch", "day": day, "lang": "en", "job_id": "1"}

    def fill(m):
        return values.get(m.group(1).split(":")[0], "x1")
    return re.sub(r"\{([^}]+)\}", fill, path)


def visible_get_routes(role: str):
    for (method, path), rule in access.RULES.items():
        if method == "GET" and path.startswith("/api/") and rule.role in access.ROLES and access.allows(role, rule.role):
            yield path


@pytest.mark.parametrize("who,foreign,markers", [
    ("branch_operator", "home", HOME_MARKERS),
    ("branch_viewer", "home", HOME_MARKERS),
    ("home_viewer", "branch", BRANCH_MARKERS),
])
def test_no_answer_names_a_site_the_person_may_not_see(two_sites, who, foreign, markers):
    c = two_sites
    sign_in(c, who)
    role = "operator" if who.endswith("operator") else "viewer"
    leaks = {}
    asked = 0
    for path in visible_get_routes(role):
        url = concrete(path, c, foreign)
        for extra in ({}, {sites_mod.HEADER: foreign}):                  # asking for the other site changes nothing
            c.cookies.set(sites_mod.COOKIE, foreign)
            r = c.get(url, headers=dict(HDR, **extra), params={"q": "1", "window": "24h"})
            asked += 1
            hits = found(r.text, markers)
            if hits:
                leaks[(path, bool(extra))] = (r.status_code, hits)
    assert asked > 100
    assert not leaks, leaks


def test_own_site_is_answered_and_the_other_site_is_never_picked(two_sites):
    c = two_sites
    sign_in(c, "branch_viewer")
    c.cookies.set(sites_mod.COOKIE, "home")
    # M1T: naming a site one may not see is refused, never answered from another (not even the own) site
    r = c.get("/api/devices", headers={sites_mod.HEADER: "home"})
    assert r.status_code == 404 and r.json()["error"] == "site.err.unknown" and not found(r.text, HOME_MARKERS)
    c.cookies.set(sites_mod.COOKIE, c.branch)
    r = c.get("/api/devices")
    ips = [d["ip"] for d in r.json()["devices"]]
    assert ips and all(ip.startswith("10.44.7.") for ip in ips)
    c.cookies.delete(sites_mod.COOKIE)
    r = c.get("/api/devices")                                   # a request that names no site gets the person's first one
    assert r.headers["x-lanloom-site"] == c.branch and "10.44.7." in r.text
    s = c.get("/api/sites").json()
    assert [x["id"] for x in s["sites"]] == [c.branch] and s["current"] == c.branch and not s["several"]
    state = c.get("/api/state").json()
    assert state["site"]["id"] == c.branch and state["site"]["name"] == "Branch office" and not state["site"]["several"]
    sign_in(c, "admin")
    c.cookies.set(sites_mod.COOKIE, c.branch)
    assert c.get("/api/state").json()["site"]["several"] is True
    ov = c.get("/api/sites/overview").json()
    assert {x["id"] for x in ov["sites"]} == {"home", c.branch}
    branch = [x for x in ov["sites"] if x["id"] == c.branch][0]
    assert branch["devices"]["total"] == 9 and branch["devices"]["online"] == 8 and "agent" in branch["reach"]


def test_the_live_socket_carries_only_the_own_site(two_sites):
    c = two_sites
    sign_in(c, "branch_viewer")
    time.sleep(1.5)                                         # one live frame of each site
    with c.websocket_connect("/api/live", headers={"Origin": "http://testserver"}) as ws:
        text = ws.receive_text()
    assert json.loads(text)["site"] == c.branch             # every frame names its site (M1T)
    # device ids carry the hardware address: the sample branch office is 02:4c:4c:01:..., the home network 02:4c:4c:00:...
    assert "d-024c4c01" in text and "d-024c4c00" not in text and not found(text, HOME_MARKERS)


def test_static_files_never_name_a_site(two_sites):
    c = two_sites
    sign_in(c, "home_viewer")
    files = [p for p in WEB.rglob("*") if p.is_file()]
    assert len(files) > 50
    for p in files:
        r = c.get("/" + str(p.relative_to(WEB)).replace(os.sep, "/"))
        assert r.status_code == 200
        if p.suffix in (".js", ".css", ".html", ".json", ".webmanifest", ".svg"):
            assert not found(r.text, BRANCH_MARKERS + (HOME_NAME,)), p.name
    for lang in ("en", "de"):
        assert not found(c.get("/api/i18n/" + lang).text, BRANCH_MARKERS + (HOME_NAME,))


def test_a_person_without_any_site_sees_no_site_at_all(two_sites):
    c = two_sites
    st = c.st
    st.sites.set_user_sites(c.uids["branch_viewer"], [])
    sign_in(c, "branch_viewer")
    state = c.get("/api/state").json()
    assert state["authenticated"] and not state["enroll"] and state["site"]["none"] is True
    assert state["hubs"] == [] and not found(json.dumps(state), HOME_MARKERS + BRANCH_MARKERS)
    r = c.get("/api/devices")
    assert r.status_code == 403 and r.json()["error"] == "site.err.none"
    assert c.get("/api/sites").json()["sites"] == []
    with pytest.raises(Exception):
        with c.websocket_connect("/api/live", headers={"Origin": "http://testserver"}) as ws:
            ws.receive_text()


def test_api_keys_follow_the_sites_of_their_account(two_sites):
    c = two_sites
    st = c.st
    token = st.tokens.create({"id": c.uids["branch_viewer"], "role": "viewer"}, "probe", "read")["token"]
    c.cookies.clear()
    r = c.get("/api/v1/devices", headers={"Authorization": "Bearer " + token, sites_mod.HEADER: "home"})
    assert r.status_code == 404 and not found(r.text, HOME_MARKERS)            # a site the account may not see: refused (M1T)
    r = c.get("/api/v1/devices", headers={"Authorization": "Bearer " + token})
    assert r.status_code == 200
    assert "10.44.7." in r.text and not found(r.text, HOME_MARKERS)


# ---------------------------------------------------------------------------------------------------------- remote sites
def test_a_remote_site_never_uses_this_host_and_never_scans_without_consent(two_sites):
    c = two_sites
    core = c.st.sites.core(c.branch)
    assert core.remote and core.site_id == c.branch
    assert core.networks() == []
    assert "host" not in core._source_tasks
    with pytest.raises(ValueError) as exc:
        core.start_scan(["10.44.7.0/24"])
    assert str(exc.value) == "site.err.scan_consent"
    with pytest.raises(ValueError):
        core.update_settings({"netflow_enabled": True, "netflow_exporters": ["10.44.7.1"]})
    assert core.settings()["netflow_enabled"] is False
    sign_in(c, "admin")
    c.cookies.set(sites_mod.COOKIE, c.branch)
    r = c.post("/api/scan", headers=HDR, json={"cidrs": ["10.44.7.0/24"]})
    assert r.status_code == 400 and r.json()["error"] == "site.err.scan_consent"


def test_the_firewall_editor_and_assistant_stay_at_home(two_sites):
    c = two_sites
    sign_in(c, "admin")
    c.cookies.set(sites_mod.COOKIE, c.branch)
    for path in ("/api/firewall", "/api/firewall/insight", "/api/ai/hints"):
        r = c.get(path)
        assert r.status_code == 409 and r.json()["error"] == "site.err.primary_only", path
    c.cookies.set(sites_mod.COOKIE, "home")
    assert c.get("/api/firewall").status_code == 200


def test_a_host_agent_joins_the_site_its_code_was_made_in(two_sites):
    c = two_sites
    st = c.st
    sign_in(c, "admin")
    c.cookies.set(sites_mod.COOKIE, c.branch)
    code = c.post("/api/agents/invite", headers=HDR, json={"name": "branch-nas"}).json()["code"]
    c.cookies.clear()
    from lanloom import agents as agents_mod
    r = c.post("/api/agent/pair", headers=HDR, json={"code": code, "proto": agents_mod.PROTO, "version": "1",
                                                      "host": {"name": "branch-nas", "ips": ["10.44.7.20"]}})
    assert r.status_code == 200, r.text
    aid, token = r.json()["agent_id"], r.json()["token"]
    assert st.sites.core(c.branch).store.one("SELECT id FROM agent_hosts WHERE id=?", (aid,))
    assert not st.store.one("SELECT id FROM agent_hosts WHERE id=?", (aid,))
    rep = c.post("/api/agent/report", headers=dict(HDR, Authorization="Bearer " + token),
                 json={"proto": agents_mod.PROTO, "t": time.time(), "interval": 10})
    assert rep.status_code in (200, 400, 409)
    assert rep.status_code != 401


def test_notifications_of_a_site_go_only_to_people_who_may_see_it(two_sites):
    c = two_sites
    st = c.st
    names = lambda core: {u["username"] for u in core.notify.users()}      # noqa: E731
    branch = names(st.sites.core(c.branch))
    home = names(st.real_core)
    assert "branch-viewer" in branch and "home-viewer" not in branch and "site-admin" in branch
    assert "home-viewer" in home and "branch-viewer" not in home


def test_rights_change_and_removing_a_site_cleans_up(two_sites):
    c = two_sites
    st = c.st
    sign_in(c, "admin")
    r = c.post("/api/sites", headers=HDR, json={"name": "Holiday flat"})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert r.json()["kind"] == "remote" and sid in st.sites.cores
    assert c.post("/api/sites", headers=HDR, json={"name": "holiday flat"}).json()["error"] == "site.err.name_taken"
    r = c.patch("/api/users/%d" % c.uids["home_viewer"], headers=HDR, json={"sites": ["home", sid]})
    assert r.status_code == 200 and r.json()["sites"] == ["home", sid]
    assert c.patch("/api/users/%d" % c.uids["home_viewer"], headers=HDR, json={"sites": ["s-00000000"]}).status_code == 400
    folder = Path(st.cfg.data_dir) / "sites" / sid
    assert folder.is_dir()
    assert c.delete("/api/sites/home", headers=HDR).json()["error"] == "site.err.home"
    assert c.delete("/api/sites/" + sid, headers=HDR).json() == {"ok": True}
    assert sid not in st.sites.cores and not folder.exists()
    assert sites_mod.parse_user_sites(st.store.one("SELECT sites FROM users WHERE id=?", (c.uids["home_viewer"],))["sites"]) == ["home"]
    actions = [r["action"] for r in st.audit.between(0, time.time() + 5, ("sites.",))]
    assert "sites.create" in actions and "sites.delete" in actions


def test_a_site_from_a_controller_copies_the_connection_with_the_other_site(two_sites):
    c = two_sites
    st = c.st
    sign_in(c, "admin")
    c.cookies.set(sites_mod.COOKIE, "home")
    ctl = [i for i in st.real_core.integrations() if i["kind"] == "controller"][0]
    listing = c.get("/api/sites/controller/%d" % ctl["id"]).json()
    assert listing["ok"] and {"ref": "branch", "name": "Branch office"} in listing["sites"]
    r = c.post("/api/sites/from-controller", headers=HDR, json={"integration": ctl["id"], "ref": "storage", "name": "Storage unit"})
    assert r.status_code == 200, r.text
    core = st.sites.core(r.json()["id"])
    copied = core.integrations()
    assert len(copied) == 1 and copied[0]["kind"] == "controller" and copied[0]["config"]["site"] == "storage"
    assert c.post("/api/sites/from-controller", headers=HDR,
                  json={"integration": ctl["id"], "ref": "default", "name": "Same"}).json()["error"] == "site.err.same_controller_site"


# ---------------------------------------------------------------------------------------------------------- reports
def test_the_combined_report_names_other_sites_only_for_readers_who_may_see_them(two_sites):
    c = two_sites
    st = c.st
    st.sites.set_report_mode("combined")
    sign_in(c, "admin")
    c.cookies.set(sites_mod.COOKIE, "home")
    admin_view = c.get("/api/reports/preview").json()
    secs = {s["id"]: s for s in admin_view["sections"]}
    assert "sites" in secs and secs["sites"]["items"][0]["vars"]["name"] == "Branch office"
    sign_in(c, "home_viewer")
    viewer = c.get("/api/reports/preview")
    assert viewer.status_code == 200
    assert "sites" not in {s["id"] for s in viewer.json()["sections"]}
    assert not found(viewer.text, BRANCH_MARKERS)
    # a further site delivers nothing on its own in the combined mode; per site it uses the home channels
    bcore = st.sites.core(c.branch)
    assert bcore.reports._via() is None
    st.sites.set_report_mode("per_site")
    assert bcore.reports._via() is st.real_core.reports


def test_a_backup_carries_every_site(two_sites):
    c = two_sites
    st = c.st
    bcore = st.sites.core(c.branch)
    before = bcore.store.query("SELECT id, ip FROM devices ORDER BY id")
    content = st.backups.collect(history=False)
    assert c.branch in content["sites"] and len(content["sites"][c.branch]["tables"]["devices"]) == len(before)
    assert {r["id"] for r in content["tables"]["sites"]} == {"home", c.branch}
    bcore.store.execute("DELETE FROM devices")
    data = st.backups._validated(content)
    st.backups.restore(data)
    assert bcore.store.query("SELECT id, ip FROM devices ORDER BY id") == before


# ---------------------------------------------------------------------------------------------------------- adapters
def test_the_controller_lists_its_sites_read_only():
    import asyncio
    from lanloom.sources.controller import Controller
    c = Controller("https://10.0.0.1:8443", "viewer", "a-password", site="default", style="classic")
    seen = []

    async def fake_call(method, path, body=None):
        seen.append((method, path))
        if path == "/api/login":
            c._cookies = {"session": "x"}
            return 200, b'{"meta": {"rc": "ok"}}'
        if path == "/api/self/sites":
            return 200, json.dumps({"meta": {"rc": "ok"}, "data": [{"name": "default", "desc": "Default"}, {"name": "branch", "desc": "Branch"},
                                                                    {"name": "bad name", "desc": "dropped"}]}).encode()
        return 200, b"{}"
    c._call = fake_call
    ok, key, found = asyncio.run(c.sites())
    assert ok and found == [{"ref": "default", "name": "Default"}, {"ref": "branch", "name": "Branch"}]
    assert ("GET", "/api/self/sites") in seen and ("POST", "/api/logout") in seen
    assert all(m == "GET" or p in ("/api/login", "/api/logout") for m, p in seen)


# ---------------------------------------------------------------------------------------------------------- M2: no mixed picture
def ask_as(c, who, asked, cookie=None):
    """Sign in as ``who`` and ask for ``asked`` the way a page does: the page's own site in the header, the cookie
    (shared by every tab) possibly of another site."""
    sign_in(c, who)
    if cookie:
        c.cookies.set(sites_mod.COOKIE, cookie)
    return {"X-Lanloom-Site": asked}


def test_every_answer_names_the_site_it_was_made_for_and_no_cache_may_mix_them(two_sites):
    c = two_sites
    for who, own in (("branch_viewer", c.branch), ("home_viewer", "home")):
        other = "home" if own != "home" else c.branch
        for asked in (other, own, None):                       # a page that shows another site than its rights allow, its own, none
            hdr = ask_as(c, who, asked, cookie=other) if asked else ask_as(c, who, "", cookie=None)
            if not asked:
                hdr = {}
            seen = refused = 0
            for path in visible_get_routes("viewer"):
                r = c.get(concrete(path, c, other), headers=dict(HDR, **hdr), params={"q": "1", "window": "24h"})
                mirrored = r.headers.get("x-lanloom-site")
                assert mirrored in (None, own), (who, asked, path, mirrored)       # never the site the person may not see
                vary = r.headers.get("vary", "").lower()
                assert "cookie" in vary and "x-lanloom-site" in vary, (path, vary)
                seen += mirrored == own
                if asked == other and sites_mod.scope_of(path) != "global":
                    # M1T: a named site one may not see is refused on every site-bound route, never answered from the own site
                    assert r.status_code == 404 and mirrored is None, (who, path, r.status_code, mirrored)
                    refused += 1
            if asked == other:
                assert refused > 80
            else:
                assert seen > 80                               # the site-bound and the global routes name their site alike
    # a person who may see both: the answer follows the asked site, whatever the shared cookie says
    for asked, cookie in (("home", c.branch), (c.branch, "home")):
        hdr = ask_as(c, "admin", asked, cookie=cookie)
        r = c.get("/api/devices", headers=dict(HDR, **hdr))
        assert r.headers["x-lanloom-site"] == asked
        ips = [d["ip"] for d in r.json()["devices"]]
        assert ips and all(ip.startswith("10.44.7.") == (asked != "home") for ip in ips)
        assert c.get("/api/state", headers=hdr).json()["site"]["id"] == asked


def test_a_refused_route_and_an_error_name_their_site_too(two_sites):
    c = two_sites
    hdr = ask_as(c, "admin", c.branch, cookie="home")
    fw = [p for (m, p) in access.RULES if m == "GET" and p.startswith("/api/firewall") and "{" not in p]
    assert fw
    r = c.get(fw[0], headers=dict(HDR, **hdr))
    assert r.status_code == 409 and r.json()["error"] == "site.err.primary_only"
    assert r.headers["x-lanloom-site"] == c.branch
    r = c.get("/api/reports/1999-01-01", headers=dict(HDR, **hdr))          # an unknown day: an error answer names its site as well
    assert r.status_code >= 400 and r.headers["x-lanloom-site"] == c.branch


def test_the_live_socket_serves_the_site_the_page_asks_for_and_refuses_the_rest(two_sites):
    from starlette.websockets import WebSocketDisconnect
    c = two_sites
    time.sleep(1.5)
    origin = {"Origin": "http://testserver"}
    # a person with both sites: the page's own site (address) wins over the cookie of another tab
    for asked, cookie, own, foreign in (("home", c.branch, "d-024c4c00", "d-024c4c01"), (c.branch, "home", "d-024c4c01", "d-024c4c00")):
        sign_in(c, "admin")
        c.cookies.set(sites_mod.COOKIE, cookie)
        with c.websocket_connect("/api/live?site=" + asked, headers=origin) as ws:
            text = ws.receive_text()
        assert own in text and foreign not in text, (asked, cookie)
    # a person without the right: closed with the "site changed" code, no frame at all
    sign_in(c, "branch_viewer")
    c.cookies.set(sites_mod.COOKIE, "home")
    with pytest.raises(WebSocketDisconnect) as gone:
        with c.websocket_connect("/api/live?site=home", headers=origin) as ws:
            ws.receive_text()
    assert gone.value.code == sites_mod.WS_CHANGED
    # the shared cookie names a site this person may not see: refused as well (M1T), never the person's first site instead
    with pytest.raises(WebSocketDisconnect) as gone:
        with c.websocket_connect("/api/live", headers=origin) as ws:
            ws.receive_text()
    assert gone.value.code == sites_mod.WS_CHANGED
    # a socket that names no site at all gets the released one
    c.cookies.delete(sites_mod.COOKIE)
    with c.websocket_connect("/api/live", headers=origin) as ws:
        text = ws.receive_text()
    assert "d-024c4c01" in text and json.loads(text)["site"] == c.branch


def test_the_browser_code_sends_the_site_discards_foreign_answers_and_cancels_on_switch():
    """The page code, read as text (the browser run covers the switch itself)."""
    js = (WEB / "js" / "app.js").read_text()
    assert "X-Lanloom-Site" in js and "inflight" in js and "LL.leaveSite" in js and "LL.siteResync" in js
    views = (WEB / "js" / "views.js").read_text()
    assert "?site=" in views and "4409" in views and "shutdown()" in views
    sites_js = (WEB / "js" / "sites.js").read_text()
    assert "LL.leaveSite()" in sites_js and "LL.siteDeepLink" in sites_js
