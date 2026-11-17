"""The read half against a local fake of the XML API: what is sent, what is refused, error reasons, fallbacks."""
import asyncio
from urllib.parse import parse_qs
import xml.etree.ElementTree as ET

import pytest

from lanloom.firewall import neutral
from lanloom.integrations import capabilities
from lanloom.integrations.sophos import read
from lanloom.integrations.transport import Forbidden
from lanloom.sources._common import SourceError

from . import fixtures as fx
from .fake_fw import PASSWORD, USER, FakeFw


@pytest.fixture()
def fw():
    srv = FakeFw()
    yield srv
    srv.stop()


def reader(srv, **kw):
    return read.SophosReader(url=srv.url, username=kw.pop("username", USER), password=kw.pop("password", PASSWORD), **kw)


def query(get="<Zone></Zone>", login="<Login><Username>u</Username><Password>p</Password></Login>", root_attr=""):
    return "<Request%s>%s<Get>%s</Get></Request>" % (root_attr, login, get)


# ---------------------------------------------------------------------------- constructor
@pytest.mark.parametrize("kw", [{"url": ""}, {"url": None}, {"url": "ftp://x"}, {"url": "https://user:pw@x"},
                                {"url": "https://a b"}, {"password": ""}, {"password": None}, {"username": ""},
                                {"username": "a\nb"}])
def test_bad_settings_are_refused(kw):
    args = {"url": "https://192.0.2.1:4444", "username": USER, "password": PASSWORD}
    args.update(kw)
    with pytest.raises(SourceError) as e:
        read.SophosReader(**args)
    assert e.value.key == "source.family.config"


def test_repr_str_and_state_hide_the_password(fw):
    r = reader(fw)
    for text in (repr(r), str(r), repr(r._t), repr(vars(r))):
        assert PASSWORD not in text
    with pytest.raises(SourceError) as e:
        reader(fw, password="wrong").collect()
    assert "wrong" not in str(e.value) and "wrong" not in repr(e.value)


# ---------------------------------------------------------------------------- the full read
def test_full_poll_reads_the_state_in_one_request(fw):
    r = reader(fw)
    raw = r.collect()
    ev = r.config_event(raw)
    st = ev["state"]
    neutral.validate(st)
    assert len(fw.requests) == 1 and fw.requests[0]["asked"] == list(read.READ_ENTITIES)
    assert len(st["zones"]) == 5 and len(st["networks"]) == 5
    assert len({p["vendor_ref"] for p in st["policies"]}) == 8 and len(st["port_forwards"]) == 1
    assert st["controller"]["version"] == fx.API_VERSION


def test_config_event_shape_and_capabilities(fw):
    r = reader(fw)
    ev = r.config_event(r.collect())
    assert ev["type"] == "config" and ev["source"] == "family" and ev["family"] == "sophos" and isinstance(ev["t"], (int, float))
    caps = capabilities.validate(ev["capabilities"], "sophos")
    assert caps["write"] == {} and ev["capabilities"]["write"] == {}
    assert caps["api"]["version"] == fx.API_VERSION
    assert "port_forwards" in caps["read"] and r.capabilities()["write"] == {}


def test_refine_marks_port_forwards_missing_when_nat_is_empty(fw):
    fw.set_entity("NATRule", [fx.EMPTY_NAT])
    r = reader(fw)
    raw = r.collect()
    assert r.last_entity_status.get("NATRule") == "529"
    ev = r.config_event(raw)
    assert "port_forwards" not in ev["capabilities"]["read"] and "policies" in ev["capabilities"]["read"]
    assert ev["state"]["port_forwards"] == []


def test_poll_returns_one_config_event(fw):
    evs = asyncio.run(reader(fw).poll())
    assert [e["type"] for e in evs] == ["config"] and evs[0]["family"] == "sophos"


def test_async_test_returns_counts(fw):
    ok, key, details = asyncio.run(reader(fw).test())
    assert ok and key == "source.family.ok"
    assert details == {"version": fx.API_VERSION, "zones": 5, "networks": 5, "policies": 8, "port_forwards": 1}


def test_async_test_reports_the_reason(fw):
    ok, key, _ = asyncio.run(reader(fw, password="nope").test())
    assert not ok and key == "source.family.auth_failed"


def test_account_scope_is_unknown(fw):
    assert asyncio.run(reader(fw).account_scope()) == "unknown"


