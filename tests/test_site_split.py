"""Site separation, checked in full (M1T, after the house separation of the private map).

What the leak test of M1 (test_sites.py) does not cover:

* A person who may see BOTH sites asks every site-bound route for one site: the answer names that site, carries
  nothing of the other one and was made from that site's core only (an answer that read two cores names ``mixed``).
* A request that names a site - header, address, cookie - never falls back: an empty, unknown, malformed or not released
  site is 404 on every site-bound route, no redirect by a trailing slash, and the start state says "unknown" without
  any counts of another site.
* Ways in: an external report (``POST /api/v1/ingest``) lands in the site it names and is refused without one when there
  are several sites; a host agent reports only for the site it paired with; an empty site list never means "all".
* Links that leave the app (notifications, delivered reports, webhooks) name the site they are about.
* Every live frame names its site.
"""
import json
import time

import pytest

from lanloom import access, sites as sites_mod
from test_sites import (BRANCH_MARKERS, HDR, HOME_MARKERS, concrete, found, sign_in, two_sites,  # noqa: F401 (fixture)
                        visible_get_routes)

SITE_BOUND = [p for p in visible_get_routes("admin") if sites_mod.scope_of(p) != "global"]


def as_admin(c, asked, cookie=None):
    sign_in(c, "admin")
    if cookie:
        c.cookies.set(sites_mod.COOKIE, cookie)
    return dict(HDR, **{sites_mod.HEADER: asked})


# ---------------------------------------------------------------------------------------------------------- every route
def test_every_site_bound_route_answers_from_the_asked_site_only(two_sites):
    """A person who sees both sites: each answer comes from the core of the asked site, names it, and carries nothing of
    the other site - also when the shared cookie names the other site and the address asks for an object of it."""
    c = two_sites
    assert len(SITE_BOUND) > 100
    problems = {}
    for asked, other, markers in (("home", c.branch, BRANCH_MARKERS), (c.branch, "home", HOME_MARKERS)):
        hdr = as_admin(c, asked, cookie=other)
        for path in SITE_BOUND:
            for objects_of in (asked, other):              # own objects, then the ids of the other site in the address
                r = c.get(concrete(path, c, objects_of), headers=hdr, params={"q": "1", "window": "24h"})
                named = r.headers.get("x-lanloom-site")
                hits = found(r.text, markers)
                if named != asked or hits:
                    problems[(path, asked, objects_of)] = (r.status_code, named, hits)
    assert not problems, problems


def test_an_answer_that_read_two_sites_never_names_one_of_them():
    token = sites_mod.SERVED.set(set())
    try:
        assert sites_mod.served_name("home", "site") == "home"           # read nothing: the picked site
        sites_mod.note_served("s-12345678")
        assert sites_mod.served_name("home", "site") == "s-12345678"     # the site of the data, not of the request
        sites_mod.note_served("home")
        assert sites_mod.served_name("home", "site") == sites_mod.MIXED
        assert sites_mod.served_name("home", "global") == "home"
    finally:
        sites_mod.SERVED.reset(token)


# ---------------------------------------------------------------------------------------------------------- no fallback
@pytest.mark.parametrize("asked", ["", " ", "s-00000000", "S-B7A2D3E0", "*", "all", "home,s-b7a2d3e0", "../home", "null"])
def test_a_named_site_that_is_empty_unknown_or_malformed_is_refused(two_sites, asked):
    c = two_sites
    sign_in(c, "admin")
    hdr = dict(HDR, **{sites_mod.HEADER: asked})
    for path in ("/api/devices", "/api/map", "/api/overview/now", "/api/reports"):
        r = c.get(path, headers=hdr)
        assert r.status_code == 404 and r.json()["error"] == "site.err.unknown", (asked, path, r.status_code)
        assert "x-lanloom-site" not in r.headers and not found(r.text, HOME_MARKERS + BRANCH_MARKERS)
        assert "x-lanloom-site" in r.headers.get("vary", "").lower()
    # the same through the address (plain links, downloads) and through the cookie alone
    r = c.get("/api/devices", params={"site": asked}, headers=HDR)
    assert r.status_code == 404
    c.cookies.set(sites_mod.COOKIE, asked)
    assert c.get("/api/devices", headers=HDR).status_code == 404
    st = c.get("/api/state", headers=hdr).json()
    assert st["site"]["id"] is None and st["site"]["unknown"] is True and st["site"]["first"] == "home"
    assert st["reports_unread"] == 0 and st["hubs"] == [] and st["agents"] == 0


