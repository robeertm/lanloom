"""Integration foundation: registry (built-in only), manifests, credentials kept apart, the one send path with its
allow-list, and static checks (no plugin loading, the app never imports a write half, read halves never write).
Nothing here leaves the machine; the HTTP tests talk to a server on 127.0.0.1 started by the test."""
import ast
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod
from lanloom import integrations
from lanloom.firewall import adapter
from lanloom.integrations import capabilities as capmod
from lanloom.integrations import credentials, transport
from lanloom.main import create_app

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "lanloom"
HDR = {"X-Lanloom": "1"}
SIM_WRITE = {"move": "B", "client.set.name": "B", "client.set.fixed_ip": "B", "client.set.network": "B",
             "guest.nic": "B", "guest.fw_rule": "B", "guest.fw_rule_create": "B", "guest.fw_rule_delete": "B",
             "guest.firewall": "B*", "guest.power": "B*"}     # the sample family: clients and the sample hypervisor's guests


def sign_in(c, data_dir):
    code = (Path(data_dir) / "setup-code.txt").read_text().strip()
    r = c.post("/api/setup/account", json={"code": code, "username": "admin", "password": "a-long-test-password"}, headers=HDR)
    assert r.status_code == 200


# ---------------------------------------------------------------------------- registry and manifests
def test_registry_holds_only_built_in_families():
    shipped = sorted(p.name for p in (PKG / "integrations").iterdir()
                     if p.is_dir() and (p / "__init__.py").is_file() and not p.name.startswith("_"))
    fams = [m.family for m in integrations.BUILTIN]
    assert fams[0] == "simulation" and sorted(fams) == shipped and fams[1:] == sorted(fams[1:])
    for m in integrations.BUILTIN:
        assert m.validate() is m
        for spec in (m.read, m.write):
            assert not spec or spec.startswith("lanloom.integrations.%s." % m.family)
        assert (PKG / "integrations" / m.family / "__init__.py").is_file()
        if m.selectable:                               # every offered family has a guide and a complete form
            assert m.docs and (ROOT / m.docs).is_file()
            assert {f["k"] for f in m.form} == set(m.config_fields) | set(m.secret_fields)
    offered = [f["family"] for f in integrations.public_list()]
    assert "simulation" not in offered                 # the sample family is not offered in the dialog
    assert offered == [m.family for m in integrations.BUILTIN if m.selectable]


def test_registry_loads_only_packages_of_its_own_folder(monkeypatch, tmp_path):
    """The family list comes from this package's own folder: a module path from elsewhere is never searched."""
    import pkgutil
    seen = []
    real = pkgutil.iter_modules

    def spy(path=None, prefix=""):
        seen.append(list(path or []))
        return real(path, prefix)

    monkeypatch.setattr(pkgutil, "iter_modules", spy)
    monkeypatch.syspath_prepend(str(tmp_path))
    assert [m.family for m in integrations._builtin()] == [m.family for m in integrations.BUILTIN]
    assert seen == [[str(PKG / "integrations")]]


@pytest.mark.parametrize("change", [
    {"family": "Bad-Name"}, {"label": ""}, {"api_level": 99},
    {"read": "os.path:join"}, {"write": "lanloom.integrations.other.write:X"},
    {"read": "lanloom.integrations.simulation.write:SampleWriter"},       # a write module as read half
    {"docs": "README.md"}, {"capabilities": {"family": "simulation", "api": {"kind": "cloud"}}},
    {"secret_fields": ("token",), "write_secret_fields": ("token",)},
])
def test_manifest_validation(change):
    base = integrations.get("simulation")
    fields = {k: getattr(base, k) for k in base.__dataclass_fields__}
    fields.update(change)
    with pytest.raises((integrations.ManifestError, capmod.CapabilityError)):
        integrations.IntegrationManifest(**fields).validate()