# ---------------------------------------------------------------------------- what goes on the wire
def test_every_request_is_a_post_form_with_only_reqxml(fw):
    r = reader(fw)
    r.collect()
    for req in fw.requests:
        assert req["method"] == "POST" and req["path"] == "/webconsole/APIController"
        assert req["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
        assert list(parse_qs(req["body"].decode(), keep_blank_values=True)) == ["reqxml"]
        assert read.is_read_query(req["reqxml"])
        root = ET.fromstring(req["reqxml"])
        assert [k.tag for k in root] == ["Login", "Get"]
        assert all(e.tag in read.READ_ENTITIES and len(e) == 0 for e in root.find("Get"))


def test_password_with_markup_characters_is_escaped_and_arrives_intact():
    pw = 'a&b<c>d"e\'f'
    srv = FakeFw(password=pw)
    try:
        r = reader(srv, password=pw)
        r.collect()
        assert srv.requests[0]["password"] == pw and srv.requests[0]["user"] == USER
        assert "<c>" not in srv.requests[0]["reqxml"] and read.is_read_query(srv.requests[0]["reqxml"])
    finally:
        srv.stop()


def test_user_name_with_markup_is_escaped():
    srv = FakeFw(user="a<b>&c")
    try:
        reader(srv, username="a<b>&c").collect()
        assert srv.requests[0]["user"] == "a<b>&c"
    finally:
        srv.stop()


# ---------------------------------------------------------------------------- is_read_query
def test_a_plain_query_passes():
    assert read.is_read_query(query()) is True
    assert read.is_read_query(query("<Zone></Zone><NATRule/>")) is True
    assert read.is_read_query(query(root_attr=' APIVersion="2000.2"')) is True


@pytest.mark.parametrize("doc", [
    query("<Set><Zone></Zone></Set>"),
    query("<Zone></Zone>").replace("<Get>", "<Set>").replace("</Get>", "</Set>"),
    query("<Remove></Remove>"),
    query("<Zone></Zone><Remove></Remove>"),
    query("<Zone><Name>x</Name></Zone>"),                                           # extra element inside entity
    query("<Zone/>").replace("</Request>", "<Set/></Request>"),                     # extra element after Get
    query('<Zone name="x"></Zone>'),                                                # attribute on entity
    query("<Zone>text</Zone>"),                                                     # text inside entity
    query("<Zone/><Zone/>"),                                                        # duplicate entity
    query("<Firmware/>"),                                                           # unknown entity
    query(""),                                                                      # nothing asked
    query(login=""),                                                                # missing Login
    query(login="<Login><Username>u</Username></Login>"),
    query(login="<Login><Password>p</Password><Username>u</Username></Login>"),
    query(login='<Login a="1"><Username>u</Username><Password>p</Password></Login>'),
    query(login="<Login><Username><x/></Username><Password>p</Password></Login>"),
    query(root_attr=' x="1"'),
    "<Request><Get><Zone/></Get></Request>",
    "<Other><Login><Username>u</Username><Password>p</Password></Login><Get><Zone/></Get></Other>",
    '<!DOCTYPE x [<!ENTITY a "b">]>' + query(),
    '<?xml version="1.0"?>' + query(),
    "<!-- c -->" + query(),
    "x" * (read.MAX_QUERY + 1),
    query("<Zone></Zone>" * 3 + "<Interface/>").replace("<Get>", "<Get>stray"),
    query() + "tail",
])
def test_is_read_query_rejects(doc):
    try:
        verdict = read.is_read_query(doc)
    except ET.ParseError:
        verdict = False                     # the transport wraps the check, so a parse error refuses too
    assert verdict is False
    assert read.ALLOW.rules and _refused_by_allowlist(doc)


def _refused_by_allowlist(doc) -> bool:
    try:
        read.ALLOW.check("POST", read.API_PATH, {"reqxml": doc})
    except Forbidden:
        return True
    return False


@pytest.mark.parametrize("doc", [None, 5, b"<Request/>", ["<Request/>"], {"a": 1}])
def test_is_read_query_rejects_non_strings(doc):
    assert read.is_read_query(doc) is False
    assert _refused_by_allowlist(doc)


def test_oversize_query_is_refused():
    big = query("<Zone></Zone>").replace("<Username>u", "<Username>" + "u" * read.MAX_QUERY)
    assert read.is_read_query(big) is False


# ---------------------------------------------------------------------------- the transport refuses
@pytest.mark.parametrize("method,body", [
    ("POST", {"reqxml": query("<Set><Zone/></Set>")}),                              # not a read query
    ("POST", {"reqxml": query(), "extra": "x"}),                                    # extra field
    ("POST", {}),                                                                   # missing field
    ("POST", {"username": "a", "password": "b"}),                                   # JSON-style login
    ("POST", "reqxml=" + query()),                                                  # not a dict
    ("POST", None),
    ("GET", None),
    ("PUT", {"reqxml": query()}),
    ("DELETE", None),
])
def test_transport_refuses_everything_but_a_read_query(fw, method, body):
    t = reader(fw)._t
    with pytest.raises(Forbidden):
        t.request(method, read.API_PATH, body)
    assert t.refused == 1 and t.sent == 0 and fw.requests == []


def test_transport_refuses_other_paths(fw):
    t = reader(fw)._t
    with pytest.raises(Forbidden):
        t.request("POST", "/webconsole/Other", {"reqxml": query()})
    with pytest.raises(Forbidden):
        t.request("GET", read.API_PATH)
    assert t.refused == 2 and fw.requests == []


def test_transport_lets_a_read_query_through_and_counts_it(fw):
    t = reader(fw)._t
    status, _, data = t.request("POST", read.API_PATH, {"reqxml": query("<Zone></Zone>").replace(">u<", ">%s<" % USER).replace(">p<", ">%s<" % PASSWORD)})
    assert status == 200 and b"<Zone" in data and t.sent == 1 and t.refused == 0


# ---------------------------------------------------------------------------- errors
def test_auth_failure_then_pause_without_a_request(fw):
    r = reader(fw, password="wrong")
    with pytest.raises(SourceError) as e:
        r.collect()
    assert e.value.key == "source.family.auth_failed" and len(fw.requests) == 1
    for _ in range(3):
        with pytest.raises(SourceError) as e:
            r.collect()
        assert e.value.key == "source.family.paused"
    assert len(fw.requests) == 1                            # nothing was sent while paused


def test_pause_ends_after_the_time(fw, monkeypatch):
    r = reader(fw, password="wrong")
    with pytest.raises(SourceError):
        r.collect()
    clock = {"t": read.time.monotonic()}
    monkeypatch.setattr(read.time, "monotonic", lambda: clock["t"] + read.PAUSE_S + 1)
    with pytest.raises(SourceError) as e:
        r.collect()
    assert e.value.key == "source.family.auth_failed" and len(fw.requests) == 2    # asked again after the pause


def test_pause_constant_is_a_quarter_hour():
    assert read.PAUSE_S == 900.0


def test_ip_refused(fw):
    fw.mode = "ip_refused"
    with pytest.raises(SourceError) as e:
        reader(fw).collect()
    assert e.value.key == "source.family.api_ip"


def test_api_off(fw):
    fw.mode = "api_off"
    with pytest.raises(SourceError) as e:
        reader(fw).collect()
    assert e.value.key == "source.family.api_off"


def test_http_500_is_a_protocol_error(fw):
    fw.mode, fw.http_status = "http", 500
    with pytest.raises(SourceError) as e:
        reader(fw).collect()
    assert e.value.key == "source.family.protocol"


@pytest.mark.parametrize("code", [401, 403])
def test_http_401_403_is_forbidden(fw, code):
    fw.mode, fw.http_status = "http", code
    with pytest.raises(SourceError) as e:
        reader(fw).collect()
    assert e.value.key == "source.family.forbidden"


def test_reply_that_is_not_xml_is_a_protocol_error(fw):
    fw.mode, fw.http_status = "http", 200               # the fake sends an HTML body with 200
    with pytest.raises(SourceError) as e:
        reader(fw).collect()
    assert e.value.key == "source.family.protocol"


def test_connection_refused_is_unreachable():
    srv = FakeFw()
    url = srv.url
    srv.stop()
    with pytest.raises(SourceError) as e:
        read.SophosReader(url=url, username=USER, password=PASSWORD, timeout=1).collect()
    assert e.value.key == "source.family.unreachable"


def test_account_that_may_read_nothing_is_forbidden(fw):
    for tag in list(fw.entities):
        fw.set_entity(tag, ['<%s><Status code="531">Not authorized</Status></%s>' % (tag, tag)])
    with pytest.raises(SourceError) as e:
        reader(fw).collect()
    assert e.value.key == "source.family.forbidden"


# ---------------------------------------------------------------------------- old firmware
def test_old_firmware_is_asked_one_by_one_once_then_combined_without_the_unknown_entity(fw):
    fw.mode = "old_firmware"
    r = reader(fw)
    raw = r.collect()
    asked = [req["asked"] for req in fw.requests]
    assert asked[0] == list(read.READ_ENTITIES)
    assert asked[1:] == [[e] for e in read.READ_ENTITIES]
    assert "NATRule" not in raw["entities"] and len(raw["entities"]["Zone"]) == 5 and len(raw["entities"]["FirewallRule"]) == 9
    assert r._unknown == {"NATRule"}
    n = len(fw.requests)
    raw2 = r.collect()
    assert len(fw.requests) == n + 1
    assert fw.requests[-1]["asked"] == [e for e in read.READ_ENTITIES if e != "NATRule"]
    assert len(raw2["entities"]["Zone"]) == 5
    st = r.config_event(raw2)["state"]
    neutral.validate(st)
    assert st["port_forwards"] == [] and "port_forwards" not in r.capabilities()["read"]


def test_wrong_password_during_fallback_stops_it(fw):
    fw.mode = "old_firmware"
    r = reader(fw, password="wrong")
    with pytest.raises(SourceError) as e:
        r.collect()
    assert e.value.key == "source.family.auth_failed" and len(fw.requests) == 1
