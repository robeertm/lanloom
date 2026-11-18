"""Add-on mode (H5): packaging, sign-in through the ingress proxy, refusal of everybody else, updater off.

The add-on folder (addon/lanloom) must stay in step with the release (version, image tag) and must not ask for more
than host networking; Lanloom must believe the ingress user header only from the ingress proxy's fixed address, allow
the hub's same-origin frame only in add-on mode, and never use its own updater there."""
import importlib.util
import json
import os
import re
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod
from lanloom.main import SECURITY_HEADERS, create_app

ROOT = Path(__file__).resolve().parent.parent
ADDON = ROOT / "addon" / "lanloom"
HDR = {"X-Lanloom": "1"}
INGRESS = (config_mod.ADDON_INGRESS_PROXY, 40000)
LAN_PEER = ("192.168.20.31", 40000)


def yaml_scalars(path: Path) -> dict:
    """Top-level keys of a flat add-on file (no YAML library in the image): key -> text, or a list for "- item" lines."""
    out, key = {}, None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            out[key] = value.strip('"') if value else []
        elif line.strip().startswith("- ") and isinstance(out.get(key), list):
            out[key].append(line.strip()[2:].strip())
    return out


def png_size(path: Path) -> tuple:
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", head[16:24])


def version() -> str:
    return (ROOT / "VERSION").read_text().strip()


# ------------------------------------------------------------------ packaging
def test_addon_version_and_image_follow_the_release():
    cfg = yaml_scalars(ADDON / "config.yaml")
    assert cfg["version"] == version(), "addon/lanloom/config.yaml version must equal VERSION"
    build = (ADDON / "build.yaml").read_text()
    tags = re.findall(r"^\s+(aarch64|amd64):\s*(\S+)\s*$", build, re.M)
    assert {a for a, _ in tags} == {"aarch64", "amd64"}
    for _, ref in tags:
        assert ref == "ghcr.io/robeertm/lanloom:%s" % version(), ref     # the released, signed image of the same version
    assert sorted(cfg["arch"]) == ["aarch64", "amd64"]


def test_addon_asks_only_for_host_networking_and_ingress():
    text = (ADDON / "config.yaml").read_text()
    cfg = yaml_scalars(ADDON / "config.yaml")
    assert cfg["slug"] == "lanloom" and cfg["host_network"] == "true" and cfg["ingress"] == "true"
    assert cfg["ingress_port"] == "7380" and cfg["panel_admin"] == "true"
    for key in ("privileged", "full_access", "devices", "usb", "uart", "gpio", "docker_api", "host_pid", "host_ipc",
                "host_dbus", "apparmor", "auth_api", "ports", "map", "kernel_modules", "video", "audio", "image",
                "hassio_api"):
        assert not re.search(r"^%s\s*:" % key, text, re.M), key
    assert cfg["services"] == ["mqtt:want"]


def test_addon_options_match_schema_and_translations():
    text = (ADDON / "config.yaml").read_text()
    opts = dict(re.findall(r"^  (\w+): (\S+)$", text.split("options:")[1].split("schema:")[0], re.M))
    schema = dict(re.findall(r"^  (\w+): (\S+)$", text.split("schema:")[1], re.M))
    assert set(opts) == set(schema) == {"ingress_role", "direct_access"}
    assert opts == {"ingress_role": "admin", "direct_access": "false"}
    assert schema["ingress_role"] == "list(viewer|operator|admin)" and schema["direct_access"] == "bool"
    for lang in ("en", "de"):
        tr = (ADDON / "translations" / ("%s.yaml" % lang)).read_text()
        for key in schema:
            assert re.search(r"^  %s:\n    name: .+\n    description: " % key, tr, re.M), (lang, key)


def test_addon_files_are_complete_and_neutral():
    for name in ("config.yaml", "build.yaml", "Dockerfile", "run.py", "DOCS.md", "README.md", "CHANGELOG.md",
                 "icon.png", "logo.png", "translations/en.yaml", "translations/de.yaml"):
        assert (ADDON / name).is_file(), name
    assert (ROOT / "addon" / "repository.yaml").is_file()
    docker = (ADDON / "Dockerfile").read_text()
    assert "ARG BUILD_FROM" in docker and "FROM ${BUILD_FROM}" in docker and 'CMD ["python", "/addon/run.py"]' in docker
    assert png_size(ADDON / "icon.png") == (128, 128)
    assert png_size(ADDON / "logo.png") == (250, 100)