def test_read_half_loads_in_the_app_write_half_only_in_the_executor(monkeypatch):
    reader = integrations.load_read("simulation")
    assert reader.read()["devices"] and reader.capabilities()["write"] == SIM_WRITE
    with pytest.raises(PermissionError):
        integrations.load_write("simulation", settle_s=0)
    monkeypatch.setitem(integrations._EXECUTOR, "on", True)
    w = integrations.load_write("simulation", settle_s=0)
    assert isinstance(w, adapter.ControllerAdapter) and w.family == "simulation"
    with pytest.raises(KeyError):
        integrations.load_write("nosuchfamily")


# ---------------------------------------------------------------------------- static checks
def _imports(path: Path) -> set:
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * node.level) + (node.module or "")
            out.add(mod)
            out.update(mod + "." + a.name for a in node.names)
    return out


def test_no_plugin_mechanism():
    """Dynamic imports exist only in the registry, which loads specs from validated built-in manifests."""
    for path in PKG.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(ROOT).as_posix()
        for word in ("entry_points", "__import__(", "exec(", "eval(", "pkg_resources", "spec_from_file_location"):
            assert word not in text, (rel, word)
        if "importlib" in text:
            assert rel == "lanloom/integrations/__init__.py", rel
    src = (PKG / "executor.py").read_text(encoding="utf-8")
    assert "load_adapter" not in src and "import_module" not in src


def test_app_never_imports_a_write_half():
    allowed = {"lanloom/executor.py", "lanloom/integrations/__init__.py"}
    for path in PKG.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel in allowed or (path.parent.parent.name == "integrations" and path.name == "write.py"):
            continue
        for mod in _imports(path):
            assert not mod.endswith(".write") and ".write." not in mod, (rel, mod)
        assert "load_write(" not in path.read_text(encoding="utf-8") or rel in allowed, rel


NETWORK_MODULES = ("socket", "http", "urllib", "ssl", "requests", "httpx", "aiohttp", "asyncio.open_connection", "websocket")


def test_read_halves_and_translation_modules_reach_the_network_only_through_the_transport():
    for fam in (PKG / "integrations").iterdir():
        if not fam.is_dir() or fam.name.startswith("_"):
            continue
        for name in ("read.py", "model.py", "__init__.py"):
            path = fam / name
            if not path.is_file():
                continue
            mods = _imports(path)
            for mod in mods:
                root = mod.lstrip(".").split(".")[0]
                assert root not in NETWORK_MODULES, (path, mod)
                assert not mod.endswith(".write") and not mod.endswith("write"), (path, mod)
            text = path.read_text(encoding="utf-8")
            assert "write_allowlist" not in text, path        # a read half builds read lists only


def test_transport_is_the_only_raw_send_site_in_integrations():
    for path in (PKG / "integrations").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name == "transport.py":
            continue
        assert "http.client" not in text and "socket" not in text, path


# ---------------------------------------------------------------------------- capabilities through the app
def test_capability_keys_cover_known_step_kinds_only():
    caps = capmod.validate(integrations.get("simulation").capabilities, "simulation")
    assert capmod.can(caps, "move") and not capmod.can(caps, "port.set")
    assert capmod.stage_for(caps, {"kind": "move", "device": "d1", "before": {"network": "a", "ip": ""},
                                   "after": {"network": "b", "ip": "192.0.2.5"}}) == "B"


# ---------------------------------------------------------------------------- credentials
def test_secret_never_shows_its_value():
    s = credentials.Secret("hunter2-very-secret")
    assert "hunter2" not in repr(s) and "hunter2" not in str(s) and "hunter2" not in "%s %r {}".format(s) % (s, s)
    assert "hunter2" not in f"{s}" and s.reveal() == "hunter2-very-secret"
    with pytest.raises(TypeError):
        json.dumps({"x": s})
    with pytest.raises(TypeError):
        import pickle
        pickle.dumps(s)
    with pytest.raises(AttributeError):
        s._value = "x"
    try:
        raise ValueError("login failed for %r" % s)
    except ValueError as exc:
        assert "hunter2" not in str(exc)


