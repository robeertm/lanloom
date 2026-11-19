"""The soap rule and the digest answer of the transport (the foundation of this family), checked on their own."""
import hashlib

import pytest

from lanloom.integrations import transport
from lanloom.integrations.transport import AllowList, Forbidden, Rule, Transport, TransportError

from . import fixtures as fx
from .fake_box import FakeBox

SVC = "urn:example:service:S:1"


def good(v):
    return isinstance(v, str) and v.isdigit()


def allow(table=None, path="/ctl"):
    return transport.read_allowlist([], soap=[(path, table or {(SVC, "GetInfo"): {}, (SVC, "GetEntry"): {"NewIndex": good}})])


@pytest.fixture()
def box():
    srv = FakeBox()
    yield srv
    srv.stop()


def test_a_table_row_is_allowed_with_exactly_its_arguments():
    a = allow()
    assert a.check("POST", "/ctl", {"service": SVC, "action": "GetInfo", "args": {}})
    assert a.check("POST", "/ctl", {"service": SVC, "action": "GetEntry", "args": {"NewIndex": "3"}})


@pytest.mark.parametrize("body", [
    {"service": SVC, "action": "GetInfo", "args": {"NewIndex": "3"}},               # an argument the row does not have
    {"service": SVC, "action": "GetEntry", "args": {}},                             # an argument missing
    {"service": SVC, "action": "GetEntry", "args": {"NewIndex": "x"}},              # value check fails
    {"service": SVC, "action": "GetEntry", "args": {"NewIndex": 3}},                # not a string: the check says no
    {"service": SVC, "action": "GetEntry", "args": {"NewIndex": "3", "Other": "1"}},
    {"service": SVC, "action": "SetInfo", "args": {}}, {"service": "urn:other:1", "action": "GetInfo", "args": {}},
    {"service": SVC, "action": "GetInfo"}, {"service": SVC, "args": {}}, {"action": "GetInfo", "args": {}},
    {"service": SVC, "action": "GetInfo", "args": {}, "x": 1}, {"service": [SVC], "action": "GetInfo", "args": {}},
    {"service": SVC, "action": ["GetInfo"], "args": {}}, {"service": SVC, "action": "GetInfo", "args": None},
    None, {}, [], "x", b"x"])
def test_anything_that_is_not_a_row_is_refused(body):
    with pytest.raises(Forbidden):
        allow().check("POST", "/ctl", body)


def test_a_check_that_raises_refuses():
    a = allow({(SVC, "GetEntry"): {"NewIndex": lambda v: 1 / 0}})
    with pytest.raises(Forbidden):
        a.check("POST", "/ctl", {"service": SVC, "action": "GetEntry", "args": {"NewIndex": "1"}})


def test_a_check_must_return_exactly_true():
    a = allow({(SVC, "GetEntry"): {"NewIndex": lambda v: "yes"}})
    with pytest.raises(Forbidden):
        a.check("POST", "/ctl", {"service": SVC, "action": "GetEntry", "args": {"NewIndex": "1"}})


@pytest.mark.parametrize("action", ["SetInfo", "AddEntry", "DeleteEntry", "Reboot", "ForceTermination", "X_AVM-DE_SetHostName",
                                    "X_AVM-DE_WakeOnLAN", "get", "GetInfo; x", "Get Info", "", "X_AVM-DE_", "X_TOO_LONG_VENDOR-X_Get"])
def test_a_rule_cannot_be_built_for_an_action_that_is_not_a_get(action):
    with pytest.raises(ValueError):
        Rule("POST", "/ctl", soap={(SVC, action): {}})


@pytest.mark.parametrize("action", ["GetInfo", "GetStatusInfo", "X_AVM-DE_GetHostListPath", "GetGenericHostEntry"])
def test_vendor_get_actions_are_fine(action):
    Rule("POST", "/ctl", soap={(SVC, action): {}})


def test_a_soap_rule_needs_a_table_and_post_and_no_other_body_spec():
    for kw in ({"soap": {}}, ):
        with pytest.raises(ValueError):
            Rule("POST", "/ctl", **kw)
    with pytest.raises(ValueError):
        Rule("GET", "/ctl", soap={(SVC, "GetInfo"): {}})
    with pytest.raises(ValueError):
        Rule("POST", "/ctl", fields=("a",), soap={(SVC, "GetInfo"): {}})
    with pytest.raises(ValueError):
        Rule("POST", "/ctl", soap={(SVC, "GetInfo"): {"bad name": good}})
    with pytest.raises(ValueError):
        Rule("POST", "/ctl", soap={(SVC, "GetInfo"): {"NewIndex": "not callable"}})


def test_a_write_list_cannot_hold_a_soap_rule():
    with pytest.raises(ValueError):
        AllowList([Rule("POST", "/ctl", soap={(SVC, "GetInfo"): {}})], write=True)


def test_the_path_is_matched_whole():
    a = allow()
    for path in ("/ctl/x", "/ctlx", "/x/ctl", "/ctl?a=1", "/CTL", ""):
        with pytest.raises(Forbidden):
            a.check("POST", path, {"service": SVC, "action": "GetInfo", "args": {}})


def test_envelope_escapes_values_and_names_the_call():
    env = transport._soap_envelope(SVC, "GetEntry", {"NewIndex": "1<2&3"}).decode()
    assert "<NewIndex>1&lt;2&amp;3</NewIndex>" in env and '<u:GetEntry xmlns:u="%s">' % SVC in env and env.startswith("<?xml")


