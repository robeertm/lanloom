"""Every route of the app needs a session, except an explicit allow-list.

A new route that is not listed here and answers without a session turns this test red, so an endpoint can never
become public by accident."""
import re

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from lanloom import config as config_mod
from lanloom import main as main_mod
from lanloom.main import create_app

HDR = {"X-Lanloom": "1"}

# (method, route path) -> why it is reachable without a session
PUBLIC = {
    ("GET", "/api/health"): "container health check",
    ("GET", "/api/state"): "tells the page whether to show setup or sign-in",
    ("GET", "/api/i18n/{lang}"): "texts of the sign-in page",
    ("POST", "/api/setup/account"): "first account, needs the one-time setup code",
    ("POST", "/api/login"): "sign-in, rate limited",
    ("POST", "/api/passkey/login/options"): "passkey sign-in challenge",
    ("POST", "/api/passkey/login/verify"): "passkey sign-in, rate limited",
    ("GET", "/"): "the web page itself",
    ("GET", "/{path:path}"): "static files of the web page (never /api/)",
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        c.app_routes = app.routes
        c.the_app = app
        yield c


def http_routes(routes):
    for r in routes:
        if isinstance(r, APIRoute):
            for method in sorted(r.methods - {"HEAD", "OPTIONS"}):
                yield method, r.path


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x1", path)


def test_allow_list_matches_the_app(client):
    routes = set(http_routes(client.app_routes))
    stale = set(PUBLIC) - routes
    assert not stale, "allow-list names routes that no longer exist: %s" % sorted(stale)
    api_public = {p for (_, p) in PUBLIC if p.startswith("/api/") and "{" not in p}
    assert api_public == set(main_mod.PUBLIC_API)


def open_routes(client) -> list:
    """Routes outside the allow-list that answer without a session."""
    found = []
    for method, path in http_routes(client.app_routes):
        if (method, path) in PUBLIC:
            continue
        r = client.request(method, concrete(path), headers=HDR, json={})
        if r.status_code not in (401, 403):
            found.append((method, path, r.status_code))
    return found


def test_every_other_route_needs_a_session(client):
    assert len([1 for k in http_routes(client.app_routes) if k not in PUBLIC]) >= 20
    assert open_routes(client) == []


def test_the_guard_test_catches_a_new_open_route(client):
    @client.the_app.get("/export.csv")
    async def export():
        return {"rows": []}

    @client.the_app.get("/api/new-thing")
    async def new_thing():
        return {"ok": True}

    # routes are matched in order: move the new ones in front of the static catch-all
    routes = client.the_app.router.routes
    routes.insert(0, routes.pop())
    routes.insert(0, routes.pop())
    assert open_routes(client) == [("GET", "/export.csv", 200)]     # /api/new-thing is closed by the middleware


def test_websocket_routes_need_a_session(client):
    ws_routes = [r.path for r in client.app_routes if isinstance(r, APIWebSocketRoute)]
    assert ws_routes == ["/api/live"]
    for path in ws_routes:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(path, headers={"Origin": "http://testserver"}) as ws:
                ws.receive_text()
        assert exc.value.code == 4401


def test_static_catch_all_never_answers_api_paths(client):
    for path in ("/api/unknown", "/api/", "/api/map/../settings"):
        assert client.get(path).status_code in (401, 404), path