def test_a_site_the_person_may_not_see_is_refused_like_an_unknown_one(two_sites):
    c = two_sites
    sign_in(c, "home_viewer")
    for how in ("header", "address", "cookie"):
        c.cookies.delete(sites_mod.COOKIE)
        kw = {"headers": dict(HDR, **({sites_mod.HEADER: c.branch} if how == "header" else {}))}
        if how == "address":
            kw["params"] = {"site": c.branch}
        if how == "cookie":
            c.cookies.set(sites_mod.COOKIE, c.branch)
        r = c.get("/api/devices", **kw)
        assert r.status_code == 404 and r.json()["error"] == "site.err.unknown", how
        assert not found(r.text, BRANCH_MARKERS + HOME_MARKERS)


def test_the_address_names_the_site_of_a_plain_link(two_sites):
    """Plain links and downloads of a page carry only the cookie - the address ?site= names the page's site instead."""
    c = two_sites
    sign_in(c, "admin")
    c.cookies.set(sites_mod.COOKIE, "home")                 # the other tab switched the shared cookie
    r = c.get("/api/devices", params={"site": c.branch}, headers=HDR)
    assert r.headers["x-lanloom-site"] == c.branch and "10.44.7." in r.text and not found(r.text, HOME_MARKERS)


def test_a_trailing_slash_never_redirects_into_another_answer(two_sites):
    c = two_sites
    sign_in(c, "admin")
    for path in ("/api/devices/", "/api/map/", "/api/reports/", "/api/state/"):
        r = c.get(path, headers=dict(HDR, **{sites_mod.HEADER: c.branch}), follow_redirects=False)
        assert r.status_code == 404 and "location" not in r.headers, (path, r.status_code)


def test_an_empty_or_broken_site_list_never_releases_every_site(two_sites):
    assert sites_mod.parse_user_sites("") == []
    assert sites_mod.parse_user_sites(None) == []
    assert sites_mod.parse_user_sites("not json") == []
    assert sites_mod.parse_user_sites('{"a": 1}') == []
    assert sites_mod.parse_user_sites("*") == sites_mod.ALL
    c = two_sites
    c.st.store.execute("UPDATE users SET sites='' WHERE id=?", (c.uids["home_viewer"],))
    sign_in(c, "home_viewer")
    r = c.get("/api/devices", headers=HDR)
    assert r.status_code == 403 and r.json()["error"] == "site.err.none"
    assert c.get("/api/sites", headers=HDR).json()["sites"] == []