def _run_module():
    spec = importlib.util.spec_from_file_location("addon_run", ADDON / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_start_script_maps_the_options(tmp_path):
    run = _run_module()
    p = tmp_path / "options.json"
    assert run.options(str(p)) == {"ingress_role": "admin", "direct_access": False}       # missing file: defaults
    p.write_text(json.dumps({"ingress_role": "Viewer", "direct_access": True}))
    assert run.options(str(p)) == {"ingress_role": "viewer", "direct_access": True}
    p.write_text(json.dumps({"ingress_role": "root", "direct_access": "yes"}))
    assert run.options(str(p)) == {"ingress_role": "admin", "direct_access": False}      # nothing outside the schema
    env = run.environment({"ingress_role": "operator", "direct_access": False}, {"PATH": "/usr/bin", "SUPERVISOR_TOKEN": "x"})
    assert env["LANLOOM_ADDON"] == "1" and env["LANLOOM_DATA"] == "/data/lanloom" and env["LANLOOM_PORT"] == "7380"
    assert env["LANLOOM_AUTH_DEFAULT_ROLE"] == "operator" and env["LANLOOM_ADDON_LAN"] == "0"
    assert env["LANLOOM_UPDATE_CHECK"] == "0" and env["SUPERVISOR_TOKEN"] == "x" and env["PATH"] == "/usr/bin"


def test_start_script_prepares_the_data_folder(tmp_path):
    run = _run_module()
    target = tmp_path / "lanloom"
    (target / "sub").mkdir(parents=True)
    (target / "sub" / "f").write_text("x")
    run.prepare(str(target), os.getuid(), os.getgid())
    assert target.is_dir() and oct(target.stat().st_mode & 0o777) == "0o750"
    assert (target / "sub" / "f").stat().st_uid == os.getuid()


# ------------------------------------------------------------------ configuration
def test_addon_config_trusts_only_the_ingress_proxy(monkeypatch):
    monkeypatch.setenv("LANLOOM_ADDON", "1")
    monkeypatch.setenv("LANLOOM_TRUSTED_PROXIES", "0.0.0.0/0")          # ignored in add-on mode
    monkeypatch.setenv("LANLOOM_AUTH_HEADER", "X-Evil")
    monkeypatch.setenv("LANLOOM_BASE_PATH", "/x")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "1")
    cfg = config_mod.load()
    assert cfg.addon and not cfg.addon_lan
    assert cfg.trusted_proxies == ("172.30.32.2",) and cfg.auth_header == "X-Remote-User-Name"
    assert cfg.auth_default_role == "admin" and cfg.update_check is False and cfg.base_path == ""
    monkeypatch.setenv("LANLOOM_AUTH_DEFAULT_ROLE", "viewer")
    monkeypatch.setenv("LANLOOM_ADDON_LAN", "1")
    cfg = config_mod.load()
    assert cfg.auth_default_role == "viewer" and cfg.addon_lan


def test_plain_install_is_unchanged(monkeypatch):
    monkeypatch.delenv("LANLOOM_ADDON", raising=False)
    monkeypatch.setenv("LANLOOM_ADDON_LAN", "1")
    cfg = config_mod.load()
    assert not cfg.addon and not cfg.addon_lan and cfg.trusted_proxies == () and cfg.auth_header == ""


# ------------------------------------------------------------------ the running app
def make_app(tmp_path, monkeypatch, lan=False):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_ADDON", "1")
    monkeypatch.setenv("LANLOOM_ADDON_LAN", "1" if lan else "0")
    monkeypatch.delenv("LANLOOM_AUTH_DEFAULT_ROLE", raising=False)
    return create_app(config_mod.load())


@pytest.fixture()
def addon_app(tmp_path, monkeypatch):
    return make_app(tmp_path, monkeypatch)


def ingress_client(app):
    return TestClient(app, base_url="http://lanloom.test", client=INGRESS)


def test_ingress_user_is_signed_in_and_created(addon_app):
    with ingress_client(addon_app) as c:
        r = c.get("/api/state", headers={"X-Remote-User-Name": "alex"})
        st = r.json()
        assert st["authenticated"] and st["user"] == "alex" and st["proxy"] and st["addon"] and not st["setup_needed"]
        assert st["can"]["admin"]
        assert c.get("/api/overview", headers={"X-Remote-User-Name": "alex"}).status_code == 200
        # without the header the ingress proxy is just a client without a session
        assert c.get("/api/overview").status_code == 401


def test_the_hub_may_frame_the_page_only_in_addon_mode(addon_app, tmp_path, monkeypatch):
    with ingress_client(addon_app) as c:
        r = c.get("/api/health")
        assert r.headers["x-frame-options"] == "SAMEORIGIN"
        assert "frame-ancestors 'self'" in r.headers["content-security-policy"]
        assert "frame-ancestors 'none'" not in r.headers["content-security-policy"]
        index = c.get("/")
        assert index.status_code == 200 and index.headers["x-frame-options"] == "SAMEORIGIN"
    assert SECURITY_HEADERS["X-Frame-Options"] == "DENY" and "frame-ancestors 'none'" in SECURITY_HEADERS["Content-Security-Policy"]
    monkeypatch.delenv("LANLOOM_ADDON")
    plain = create_app(config_mod.load())
    with TestClient(plain, base_url="http://lanloom.test") as c:
        assert c.get("/api/health").headers["x-frame-options"] == "DENY"


