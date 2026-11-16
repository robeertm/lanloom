"""Privacy layer: placeholders out, real values back, nothing address-like survives."""
import random
import re
from types import SimpleNamespace

import pytest

from lanloom.ai import privacy
from lanloom.ai.privacy import Pseudonymizer, known_values, leaks

# independent of the module's own patterns
RX_IPV4 = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d])(?!\.\d)")
RX_MAC = re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f])|(?<![0-9a-f])[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}(?![0-9a-f])")
RX_IPV6 = re.compile(r"(?i)(?<![0-9a-z:])(?:[0-9a-f]{1,4}:){2,7}[0-9a-f]{0,4}(?![0-9a-z:])|(?<![0-9a-z:])::[0-9a-f:]+")


def roundtrip(text, known=None):
    ps = Pseudonymizer(known)
    out = ps.out(text)
    assert ps.back(out) == text
    return ps, out


@pytest.mark.parametrize("text,kind", [
    ("host 192.168.1.20 is up", "IP"),
    ("range 10.0.0.0/8 is private", "IP"),
    ("net 192.168.0.0/16", "IP"),
    ("v6 2001:db8::1 here", "IP"),
    ("v6 fe80::1ff:fe23:4567:890a%eth0 here", "IP"),
    ("full 2001:0db8:0000:0000:0000:ff00:0042:8329 end", "IP"),
    ("prefix 2001:db8:abcd::/48 end", "IP"),
    ("loop ::1 end", "IP"),
    ("mac aa:bb:cc:dd:ee:ff end", "MAC"),
    ("mac AA-BB-CC-DD-EE-FF end", "MAC"),
    ("mac aabb.ccdd.eeff end", "MAC"),
    ("name printer.local end", "HOST"),
    ("name nas.lan end", "HOST"),
    ("name a.b.home.arpa end", "HOST"),
    ("serial: ABCD12345 end", "SERIAL"),
    ("S/N 9X8Y7Z6W5 end", "SERIAL"),
    ("pos 12.345678, 23.456789 end", "PLACE"),
])
def test_patterns_roundtrip(text, kind):
    ps, out = roundtrip(text)
    assert out != text and "[%s-1]" % kind in out
    assert leaks(out) == []
    assert [m["kind"] for m in ps.mapping()] == [kind]


def test_same_value_same_placeholder():
    ps = Pseudonymizer()
    out = ps.out("a 192.168.1.2 b 192.168.1.3 c 192.168.1.2 d AA:BB:CC:DD:EE:FF e aa:bb:cc:dd:ee:ff")
    assert out == "a [IP-1] b [IP-2] c [IP-1] d [MAC-1] e [MAC-1]"
    # a second call of the same request keeps the numbering
    assert ps.out("again 192.168.1.3 and 10.9.9.9") == "again [IP-2] and [IP-3]"
    assert ps.back("[IP-1] and [IP-3]") == "192.168.1.2 and 10.9.9.9"


def test_back_leaves_unknown_placeholders_and_normal_text():
    ps = Pseudonymizer()
    ps.out("192.168.1.2")
    assert ps.back("see [IP-9] and [IP-1].") == "see [IP-9] and 192.168.1.2."
    assert ps.back("plain text, no placeholders") == "plain text, no placeholders"
    assert ps.back(None) == ""


def test_known_names_hidden_case_insensitive_longest_first():
    known = {"HOST": ["Attic Printer", "Attic", "Hall Camera"], "SSID": ["HomeNet-5G"], "SERIAL": ["SN-XYZ-0001"], "PLACE": ["Elm Street Office"]}
    text = "The attic printer and Hall Camera joined HomeNet-5G; unit SN-XYZ-0001 at Elm Street Office. The attic is cold."
    ps, out = roundtrip(text, known)
    for secret in ("rinter", "Camera", "HomeNet", "XYZ", "Elm"):
        assert secret not in out
    assert "[HOST-1] and [HOST-2] joined [SSID-1]" in out
    assert ps.back(out) == text
    # word boundaries: part of another word stays
    ps = Pseudonymizer({"HOST": ["Hall"]})
    assert ps.out("hallway and Hall") == "hallway and [HOST-1]"