# ---------------------------------------------------------------------------------------------------------- ways in
def test_an_external_report_lands_in_the_site_it_names_and_never_in_the_default_one(two_sites):
    c = two_sites
    st = c.st
    token = st.tokens.create({"id": c.uids["admin"], "role": "admin"}, "backup-job", "ingest")["token"]
    c.cookies.clear()
    auth = {"Authorization": "Bearer " + token}
    rep = {"source": "nightly-backup", "title": "Branch backup finished", "level": "warn"}
    # several sites and no site named: refused, never booked for the home site
    r = c.post("/api/v1/ingest", headers=auth, json=rep)
    assert r.status_code == 400 and r.json()["error"] == "site.err.required"
    # the field or the header names it
    r = c.post("/api/v1/ingest", headers=auth, json=dict(rep, site=c.branch))
    assert r.status_code == 200, r.text
    r = c.post("/api/v1/ingest", headers=dict(auth, **{sites_mod.HEADER: c.branch}), json=dict(rep, title="Second"))
    assert r.status_code == 200, r.text
    # header and field disagree, an unknown site, an empty one: refused
    assert c.post("/api/v1/ingest", headers=dict(auth, **{sites_mod.HEADER: c.branch}), json=dict(rep, site="home")).status_code == 409
    assert c.post("/api/v1/ingest", headers=auth, json=dict(rep, site="s-00000000")).status_code == 404
    assert c.post("/api/v1/ingest", headers=dict(auth, **{sites_mod.HEADER: ""}), json=rep).status_code == 404
    rows = st.store.query("SELECT site, title FROM api_ingest WHERE source='nightly-backup' ORDER BY id")
    assert [r["site"] for r in rows] == [c.branch, c.branch]
    # each site lists only its own reports, and only the branch's nightly report shows them
    branch_core = st.sites.core(c.branch)
    assert [r["title"] for r in branch_core.api_ingest.list(10)] == ["Second", "Branch backup finished"]
    assert all(r["title"] not in ("Second", "Branch backup finished") for r in st.real_core.api_ingest.list(50))
    sign_in(c, "admin")
    seen = c.get("/api/v1/ingest", headers=dict(HDR, **{sites_mod.HEADER: "home"})).json()["reports"]
    assert all(r["source"] != "nightly-backup" for r in seen)
    seen = c.get("/api/v1/ingest", headers=dict(HDR, **{sites_mod.HEADER: c.branch})).json()["reports"]
    assert [r["title"] for r in seen] == ["Second", "Branch backup finished"]