# ---------------------------------------------------------------------------- digest
def test_challenge_parsing():
    pc = transport._parse_challenge
    head = lambda v: [("WWW-Authenticate", v)]
    c = pc(head('Digest realm="F!Box SOAP-Auth", nonce="abc", algorithm=MD5, qop="auth"'))
    assert c == {"realm": "F!Box SOAP-Auth", "nonce": "abc", "opaque": "", "algorithm": "MD5", "qop": "auth"}
    assert pc(head('Digest realm="r", nonce="n"'))["qop"] == "" and pc(head('Digest realm="r", nonce="n"'))["algorithm"] == "MD5"
    assert pc(head('digest realm="r", nonce="n", algorithm=SHA-256, qop="auth,auth-int", opaque="o"'))["algorithm"] == "SHA-256"
    assert pc(head('Digest realm="", nonce="n"'))["realm"] == ""
    for bad in ('Basic realm="r"', 'Digest realm="r"', 'Digest nonce="n"', 'Digest realm="r", nonce="n", algorithm=SHA-512',
                'Digest realm="r", nonce="n", qop="auth-int"', "", "Digest"):
        assert pc(head(bad)) is None, bad
    assert pc([]) is None and pc(None) is None and pc([("Content-Type", "x")]) is None


def test_the_response_is_the_documented_digest(box):
    t = Transport(box.url, allow({("urn:dslforum-org:service:Hosts:1", "X_AVM-DE_GetHostListPath"): {}}, "/upnp/control/hosts"),
                  digest=lambda: (fx.USER, fx.PASSWORD))
    status, _, _ = t.request("POST", "/upnp/control/hosts", {"service": "urn:dslforum-org:service:Hosts:1",
                                                            "action": "X_AVM-DE_GetHostListPath", "args": {}})
    assert status == 200 and box.bad_attempts == 0 and t.challenges == 1


def test_the_nonce_counter_goes_up_and_a_replay_is_not_possible(box):
    t = Transport(box.url, allow({("urn:dslforum-org:service:Hosts:1", "X_AVM-DE_GetHostListPath"): {}}, "/upnp/control/hosts"),
                  digest=lambda: (fx.USER, fx.PASSWORD))
    body = {"service": "urn:dslforum-org:service:Hosts:1", "action": "X_AVM-DE_GetHostListPath", "args": {}}
    for _ in range(4):
        assert t.request("POST", "/upnp/control/hosts", body)[0] == 200
    assert box.bad_attempts == 0 and t.challenges == 1
    assert sorted(box.last_nc.values()) == [4]


def test_wrong_credentials_give_the_401_back_after_one_try_and_keep_nothing(box):
    t = Transport(box.url, allow({("urn:dslforum-org:service:Hosts:1", "X_AVM-DE_GetHostListPath"): {}}, "/upnp/control/hosts"),
                  digest=lambda: (fx.USER, "wrong"))
    body = {"service": "urn:dslforum-org:service:Hosts:1", "action": "X_AVM-DE_GetHostListPath", "args": {}}
    assert t.request("POST", "/upnp/control/hosts", body)[0] == 401
    assert box.bad_attempts == 1 and t.challenges == 1 and t._chal is None
    assert t.request("POST", "/upnp/control/hosts", body)[0] == 401
    assert box.bad_attempts == 2, "the transport alone does not pause; the reader does"


def test_without_a_digest_callable_nothing_is_answered(box):
    t = Transport(box.url, allow({("urn:dslforum-org:service:Hosts:1", "X_AVM-DE_GetHostListPath"): {}}, "/upnp/control/hosts"))
    body = {"service": "urn:dslforum-org:service:Hosts:1", "action": "X_AVM-DE_GetHostListPath", "args": {}}
    assert t.request("POST", "/upnp/control/hosts", body)[0] == 401
    assert all(r["auth"] == "" for r in box.requests) and box.bad_attempts == 0


def test_a_user_name_that_cannot_be_sent_is_refused_not_mangled(box):
    t = Transport(box.url, allow({("urn:dslforum-org:service:Hosts:1", "X_AVM-DE_GetHostListPath"): {}}, "/upnp/control/hosts"),
                  digest=lambda: ('a"b', "x"))
    with pytest.raises(TransportError):
        t.request("POST", "/upnp/control/hosts", {"service": "urn:dslforum-org:service:Hosts:1",
                                                 "action": "X_AVM-DE_GetHostListPath", "args": {}})


def test_the_credentials_are_not_in_repr_or_in_the_object(box):
    t = Transport(box.url, allow(), digest=lambda: (fx.USER, fx.PASSWORD))
    assert fx.PASSWORD not in repr(t) and fx.PASSWORD not in repr(vars(t))


def test_the_algorithm_follows_the_challenge_sha256_included():
    t = Transport("http://127.0.0.1:9", allow(), digest=lambda: ("u", "p"))
    chal = {"realm": "r", "nonce": "n", "opaque": "", "algorithm": "SHA-256", "qop": "auth"}
    head = t._digest_header("POST", "/ctl", chal, 1)
    got = dict(x.split("=", 1) for x in head[len("Digest "):].split(", "))
    h = lambda x: hashlib.sha256(x.encode()).hexdigest()
    want = h("%s:n:00000001:%s:auth:%s" % (h("u:r:p"), got["cnonce"].strip('"'), h("POST:/ctl")))
    assert got["response"].strip('"') == want and got["algorithm"] == "SHA-256" and got["nc"] == "00000001"
    no_qop = t._digest_header("POST", "/ctl", dict(chal, qop="", algorithm="MD5"), 1)
    assert "qop=" not in no_qop and "cnonce" not in no_qop