def test_short_numeric_and_common_known_values_are_ignored():
    ps = Pseudonymizer({"HOST": ["ab", "12345", "router", "Gateway", "NAS", "default"], "BOGUS": ["secret-value"]})
    text = "router gateway nas default 12345 ab secret-value"
    assert ps.out(text) == text


def test_ordinary_words_and_numbers_stay():
    text = "The router at 12:30:45 runs version 1.2.3.4.5 and firmware 2.0, port 8080, 100 devices, v1.2.3, 2026-11-16."
    ps, out = roundtrip(text)
    assert out == text and leaks(out) == []


def test_four_part_number_roundtrips():
    ps, out = roundtrip("build 10.2.3.4 shipped")
    assert ps.back(out) == "build 10.2.3.4 shipped"


def test_ip_inside_url_and_with_port():
    ps, out = roundtrip("open http://192.168.1.5:8080/path?x=10.0.0.1 now")
    assert "192.168" not in out and "10.0.0.1" not in out and "[IP-1]:8080" in out


def test_ipv4_mapped_v6_hides_the_v4_part():
    ps, out = roundtrip("peer ::ffff:192.168.1.9 seen")
    assert "192.168.1.9" not in out


@pytest.mark.parametrize("text", [
    "ends with a mac aa:bb:cc:dd:ee:ff.", "mac aa:bb:cc:dd:ee:ff: joined", "mac ab-cd-ef-01-23-45.", "mac abcd.ef01.2345.",
    "address 2001:db8::1.", "address fe80::1%eth0.", "(2001:db8::1).", "ip 192.168.1.1.", "net 10.0.0.0/24.", "name nas.local.",
    "x.home.arpa."])
def test_sentence_final_identifiers_do_not_leak(text):
    ps, out = roundtrip(text)
    assert out != text and leaks(out) == []
    assert not (RX_IPV4.search(out) or RX_MAC.search(out) or RX_IPV6.search(out))


def _rand_ip4(r):
    return ".".join(str(r.randint(0, 255)) for _ in range(4))


def _rand_mac(r):
    parts = ["%02x" % r.randint(0, 255) for _ in range(6)]
    form = r.choice("colon dash dotted upper".split())
    if form == "colon":
        return ":".join(parts)
    if form == "dash":
        return "-".join(parts)
    if form == "upper":
        return ":".join(parts).upper()
    return "%s%s.%s%s.%s%s" % tuple(parts)


def _rand_ip6(r):
    form = r.randint(0, 5)
    g = lambda: "%x" % r.randint(1, 0xffff)       # noqa: E731
    if form == 0:
        return ":".join("%04x" % r.randint(0, 0xffff) for _ in range(8))
    if form == 1:
        return "%s::%s" % (g(), g())
    if form == 2:
        return "fe80::%s:%s:%s:%s%%eth%d" % (g(), g(), g(), g(), r.randint(0, 3))
    if form == 3:
        return "::%s" % g()
    if form == 4:
        return "%s:%s::" % (g(), g())
    return "%s:%s:%s::%s/%d" % (g(), g(), g(), g(), r.choice((48, 56, 64)))


SENTENCES = ["{x}", "{x}.", "{x},", "({x})", "[{x}]", "<{x}>", "at {x} now", "at {x}; then", "ip={x}", "\"{x}\"", "{x}:", "{x} and {y}",
             "first {x}\nsecond {y}\n", "- {x}\n- {y}.", "{x}, {y}, {x}.", "name: {x}.", "{x}\t{y}", "{x}) next"]


def test_random_texts_never_leak():
    r = random.Random(20261116)
    gens = [_rand_ip4, _rand_mac, _rand_ip6]
    for n in range(1500):
        text = " ".join(r.choice(SENTENCES).format(x=r.choice(gens)(r), y=r.choice(gens)(r)) for _ in range(r.randint(1, 4)))
        ps = Pseudonymizer()
        out = ps.out(text)
        assert ps.back(out) == text, text
        assert leaks(out) == [], (text, out)
        for rx in (RX_IPV4, RX_MAC, RX_IPV6):
            m = rx.search(out)
            assert m is None, (text, out, m.group(0))