def test_everybody_else_is_refused_without_direct_access(addon_app):
    with TestClient(addon_app, base_url="http://lanloom.test", client=LAN_PEER) as c:
        for path in ("/", "/api/health", "/api/state", "/js/app.js"):
            r = c.get(path, headers={"X-Remote-User-Name": "alex"})
            assert r.status_code == 403 and b"side panel" in r.content, path
        with pytest.raises(Exception):
            with c.websocket_connect("/api/live"):
                pass
    with TestClient(addon_app, base_url="http://lanloom.test", client=("127.0.0.1", 40000)) as c:
        assert c.get("/api/health").status_code == 200                       # the image's own health check


def test_direct_access_answers_the_network_but_never_believes_the_header(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, lan=True)
    with TestClient(app, base_url="http://lanloom.test", client=LAN_PEER) as c:
        assert c.get("/api/health").status_code == 200
        st = c.get("/api/state", headers={"X-Remote-User-Name": "alex"}).json()
        assert not st["authenticated"]
        assert c.get("/api/overview", headers={"X-Remote-User-Name": "alex"}).status_code == 401


def test_origin_of_the_hub_is_accepted_from_the_ingress_proxy_only(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, lan=True)
    who = {"X-Remote-User-Name": "alex", "Origin": "http://hub.example:8123", "X-Forwarded-Host": "hub.example:8123"}
    with ingress_client(app) as c:
        r = c.post("/api/update/check", headers=dict(HDR, **who), json={})
        assert r.status_code == 200, r.text
        bad = dict(who, Origin="http://evil.example")
        assert c.post("/api/update/check", headers=dict(HDR, **bad), json={}).json()["error"] == "auth.err.origin"
    with TestClient(app, base_url="http://lanloom.test", client=LAN_PEER) as c:
        r = c.post("/api/login", json={"username": "a", "password": "b"},
                   headers=dict(HDR, Origin="http://hub.example:8123", **{"X-Forwarded-Host": "hub.example:8123"}))
        assert r.status_code == 403 and r.json()["error"] == "auth.err.origin"


def test_updater_is_off_and_never_asks_the_feed(addon_app):
    from lanloom import updates
    called = []
    orig = updates.Updates._fetch
    updates.Updates._fetch = lambda self: called.append(1) or {}
    try:
        with ingress_client(addon_app) as c:
            who = {"X-Remote-User-Name": "alex"}
            u = c.get("/api/update", headers=who).json()
            assert u["mode"] == "addon" and u["enabled"] is False and u["available"] is False and u["sidecar"] is False
            u = c.post("/api/update/check", headers=dict(HDR, **who), json={}).json()
            assert u["mode"] == "addon"
            for path, body in (("/api/update/apply", {"version": "9.9.9"}), ("/api/update/rollback", {"version": "1.0.0"})):
                r = c.post(path, headers=dict(HDR, **who), json=body)
                assert r.status_code == 400 and r.json()["error"] == "update.err.addon", path
            r = c.put("/api/update/settings", headers=dict(HDR, **who), json={"channel": "preview"})
            assert r.json()["error"] == "update.err.addon"
    finally:
        updates.Updates._fetch = orig
    assert not called


def test_update_event_is_not_offered_in_addon_mode(addon_app):
    from lanloom.notify import events
    ev = events.get("update_available")
    assert ev.available(addon_app.state.ll.core) is False


def test_data_stays_in_the_configured_folder(addon_app, tmp_path):
    with ingress_client(addon_app) as c:
        c.get("/api/state", headers={"X-Remote-User-Name": "alex"})
    assert any(tmp_path.iterdir())
    assert addon_app.state.ll.cfg.data_dir == tmp_path


def test_start_script_drops_privileges_before_lanloom_starts():
    import ast
    tree = ast.parse((ADDON / "run.py").read_text(encoding="utf-8"))
    funcs = {f.name: f for f in tree.body if isinstance(f, ast.FunctionDef)}
    calls = lambda f: [ast.unparse(c.func) for c in sorted((n for n in ast.walk(f) if isinstance(n, ast.Call)),   # noqa: E731
                                                          key=lambda n: (n.lineno, n.col_offset))]
    assert [c for c in calls(funcs["drop"]) if c.startswith("os.set")] == ["os.setgroups", "os.setgid", "os.setuid"]
    main = calls(funcs["main"])
    assert main.index("drop") < main.index("os.execvpe") and main.index("prepare") < main.index("drop")