def test_an_external_report_with_a_single_site_still_needs_no_site(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from lanloom import config as config_mod
    from lanloom.main import create_app
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        st = app.state.ll
        uid = st.accounts.create("solo-admin", "a-long-test-password", role="admin")
        token = st.tokens.create({"id": uid, "role": "admin"}, "job", "ingest")["token"]
        r = c.post("/api/v1/ingest", headers={"Authorization": "Bearer " + token}, json={"source": "job", "title": "Done"})
        assert r.status_code == 200, r.text
        assert st.store.one("SELECT site FROM api_ingest WHERE source='job'")["site"] == "home"


def test_a_host_agent_reports_only_for_the_site_it_paired_with(two_sites):
    from lanloom import agents as agents_mod
    c = two_sites
    st = c.st
    sign_in(c, "admin")
    c.cookies.set(sites_mod.COOKIE, c.branch)
    code = c.post("/api/agents/invite", headers=dict(HDR, **{sites_mod.HEADER: c.branch}), json={"name": "branch-nas"}).json()["code"]
    c.cookies.clear()
    # a code of no site: refused (it never pairs into the default site)
    bad = c.post("/api/agent/pair", headers=HDR, json={"code": "ZZZZ-ZZZZ", "proto": agents_mod.PROTO, "version": "1", "host": {"name": "x"}})
    assert bad.status_code == 403
    r = c.post("/api/agent/pair", headers=dict(HDR, **{sites_mod.HEADER: "home"}),     # a header cannot move the pairing
               json={"code": code, "proto": agents_mod.PROTO, "version": "1", "host": {"name": "branch-nas", "ips": ["10.44.7.20"]}})
    assert r.status_code == 200, r.text
    aid, token = r.json()["agent_id"], r.json()["token"]
    assert st.sites.core(c.branch).store.one("SELECT id FROM agent_hosts WHERE id=?", (aid,))
    assert not st.store.one("SELECT id FROM agent_hosts WHERE id=?", (aid,))
    body = {"proto": agents_mod.PROTO, "version": "1", "reports": [{"t": time.time(), "host": {"name": "branch-nas", "ips": ["10.44.7.20"]}}]}
    auth = dict(HDR, Authorization="Bearer " + token)
    r = c.post("/api/agent/report", headers=dict(auth, **{sites_mod.HEADER: "home"}), json=body)
    assert r.status_code == 409 and r.json()["error"] == "site.err.agent_site"
    r = c.post("/api/agent/report", headers=dict(auth, **{sites_mod.HEADER: ""}), json=body)
    assert r.status_code == 409
    r = c.post("/api/agent/report", headers=auth, json=body)
    assert r.status_code == 200, r.text
    assert st.sites.core(c.branch).store.one("SELECT last_seen FROM agent_hosts WHERE id=?", (aid,))["last_seen"]


# ---------------------------------------------------------------------------------------------------------- links, live
def test_links_that_leave_the_app_name_the_site_they_are_about(two_sites):
    from lanloom.nightly.render import short_text
    c = two_sites
    st = c.st
    branch = st.sites.core(c.branch)
    channels = type("Channels", (), {"settings": lambda self: {"link": "https://lanloom.example"}})()
    branch.channel_reports = channels                         # the address the operator entered for links
    st.real_core.channel_reports = channels
    assert branch.notify.link("#/devices/d-1") == "https://lanloom.example/#/devices/d-1?site=" + c.branch
    assert branch.notify.link("#/settings?x=1") == "https://lanloom.example/#/settings?x=1&site=" + c.branch
    assert st.real_core.notify.link("#/devices/d-1") == "https://lanloom.example/#/devices/d-1"
    rep = {"day": "2026-11-20", "sections": [], "points": 0, "level": "ok", "site": {"id": c.branch, "name": "Branch office"}}
    assert short_text(rep, "en", None, "https://lanloom.example")["url"].endswith("#/reports/2026-11-20?site=" + c.branch)
    rep["site"] = {"id": "home", "name": ""}
    assert short_text(rep, "en", None, "https://lanloom.example")["url"].endswith("#/reports/2026-11-20")


def test_every_live_frame_names_its_site(two_sites):
    c = two_sites
    time.sleep(1.5)
    sign_in(c, "admin")
    for asked in ("home", c.branch):
        with c.websocket_connect("/api/live?site=" + asked, headers={"Origin": "http://testserver"}) as ws:
            frame = json.loads(ws.receive_text())
        assert frame["site"] == asked
    for core in c.st.sites.cores.values():
        assert core.tick(time.time())["site"] == core.site_id


def test_a_live_socket_that_names_an_unknown_site_is_closed_without_a_frame(two_sites):
    from starlette.websockets import WebSocketDisconnect
    c = two_sites
    sign_in(c, "admin")
    for asked in ("", "s-00000000", "*"):
        with pytest.raises(WebSocketDisconnect) as gone:
            with c.websocket_connect("/api/live?site=" + asked, headers={"Origin": "http://testserver"}) as ws:
                ws.receive_text()
        assert gone.value.code == sites_mod.WS_CHANGED, asked


def test_the_site_header_survives_a_reverse_proxy(two_sites):
    """Behind a proxy (forwarded headers, a base path) the answer still names its site and varies by header and cookie."""
    c = two_sites
    sign_in(c, "admin")
    hdr = dict(HDR, **{sites_mod.HEADER: c.branch, "X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https",
                       "X-Forwarded-Host": "lanloom.example"})
    r = c.get("/api/devices", headers=hdr)
    assert r.headers["x-lanloom-site"] == c.branch
    vary = [v.strip().lower() for v in r.headers["vary"].split(",")]
    assert "cookie" in vary and "x-lanloom-site" in vary and r.headers["cache-control"] == "no-store"


def test_new_routes_are_site_bound_unless_listed_as_instance_routes():
    """The rule for new views (docs/SITES.md): a route is answered from the request's site unless its prefix is in the
    short list of instance routes; the list holds no route that reads site data."""
    for prefix in sites_mod.GLOBAL_PREFIXES:
        assert not prefix.startswith(("/api/devices", "/api/map", "/api/reports", "/api/paths", "/api/overview")), prefix
    routes = [p for (m, p) in access.RULES if p.startswith("/api/") and m == "GET"]
    assert sum(1 for p in routes if sites_mod.scope_of(p) == "site") > 100
