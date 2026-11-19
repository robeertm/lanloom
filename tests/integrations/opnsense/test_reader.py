"""Read half of the OPNsense family against a local fake of the REST API: GET only, Basic authentication, areas a
release or a key does not have are left out, a refused key pauses, nothing secret is kept."""
import ast
import asyncio
import json
from pathlib import Path

import pytest

from lanloom.integrations import transport
from lanloom.integrations.opnsense import read
from lanloom.integrations.opnsense.read import OpnsenseReader
from lanloom.sources._common import SourceError

from . import fixtures as fx
from .fake_opn import FakeOpn


@pytest.fixture(params=["24.7", "26.7"])
def fake(request):
    f = FakeOpn(request.param)
    yield f
    f.close()


def reader(fake, **kw):
    return OpnsenseReader(url=fake.url, api_key=fx.API_KEY, api_secret=fx.API_SECRET, **kw)


def test_allow_list_holds_only_gets_of_the_named_paths():
    assert read.ALLOW.write is False
    assert {r.method for r in read.ALLOW.rules} == {"GET"} and len(read.ALLOW.rules) == len(read.PATHS)
    for method, path, body in (("POST", "/api/firewall/filter/apply", {}), ("POST", "/api/firewall/filter/addRule", {"rule": {}}),
                               ("GET", "/api/firewall/filter/get?x=1", None), ("GET", "/api/core/firmware/update", None),
                               ("GET", "/api/firewall/filter/getRule/" + fx.U["lan_any"], None),
                               ("DELETE", "/api/firewall/filter/get", None)):
        with pytest.raises(transport.Forbidden):
            read.ALLOW.check(method, path, body)
    for p in read.PATHS.values():
        read.ALLOW.check("GET", p)


def test_the_read_half_imports_no_network_module():
    src = Path(read.__file__).read_text()
    mods = {n.names[0].name.split(".")[0] for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Import)}
    mods |= {n.module.split(".")[0] for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom) and n.module and n.level == 0}
    assert not mods & {"socket", "http", "urllib", "ssl", "requests", "aiohttp"}


def test_collect_reads_everything_with_get_and_basic_auth(fake):
    r = reader(fake)
    ev = r.config_event(r.collect())
    assert {q["method"] for q in fake.requests} == {"GET"} and all(q["key"] == fx.API_KEY for q in fake.requests)
    st = ev["state"]
    assert ev["family"] == "opnsense" and ev["capabilities"]["write"] == {}
    assert st["controller"]["version"] == fake.version and len(st["zones"]) == 7 and st["policies"]
    text = json.dumps(ev)
    assert fx.WG_PRIVATE not in text and fx.PPPOE_SECRET not in text and fx.API_SECRET not in text
    if fake.version == "24.7":
        assert "port_forwards" not in ev["capabilities"]["read"]           # no destination NAT API on this release
        assert fake.requests and "/api/firewall/d_nat/searchRule" in [q["path"] for q in fake.requests]
    else:
        assert "port_forwards" in ev["capabilities"]["read"] and st["port_forwards"]


def test_a_missing_path_is_not_asked_again_for_an_hour(fake):
    r = reader(fake)
    r.collect()
    first = [q["path"] for q in fake.requests]
    fake.requests.clear()
    r.collect()
    again = [q["path"] for q in fake.requests]
    if fake.version == "24.7":
        assert "/api/firewall/d_nat/searchRule" in first and "/api/firewall/d_nat/searchRule" not in again
    assert "/api/firewall/filter/get" in again


def test_a_page_the_key_may_not_read_is_left_out(fake):
    fake.fail_next = {"/api/firewall/alias/get": 403, "/api/kea/": 403}
    r = reader(fake)
    st = r.config_event(r.collect())["state"]
    pol = next(p for p in st["policies"] if p["name"] == "Camera stream from outside")
    assert pol["ports"] == "cam_ports"                                   # alias not readable: named, not resolved
    assert not pol["editable"]


def test_a_refused_key_pauses_the_half(fake):
    r = OpnsenseReader(url=fake.url, api_key=fx.API_KEY, api_secret=fx.WRITE_SECRET)
    with pytest.raises(SourceError) as e:
        r.collect()
    assert e.value.key == "source.family.key_refused"
    n = len(fake.requests)
    with pytest.raises(SourceError) as e:
        r.collect()
    assert e.value.key == "source.family.paused" and len(fake.requests) == n


def test_a_key_without_any_page_is_forbidden(fake):
    fake.fail_next = {p: 403 for k, p in read.PATHS.items() if k not in ("system", "firmware")}
    with pytest.raises(SourceError) as e:
        reader(fake).collect()
    assert e.value.key == "source.family.forbidden"


def test_unreachable():
    r = OpnsenseReader(url="http://127.0.0.1:9", api_key=fx.API_KEY, api_secret=fx.API_SECRET, timeout=2)
    with pytest.raises(SourceError) as e:
        r.collect()
    assert e.value.key == "source.family.unreachable"


@pytest.mark.parametrize("kw", [{"url": "ftp://x"}, {"url": "https://user:pw@x"}, {"api_key": ""}, {"api_secret": ""},
                                {"api_key": "short"}])
def test_bad_settings_are_refused(kw):
    base = {"url": "https://192.0.2.1", "api_key": fx.API_KEY, "api_secret": fx.API_SECRET}
    base.update(kw)
    with pytest.raises(SourceError) as e:
        OpnsenseReader(**base)
    assert e.value.key == "source.family.config"


def test_repr_and_errors_never_show_the_secret(fake):
    r = reader(fake)
    assert fx.API_SECRET not in repr(r) and fx.API_SECRET not in str(r.__dict__)


def test_connection_test_and_account_scope(fake):
    ok, key, details = asyncio.run(reader(fake).test())
    assert ok and key == "source.family.ok" and details["version"] == fake.version and details["zones"] == 7
    assert asyncio.run(reader(fake).account_scope()) == "unknown"


def test_scrub_keeps_option_keys_and_drops_secret_fields():
    got = read.scrub({"privkey": "x", "rows": [{"pre_shared_key": "y", "name": "n"}],
                      "destination_net": {"private_nets": {"value": "private_nets", "selected": 1}}})
    assert got == {"rows": [{"name": "n"}], "destination_net": {"private_nets": {"value": "private_nets", "selected": 1}}}