def _options(tmp_path, data, mode=0o600, name="options.json"):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    os.chmod(p, mode)
    return p


def test_write_options_file_rules(tmp_path):
    p = _options(tmp_path, {"url": "https://192.0.2.1", "api_key": "k-123", "site": "default", "pin": "77"})
    opts = credentials.load_write_options(p, secret_fields=("pin",))
    assert isinstance(opts["api_key"], credentials.Secret) and isinstance(opts["pin"], credentials.Secret)
    assert opts["url"] == "https://192.0.2.1" and opts["api_key"].reveal() == "k-123"
    with pytest.raises(credentials.CredentialError, match="0600"):
        credentials.load_write_options(_options(tmp_path, {"a": 1}, mode=0o640, name="o2.json"))
    link = tmp_path / "link.json"
    link.symlink_to(p)
    with pytest.raises(credentials.CredentialError, match="symbolic link"):
        credentials.load_write_options(link)
    with pytest.raises(credentials.CredentialError, match="belong"):
        credentials.load_write_options(p, uid=os.getuid() + 1)
    with pytest.raises(credentials.CredentialError):
        credentials.load_write_options(_options(tmp_path, [1, 2], name="o3.json"))
    (tmp_path / "o4.json").write_text("{nope")
    os.chmod(tmp_path / "o4.json", 0o600)
    with pytest.raises(credentials.CredentialError, match="JSON"):
        credentials.load_write_options(tmp_path / "o4.json")


def test_vet_write_account():
    assert credentials.vet_write_account({"principal": "root@pam"}) == (False, ["superuser"])
    assert credentials.vet_write_account({"principal": "root"})[0] is False
    ok, warn = credentials.vet_write_account({"principal": "lanloom@pve!plan", "role": "Owner", "token": True})
    assert ok and warn == ["owner_role", "no_expiry"]
    assert credentials.vet_write_account({"principal": "rooted-user", "role": "admin"}) == (True, [])


def test_executor_options_file_must_be_private(tmp_path, monkeypatch):
    from lanloom import executor as ex
    monkeypatch.setitem(integrations._EXECUTOR, "on", True)
    fake = integrations.IntegrationManifest(family="fake", label="Fake", capabilities={"family": "fake", "api": {"kind": "local"}},
                                            write="lanloom.integrations.simulation.write:SampleWriter")
    monkeypatch.setattr(integrations, "get", lambda fam: fake if fam == "fake" else (_ for _ in ()).throw(KeyError(fam)))
    monkeypatch.setattr(integrations, "_load", lambda spec: adapter.SimulatedController)
    (tmp_path / "armed").write_text("")
    opt = _options(tmp_path, {"settle_s": 0}, mode=0o644)
    env = {"LANLOOM_EXECUTOR_STATE": str(tmp_path), "LANLOOM_EXECUTOR_TARGET": "controller", "LANLOOM_EXECUTOR_ARM": "I-UNDERSTAND",
           "LANLOOM_EXECUTOR_ADAPTER": "fake", "LANLOOM_EXECUTOR_ADAPTER_OPTIONS": str(opt)}
    with pytest.raises(SystemExit, match="0600"):
        ex.build_adapter(ex.Settings(env))
    os.chmod(opt, 0o600)
    a = ex.build_adapter(ex.Settings(env))
    info = ex.executor_info(ex.Settings(env), a)
    assert info["plan_version_max"] == 2 and info["api_level"] == integrations.INTEGRATION_API
    assert info["capabilities"]["write"] == SIM_WRITE