def test_random_ipv4_with_noise_roundtrips():
    r = random.Random(7)
    words = ["router", "port", "up", "down", "1.2.3", "12:30:45", "v2.0", "alpha", "x=5", "-", "(", ")", "1.2.3.4.5"]
    for _ in range(500):
        parts = []
        for _ in range(r.randint(2, 10)):
            parts.append(r.choice(words) if r.random() < .6 else _rand_ip4(r))
        text = " ".join(parts)
        ps = Pseudonymizer()
        out = ps.out(text)
        assert ps.back(out) == text and not RX_IPV4.search(out), (text, out)


def test_known_values_leak_guard_and_fail_closed():
    assert leaks("clean text with [IP-1] and 12:30:45") == []
    assert leaks("raw 192.168.1.2") == ["IP"]
    assert leaks("raw aa:bb:cc:dd:ee:ff") == ["MAC"]
    assert leaks("raw fe80::1") == ["IP"]
    assert leaks("raw nas.local") == ["HOST"]
    assert leaks("192.168.1.2 aa:bb:cc:dd:ee:ff nas.lan") == ["HOST", "IP", "MAC"]


# ---------------------------------------------------------------------- what Lanloom knows
def _core():
    devices = [
        {"title": "Attic Printer", "auto_title": "HP-1234", "hostnames": ["attic-printer.lan", "attic"], "ip": "192.168.1.20",
         "mac": "aa:bb:cc:dd:ee:01", "mdns": [{"name": "Attic Printer._ipp._tcp", "host": "attic-printer.local.", "instance": "Printer 7"}, "junk"]},
        {"title": None, "hostnames": None, "ip": None},
    ]
    integrations = [{"name": "Main Controller", "config": {"site": "Elm Street"}}, {"name": "", "config": None}]
    live = [{"key": "k", "state": {"ssids": [{"name": "HomeNet"}, {"name": ""}, "x"], "devices": [{"serial": "SN123456", "name": "Switch Basement"}]}},
            {"state": {"wireless": [{"name": "GuestNet"}]}}, None]
    return SimpleNamespace(devices=lambda: devices, integrations=lambda: integrations, live_configs=lambda: live)


def test_known_values_from_core():
    kv = known_values(_core())
    assert {"Attic Printer", "HP-1234", "attic-printer.lan", "attic", "Printer 7", "Main Controller", "Switch Basement"} <= set(kv["HOST"])
    assert "attic-printer.local" in kv["HOST"]
    assert kv["IP"] == ["192.168.1.20"] and kv["MAC"] == ["aa:bb:cc:dd:ee:01"]
    assert kv["PLACE"] == ["Elm Street"] and kv["SERIAL"] == ["SN123456"]
    assert set(kv["SSID"]) == {"HomeNet", "GuestNet"}
    assert known_values(SimpleNamespace(devices=lambda: 1 / 0, integrations=lambda: 1 / 0)) == {}


def test_known_values_drive_the_replacement():
    text = "Device Attic Printer (attic-printer.lan) on HomeNet, unit SN123456, site Elm Street; GuestNet is open."
    ps, out = roundtrip(text, known_values(_core()))
    for secret in ("Printer", "attic", "HomeNet", "SN123456", "Elm", "GuestNet"):
        assert secret not in out
    assert leaks(out) == []


def test_add_known():
    k = privacy.add_known({"HOST": ["a"]}, "SSID", ["x", "", None])
    assert k == {"HOST": ["a"], "SSID": ["x"]}


def test_fixed_vocabulary_lines_keep_their_words():
    """A device named like its kind keeps the kind line readable; the name line and every address are still hidden."""
    ps = privacy.Pseudonymizer({"HOST": ["Storage"]})
    out = ps.out("Name: Storage\nKind: Network storage\nAddress: 192.168.1.4")
    assert out.splitlines() == ["Name: [HOST-1]", "Kind: Network storage", "Address: [IP-1]"]
    assert privacy.leaks(ps.out("Kind: Network storage 192.168.1.4")) == []