# ---------------------------------------------------------------------------- the one send path
class _Handler(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def _reply(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        _Handler.seen.append((self.command, self.path, body, self.headers.get("X-API-Key")))
        out = json.dumps({"data": [{"id": "n1", "name": "Main"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_PUT = do_DELETE = _reply


@pytest.fixture()
def server():
    _Handler.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def test_read_transport_sends_only_allowed_gets(server):
    allow = transport.read_allowlist([r"/api/v1/networks", r"/api/v1/clients/[a-z0-9]{1,16}"],
                                     login=("/api/login", ("username", "password")), logout="/api/logout")
    key = credentials.Secret("abc-key")
    t = transport.Transport(server, allow, auth_headers=lambda: {"X-API-Key": key.reveal()})
    status, data = t.json("GET", "/api/v1/networks")
    assert status == 200 and data["data"][0]["id"] == "n1"
    assert "abc-key" not in repr(t) and not t.writes
    for method, path, body in (("PUT", "/api/v1/networks", {"name": "x"}), ("GET", "/api/v1/networks/../admin", None),
                               ("GET", "/api/v1/networks", {"x": 1}), ("POST", "/api/login", {"username": "a"}),
                               ("POST", "/api/login", {"username": "a", "password": "b", "remember": True}),
                               ("DELETE", "/api/v1/clients/abc", None), ("POST", "/api/logout", {"x": 1})):
        with pytest.raises(transport.Forbidden):
            t.request(method, path, body)
    assert t.request("POST", "/api/login", {"username": "a", "password": "b"})[0] == 200
    assert t.refused == 7 and t.sent == 2
    assert [s[:2] for s in _Handler.seen] == [("GET", "/api/v1/networks"), ("POST", "/api/login")]
    assert _Handler.seen[0][3] == "abc-key"


def test_read_allowlist_cannot_hold_writes():
    with pytest.raises(ValueError):
        transport.AllowList([transport.Rule("PUT", "/x", fields=("a",))], write=False)
    with pytest.raises(ValueError):
        transport.AllowList([transport.Rule("POST", "/x", fields=("a",))], write=False)
    with pytest.raises(ValueError):
        transport.write_allowlist([transport.Rule("PUT", "/x")])        # a write rule needs its body fields
    with pytest.raises(ValueError):
        transport.Rule("PUT", "/x", fields=("a",), values={"b": int})


def test_write_transport_checks_fields_and_values(server):
    def vlan(v):
        return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 4094
    allow = transport.write_allowlist([transport.Rule("PUT", r"/api/v1/ports/[a-z0-9]{1,8}", fields=("native_vlan", "name"),
                                                      values={"native_vlan": vlan})])
    t = transport.Transport(server, allow)
    assert t.writes
    assert t.request("PUT", "/api/v1/ports/p7", {"native_vlan": 30})[0] == 200
    for body in ({"native_vlan": 5000}, {"native_vlan": 30, "poe": False}, {"native_vlan": True}, None):
        with pytest.raises(transport.Forbidden):
            t.request("PUT", "/api/v1/ports/p7", body)
    assert json.loads(_Handler.seen[-1][2]) == {"native_vlan": 30} and len(_Handler.seen) == 1


def test_transport_rejects_urls_with_credentials():
    allow = transport.read_allowlist([r"/x"])
    for url in ("ftp://a", "https://user:pw@192.0.2.1", "nohost"):  # secret-scan: fake
        with pytest.raises(transport.TransportError):
            transport.Transport(url, allow)


def test_unreachable_device_is_a_reason_key():
    t = transport.Transport("http://127.0.0.1:9", transport.read_allowlist([r"/x"]), timeout=1)
    with pytest.raises(transport.TransportError) as e:
        t.request("GET", "/x")
    assert e.value.key == "integration.err.unreachable"


# ---------------------------------------------------------------------------- built-in families in the app
class FakeReader:
    scope = "can_write"

    def __init__(self, url, token=None):
        self.url, self.token = url, token

    async def account_scope(self):
        return FakeReader.scope

    async def test(self):
        return True, "source.fake.ok", {}

    async def run(self, emit):
        return None


@pytest.fixture()
def fake_family(monkeypatch):
    fake = integrations.IntegrationManifest(family="fakefam", label="Fake family (local API)",
                                            capabilities={"family": "fakefam", "api": {"kind": "local"}, "read": ["clients"]},
                                            read="lanloom.integrations.fakefam.read:FakeReader",
                                            config_fields=("url",), secret_fields=("token",)).validate()
    real_get = integrations.get
    monkeypatch.setattr(integrations, "BUILTIN", integrations.BUILTIN + (fake,))
    monkeypatch.setattr(integrations, "get", lambda fam: fake if fam == "fakefam" else real_get(fam))
    monkeypatch.setattr(integrations, "_load", lambda spec: FakeReader if spec == fake.read else integrations._load(spec))
    return fake


def test_app_refuses_a_read_account_that_can_write(tmp_path, monkeypatch, fake_family):
    import asyncio
    from lanloom import core
    from lanloom.store import Store
    c = core.Core(config_mod.load(), Store(":memory:"), None)
    cfg, sec = c.clean_integration("fakefam", {"url": "https://192.0.2.10/", "junk": 1}, {"token": "t-1"})
    assert cfg == {"url": "https://192.0.2.10"} and sec == {"token": "t-1"}
    with pytest.raises(ValueError):
        c.clean_integration("fakefam", {"url": "https://u:p@192.0.2.10"}, {})  # secret-scan: fake
    with pytest.raises(ValueError):
        c.clean_integration("fakefam", {"url": "https://192.0.2.10/" + "x" * 300}, {})
    src = c._build_source("fakefam", cfg, sec)
    assert isinstance(src.token, credentials.Secret) and src.token.reveal() == "t-1"
    res = asyncio.run(c.test_integration("fakefam", {"url": "https://192.0.2.10"}, {"token": "t"}))
    assert res == {"ok": False, "key": "integration.err.can_write", "details": {}}
    FakeReader.scope = "read_only"
    assert asyncio.run(c.test_integration("fakefam", {"url": "https://192.0.2.10"}, {"token": "t"}))["ok"]
    FakeReader.scope = "can_write"


def test_api_lists_selectable_families_with_capabilities(tmp_path, monkeypatch, fake_family):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    with TestClient(create_app(config_mod.load()), base_url="http://lanloom.test") as c:
        sign_in(c, tmp_path)
        data = c.get("/api/integrations").json()
        fams = data["families"]
        fake = [f for f in fams if f["family"] == "fakefam"]
        assert fake and fake[0]["capabilities"]["read"] == ["clients"]
        assert fake[0]["secret_fields"] == ["token"] and data["api_level"] == integrations.INTEGRATION_API
        assert "simulation" not in [f["family"] for f in fams]


# ---------------------------------------------------------------------------- an executor of an earlier or later release
def test_executor_without_move_makes_the_editor_read_only(tmp_path, monkeypatch):
    token = "t" * 40
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    monkeypatch.setenv("LANLOOM_FW_EXECUTOR", "external")
    monkeypatch.setenv("LANLOOM_EXECUTOR_TOKEN", token)
    auth = dict(HDR, Authorization="Bearer " + token)
    with TestClient(create_app(config_mod.load()), base_url="http://lanloom.test") as c:
        sign_in(c, tmp_path)
        caps = {"family": "fakefam", "api": {"kind": "local", "version": "1"}, "read": ["clients"], "write": {}}
        r = c.post("/api/executor/state", json={"state": adapter.sample_state(), "executor": {
            "target": "controller", "armed": False, "version": "1.2.0", "capabilities": caps, "plan_version_max": 1,
            "api_level": 1}}, headers=auth)
        assert r.json()["app"]["plan_version_max"] == 2
        v = c.get("/api/firewall").json()
        assert v["capabilities"]["write"] == {} and v["executor"]["outdated"] and v["executor"]["plan_version_max"] == 1
        r = c.put("/api/firewall/draft", json={"moves": {"d19": "n_cams"}}, headers=HDR)
        assert r.status_code == 409 and r.json()["error"] == "fw.block.capability"
        # a state that breaks the neutral model is refused
        bad = adapter.sample_state()
        bad["ports"] = [{"id": "aa:bb:cc:dd:ee:ff"}]
        assert c.post("/api/executor/state", json={"state": bad, "executor": {}}, headers=auth).status_code == 400
