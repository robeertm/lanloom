"""Network routes (DW2), the pure part (lanloom/routes.py): parsers for scamper, classic traceroute and the own helper,
broken input, host name hygiene, places from router names, the round-trip-time plausibility check, exchange points,
the chain of sections and what is condensed for storage (never an address, never a host name).

All addresses are documentation or private ranges, all host names are invented under example.net / example.com /
.invalid, all AS numbers are documentation numbers. Nothing here touches the network."""
import json

import pytest

from lanloom import routes, routewatch

PLACES = routewatch.load_places()
HOME = {"lat": 52.37, "lon": 4.90, "cc": "NL", "name": "Amsterdam"}
DOC_ADDRS = ("192.0.2.", "198.51.100.", "203.0.113.", "2001:db8", "192.168.", "10.0.0.", "100.64.")


# ---------------------------------------------------------------------------------------------------------- scamper
SCAMPER = "\n".join([
    '{"type":"cycle-start","list_name":"default","id":1,"hostname":"x.invalid","start_time":1}',
    json.dumps({"type": "trace", "method": "udp-paris", "dst": "203.0.113.9", "stop_reason": "COMPLETED", "hops": [
        {"addr": "192.168.1.1", "probe_ttl": 1, "rtt": 0.4, "icmp_type": 11},
        {"addr": "198.51.100.1", "probe_ttl": 2, "rtt": 5.5, "icmp_type": 11},
        {"addr": "198.51.100.77", "probe_ttl": 5, "rtt": 12.3, "icmp_type": 11},
        {"addr": "203.0.113.9", "probe_ttl": 6, "rtt": 14.0, "icmp_type": 3, "icmp_code": 3},
        {"addr": "203.0.113.10", "probe_ttl": 7, "rtt": 15.0, "icmp_type": 11}]}),
    '{"type":"cycle-stop","list_name":"default","id":1,"hostname":"x.invalid","stop_time":2}'])


def test_scamper_gaps_reached_and_cycle_lines():
    hops = routes.read_scamper(SCAMPER)
    assert [h["ttl"] for h in hops] == [1, 2, 3, 4, 5, 6]            # nothing after the destination, gaps filled in
    assert [h["ip"] for h in hops] == ["192.168.1.1", "198.51.100.1", None, None, "198.51.100.77", "203.0.113.9"]
    assert hops[2]["rtt"] is None and hops[2]["name"] is None
    assert hops[-1].get("reached") is True and not any(h.get("reached") for h in hops[:-1])
    assert hops[1]["rtt"] == 5.5


def test_scamper_completed_alone_marks_the_destination_and_other_stops_do_not():
    base = {"type": "trace", "dst": "203.0.113.9", "stop_reason": "COMPLETED",
            "hops": [{"addr": "198.51.100.1", "probe_ttl": 1, "rtt": 1.0}, {"addr": "203.0.113.9", "probe_ttl": 2, "rtt": 2.0}]}
    assert routes.read_scamper(json.dumps(base))[-1].get("reached") is True
    base["stop_reason"] = "GAPLIMIT"
    assert not any(h.get("reached") for h in routes.read_scamper(json.dumps(base)))
    base["hops"][1]["icmp_type"] = 3                                   # port unreachable from the destination itself
    assert routes.read_scamper(json.dumps(base))[-1].get("reached") is True


def test_scamper_ipv6_unreachable_type_and_only_the_first_trace():
    v6 = {"type": "trace", "dst": "2001:db8::9", "stop_reason": "GAPLIMIT",
          "hops": [{"addr": "2001:db8::1", "probe_ttl": 1, "rtt": 1.0, "icmp_type": 3},
                   {"addr": "2001:db8::9", "probe_ttl": 2, "rtt": 2.0, "icmp_type": 1}]}
    other = dict(v6, hops=[{"addr": "2001:db8::55", "probe_ttl": 1}])
    hops = routes.read_scamper(json.dumps(v6) + "\n" + json.dumps(other))
    assert [h["ip"] for h in hops] == ["2001:db8::1", "2001:db8::9"]
    assert hops[0].get("reached") is None and hops[1]["reached"] is True


def test_scamper_smallest_rtt_and_name_sanitised():
    t = {"type": "trace", "dst": "203.0.113.9", "hops": [
        {"addr": "198.51.100.1", "probe_ttl": 1, "rtt": 9.0, "name": "ae1.fra2.example.net"},
        {"addr": "198.51.100.1", "probe_ttl": 1, "rtt": 4.0},
        {"addr": "198.51.100.2", "probe_ttl": 2, "rtt": 3.0, "name": "bad name!"}]}
    hops = routes.read_scamper(json.dumps(t))
    assert hops[0]["rtt"] == 4.0 and hops[0]["name"] == "ae1.fra2.example.net"
    assert hops[1]["name"] is None


# ---------------------------------------------------------------------------------------------------------- traceroute text
TRACE_N = """traceroute to 203.0.113.9 (203.0.113.9), 30 hops max, 60 byte packets
 1  192.168.1.1  0.412 ms  0.388 ms
 2  * *
 3  198.51.100.1  5.1 ms 198.51.100.2  6.0 ms
 4  198.51.100.9  8.0 ms  7.5 ms !H
 5  2001:db8::1  9.5 ms  9.0 ms
"""
TRACE_NAMES = """ 1  gw.example.net (192.168.1.1)  0.4 ms  0.3 ms
 2  ae1.fra2.example.net (198.51.100.1)  5.1 ms  4.9 ms
 3  * ae2.fra3.example.net (198.51.100.5)  6.0 ms *
"""


def test_traceroute_numeric_gaps_marks_and_first_address_counts():
    hops = routes.read_traceroute(TRACE_N)
    assert [h["ttl"] for h in hops] == [1, 2, 3, 4, 5]
    assert hops[0] == {"ttl": 1, "ip": "192.168.1.1", "rtt": 0.388, "name": None}
    assert hops[1]["ip"] is None and hops[1]["rtt"] is None
    assert hops[2]["ip"] == "198.51.100.1" and hops[2]["rtt"] == 5.1          # the second address of the line is ignored
    assert hops[3]["ip"] == "198.51.100.9" and hops[3]["rtt"] == 7.5 and "reached" not in hops[3]
    assert hops[4]["ip"] == "2001:db8::1"


def test_traceroute_with_names():
    hops = routes.read_traceroute(TRACE_NAMES)
    assert [(h["ip"], h["name"]) for h in hops] == [("192.168.1.1", "gw.example.net"), ("198.51.100.1", "ae1.fra2.example.net"),
                                                    ("198.51.100.5", "ae2.fra3.example.net")]
    assert hops[1]["rtt"] == 4.9
    assert hops[2]["rtt"] == 6.0


def test_traceroute_unsafe_name_is_dropped_but_the_address_stays():
    hops = routes.read_traceroute(" 1  bad<name> (198.51.100.1)  1.0 ms\n")
    assert hops[0]["ip"] == "198.51.100.1" and hops[0]["name"] is None


# ---------------------------------------------------------------------------------------------------------- own helper
def test_read_probe_basic_and_destination():
    out = {"hops": [{"ttl": 1, "ip": "192.168.1.1", "rtt": 0.5, "name": None},
                    {"ttl": 2, "ip": None, "rtt": None},
                    {"ttl": 3, "ip": "198.51.100.1", "rtt": 6.0, "name": "ae1.fra2.example.net"},
                    {"ttl": 4, "ip": "203.0.113.9", "rtt": 9.0, "reached": True},
                    {"ttl": 5, "ip": "203.0.113.10", "rtt": 9.5}]}
    hops = routes.read_probe(json.dumps(out))
    assert [h["ttl"] for h in hops] == [1, 2, 3, 4]
    assert hops[1]["ip"] is None and hops[2]["name"] == "ae1.fra2.example.net" and hops[3]["reached"] is True


def test_read_probe_wrong_types():
    out = {"hops": [{"ttl": "3", "ip": "198.51.100.1", "rtt": "abc"}, {"ttl": 0, "ip": "198.51.100.2"}, {"ttl": 300, "ip": "198.51.100.3"},
                    {"ttl": 4, "ip": "999.1.1.1", "rtt": -3}, {"ttl": 5, "ip": "198.51.100.5", "rtt": float("nan")}, "x", None, 7,
                    {"ttl": 6, "ip": None, "name": "ae1.fra2.example.net"}]}
    hops = routes.read_probe(json.dumps(out))
    by = {h["ttl"]: h for h in hops}
    assert by[3]["ip"] == "198.51.100.1" and by[3]["rtt"] is None
    assert by[4]["ip"] is None and by[4]["rtt"] is None
    assert by[5]["rtt"] is None
    assert by[6]["name"] is None                                      # a name without an address is never kept
    assert 0 not in by and 300 not in by


# ---------------------------------------------------------------------------------------------------------- broken input
GARBAGE = [
    None, 5, 3.5, b"\x00\xff\xfe", [], {}, "", "   ", "\x00\x01\x02\xff\xfe junk \x00",
    '{"type":"trace","dst":"203.0.113.9","hops":[{"addr":"198.51.1',          # half a JSON line
    '{"type":"trace","hops":"nope"}', '{"type":"trace","hops":[1,"a",null,{"addr":5,"probe_ttl":"x"}]}',
    "[1,2,3]", '"just a string"', "42", "null", '{"hops": 5}', '{"hops":[{"ttl":{}, "ip":[]}]}',
    "[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[", "{" * 5000, "a" * 3_000_000, " 1 " * 700_000,
    "\n".join(" %d  *" % i for i in range(1, 400)),
]


@pytest.mark.parametrize("fn", [routes.read_scamper, routes.read_traceroute, routes.read_probe])
@pytest.mark.parametrize("text", GARBAGE)
def test_garbage_never_raises(fn, text):
    out = fn(text)
    assert isinstance(out, list)
    assert len(out) <= routes.MAX_HOPS
    assert all(isinstance(h, dict) and {"ttl", "ip", "rtt", "name"} <= set(h) for h in out)


def test_truncated_input_gives_the_valid_part():
    assert routes.read_scamper(SCAMPER[:SCAMPER.index('"hops"') + 20]) == []
    cut = TRACE_N + " 6  198.51.1"
    hops = routes.read_traceroute(cut)
    assert [h["ip"] for h in hops[:5]] == ["192.168.1.1", None, "198.51.100.1", "198.51.100.9", "2001:db8::1"]
    assert all(h["ip"] is None for h in hops[5:])
    line1 = json.dumps({"hops": [{"ttl": 1, "ip": "192.168.1.1", "rtt": 1}]})
    hops = routes.read_probe(line1 + '\n{"hops":[{"ttl":2,"ip":"198.5')
    assert [h["ip"] for h in hops] == ["192.168.1.1"]


def test_huge_hop_lists_are_capped():
    big = json.dumps({"hops": [{"ttl": (i % 250) + 1, "ip": "198.51.100.%d" % (i % 250 + 1)} for i in range(255)]})
    assert len(routes.read_probe(big)) <= routes.MAX_HOPS
    text = "\n".join(" %d  198.51.100.%d  1.0 ms" % (i, i) for i in range(1, 200))
    assert len(routes.read_traceroute(text)) <= routes.MAX_HOPS


# ---------------------------------------------------------------------------------------------------------- names and addresses
@pytest.mark.parametrize("name, ok", [
    ("ae1.fra2.example.net", True), ("ae1.fra2.example.net.", True), ("a-b.example.com", True), ("host.example.invalid", True),
    ("bad name.example.net", False), ("bad!.example.net", False), ("a_b.example.net", False), ("hällö.example.net", False),
    ("198.51.100.1", False), ("2001:db8::1", False), ("", False), (None, False), (5, False), ("a b", False), ("x\ny.example.net", False),
])
def test_clean_name(name, ok):
    assert (routes.clean_name(name) is not None) is ok


def test_clean_name_is_trimmed_to_the_maximum_length():
    n = routes.clean_name("a" * 300 + ".example.net")
    assert n is not None and len(n) <= routes.NAME_MAX


@pytest.mark.parametrize("text, out", [("198.51.100.1", "198.51.100.1"), (" [2001:db8::1] ", "2001:db8::1"), ("2001:DB8:0:0::1", "2001:db8::1"),
                                       ("999.1.1.1", None), ("a.b.c.d", None), ("", None), (None, None), ("1" * 60, None),
                                       ("192.0.2.1/24", None)])
def test_clean_ip(text, out):
    assert routes.clean_ip(text) == out


def test_home_addresses():
    for ip in ("192.168.1.1", "10.1.2.3", "172.16.0.1", "100.64.0.1", "169.254.1.1", "127.0.0.1", "fe80::1", "fd00::1"):
        assert routes.is_home_ip(ip), ip
    for ip in ("192.0.2.1", "198.51.100.1", "203.0.113.1", "2001:db8::1", "garbage", None):
        assert not routes.is_home_ip(ip), ip


# ---------------------------------------------------------------------------------------------------------- places from names
def test_places_from_router_names_with_the_shipped_table():
    assert PLACES, "the shipped place table is missing"
    p = routes.places_from_name("ae1.fra2.example.net", PLACES)
    assert p and p[0]["name"] == "Frankfurt" and p[0]["cc"] == "DE" and p[0]["code"] == "fra"
    p = routes.places_from_name("core1.newyork.example.invalid", PLACES)
    assert p and p[0]["name"] == "New York" and p[0]["cc"] == "US"
    assert routes.places_from_name("be1.ams3.example.com", PLACES)[0]["name"] == "Amsterdam"


def test_a_code_inside_a_word_does_not_match():
    for name in ("xfrax1.example.net", "bigfrab.example.net", "frankfurter-x.example.net", "ae1.nycx.example.net", "defrag.example.net"):
        assert routes.places_from_name(name, PLACES) == [], name


def test_the_ending_never_counts_and_the_domain_only_as_a_last_resort():
    assert routes.places_from_name("router1.example.fra", PLACES) == []
    assert routes.places_from_name("ae1.example.ams", PLACES) == []
    p = routes.places_from_name("ae1.example-ams.net", PLACES)               # nothing in the device part: the domain counts
    assert p and p[0]["name"] == "Amsterdam"
    p = routes.places_from_name("ae1.fra.example-ams.net", PLACES)           # device part wins, the domain is ignored
    assert [x["name"] for x in p] == ["Frankfurt"]


def test_blocked_tokens_never_match_even_if_the_table_has_them():
    table = {"core": {"name": "Core", "cc": "XX", "lat": 1.0, "lon": 1.0}, "edge": {"name": "Edge", "cc": "XX", "lat": 2.0, "lon": 2.0},
             "gw": {"name": "GW", "cc": "XX", "lat": 3.0, "lon": 3.0}, "peer": {"name": "Peer", "cc": "XX", "lat": 4.0, "lon": 4.0},
             "fra": {"name": "Frankfurt", "cc": "DE", "lat": 50.0, "lon": 8.5}}
    assert routes.places_from_name("core1.edge2.gw3.peer4.example.net", table) == []
    assert [x["name"] for x in routes.places_from_name("core1.fra1.gw3.example.net", table)] == ["Frankfurt"]


def test_the_longer_key_wins_and_neighbouring_tokens_join():
    table = {"fra": {"name": "Short", "cc": "DE", "lat": 50.0, "lon": 8.5},
             "frankfurt": {"name": "Long", "cc": "DE", "lat": 50.1, "lon": 8.6},
             "newyork": {"name": "New York", "cc": "US", "lat": 40.6, "lon": -73.8}}
    assert [x["name"] for x in routes.places_from_name("fra-frankfurt1.example.net", table)] == ["Long", "Short"]
    assert routes.places_from_name("ae1.new-york2.example.net", table)[0]["name"] == "New York"
    # the same coordinates only once
    t2 = {"nyc": {"name": "NY", "cc": "US", "lat": 40.6, "lon": -73.8}, "newyork": {"name": "NY", "cc": "US", "lat": 40.6, "lon": -73.8}}
    assert len(routes.places_from_name("nyc1.newyork.example.net", t2)) == 1


@pytest.mark.parametrize("name, table", [(None, PLACES), ("", PLACES), ("x" * 5000, PLACES), (5, PLACES), ("fra.example.net", None),
                                         ("fra.example.net", []), ("fra.example.net", {"fra": "x"}), ("fra.example.net", {"fra": {"lat": "a", "lon": 1}})])
def test_places_from_name_bad_input(name, table):
    assert routes.places_from_name(name, table) == []


# ---------------------------------------------------------------------------------------------------------- plausibility
def test_distance_known_values():
    assert routes.distance_km(0, 0, 0, 0) == 0
    assert routes.distance_km(0, 0, 0, 180) == pytest.approx(20015.1, abs=1.0)
    d = routes.distance_km(52.37, 4.90, 50.11, 8.68)
    assert d == pytest.approx(routes.distance_km(50.11, 8.68, 52.37, 4.90)) and 360 < d < 380


def test_plausible_exactly_at_the_limit_and_just_beyond():
    origin = {"lat": 0.0, "lon": 0.0}
    place = {"lat": 0.0, "lon": 10.0}
    d = routes.distance_km(0, 0, 0, 10)
    at = (d - routes.TOLERANCE_KM) / routes.KM_PER_MS * 2.0               # round-trip time whose limit is exactly d
    assert routes.plausible(origin, place, at + 1e-9)
    assert not routes.plausible(origin, place, at - 1e-3)
    assert routes.plausible(origin, place, at + 5)
    assert not routes.plausible(origin, place, 1.0)


def test_plausible_without_rtt_or_place_and_with_zero_rtt():
    origin = {"lat": 0.0, "lon": 0.0}
    far = {"lat": 0.0, "lon": 90.0}
    assert routes.plausible(origin, far, None)
    assert routes.plausible(None, far, 1.0) and routes.plausible(origin, None, 1.0)
    assert routes.plausible({}, far, 1.0)
    near = {"lat": 0.0, "lon": 100.0 / 111.19508}                              # about 100 km
    assert routes.plausible(origin, near, 0)                                    # the tolerance covers it
    assert routes.plausible(origin, near, 0.0)
    assert not routes.plausible(origin, {"lat": 0.0, "lon": 1000.0 / 111.19508}, 0)
    assert routes.plausible(origin, far, -5) is False                           # a negative time counts as zero
    assert routes.plausible({"lat": "x"}, far, 1.0)                             # unusable input never rejects


# ---------------------------------------------------------------------------------------------------------- locate
def test_locate_drops_the_implausible_name_and_uses_the_city_data():
    start = {"lat": 52.0, "lon": 4.5}
    ny = routes.places_from_name("core1.newyork.example.invalid", PLACES)
    city = {"name": "Rotterdam", "lat": 51.92, "lon": 4.48, "cc": "NL"}
    hop = {"ip": "198.51.100.1", "rtt": 4.0}
    p = routes.locate(hop, city, None, ny, start)
    assert p["name"] == "Rotterdam" and p["source"] == "city" and p["sure"] is False and p["cc"] == "NL"
    # without a time the name place is plausible and wins (sure)
    p = routes.locate({"ip": "198.51.100.1", "rtt": None}, city, None, ny, start)
    assert p["source"] == "name" and p["sure"] is True and p["name"] == "New York"
    # a long way is fine for a larger time, and the first plausible name place wins over the city data
    p = routes.locate({"ip": "198.51.100.1", "rtt": 90.0}, city, None, ny, start)
    assert p["source"] == "name"


def test_locate_exchange_beats_name_beats_city_and_nothing_gives_none():
    start = {"lat": 52.0, "lon": 4.5}
    ex = {"name": "Example IX", "city": "Amsterdam", "cc": "NL", "lat": 52.3, "lon": 4.76}
    fra = routes.places_from_name("fra1.example.net", PLACES)
    city = {"name": "Rotterdam", "lat": 51.92, "lon": 4.48}
    hop = {"rtt": 30.0}
    assert routes.locate(hop, city, ex, fra, start)["source"] == "exchange"
    assert routes.locate(hop, city, None, fra, start)["source"] == "name"
    assert routes.locate(hop, city, None, [], start)["source"] == "city"
    assert routes.locate(hop, None, None, [], start) is None
    # an implausible exchange place is dropped as well, city data stays
    far = dict(ex, lat=-33.9, lon=151.2)
    assert routes.locate({"rtt": 3.0}, city, far, [], start)["source"] == "city"
    assert routes.locate({}, None, "nonsense", None, start) is None


def test_locate_takes_the_first_part_of_the_rtt_off():
    start = {"lat": 52.0, "lon": 4.5}
    fra = routes.places_from_name("fra1.example.net", PLACES)              # about 360 km away: needs 2.1 ms
    assert routes.locate({"rtt": 2.0}, None, None, fra, start) is None
    assert routes.locate({"rtt": 12.0}, None, None, fra, start, start_rtt=10.0) is None   # 2 ms left
    assert routes.locate({"rtt": 12.0}, None, None, fra, start, start_rtt=2.0)["name"] == "Frankfurt"


# ---------------------------------------------------------------------------------------------------------- exchange points
IXPFX = [{"prefix": "203.0.113.0/24", "ixlan_id": 1, "in_dfz": False},
         {"prefix": "198.51.100.0/24", "ixlan_id": 1, "in_dfz": True},                      # also routed normally: skipped
         {"prefix": "203.0.113.128/25", "ixlan_id": 3, "in_dfz": False},
         {"prefix": "2001:db8:1::/48", "ixlan_id": 2},
         {"prefix": "not a prefix", "ixlan_id": 1}, {"prefix": "192.0.2.0/24", "ixlan_id": 99}, "junk", {"ixlan_id": 1}]
IX = [{"id": 10, "name": "Example IX", "city": "Frankfurt", "country": "de"},
      {"id": 11, "name": "Sample Exchange", "city": "Amsterdam", "country": "NL"},
      {"id": 12, "name": "Small Exchange", "city": "Rotterdam", "country": "NL"}, "junk", {"name": "no id"}]
IXLAN = [{"id": 1, "ix_id": 10}, {"id": 2, "ix_id": 11}, {"id": 3, "ix_id": 12}]


def test_compact_peeringdb_maps_through_ixlan_and_skips_in_dfz():
    rows = routes.compact_peeringdb(IXPFX, IX, IXLAN)
    got = {r[0]: r[1:] for r in rows}
    assert got == {"203.0.113.0/24": ["Example IX", "Frankfurt", "DE"], "203.0.113.128/25": ["Small Exchange", "Rotterdam", "NL"],
                   "2001:db8:1::/48": ["Sample Exchange", "Amsterdam", "NL"]}
    # data wrapped in {"data": [...]} and without the ixlan list (ixlan_id == ix_id)
    rows = routes.compact_peeringdb({"data": [{"prefix": "203.0.113.0/24", "ixlan_id": 10}]}, {"data": IX})
    assert rows == [["203.0.113.0/24", "Example IX", "Frankfurt", "DE"]]
    assert routes.compact_peeringdb(None, None) == [] and routes.compact_peeringdb("x", 5, 7) == []


def test_exchange_table_longest_prefix_wins_and_ipv6():
    t = routes.exchange_table(routes.compact_peeringdb(IXPFX, IX, IXLAN), PLACES)
    assert t.find("203.0.113.5")["name"] == "Example IX"
    assert t.find("203.0.113.130")["name"] == "Small Exchange"                              # the /25 is more specific than the /24
    assert t.find("2001:db8:1::77")["name"] == "Sample Exchange"
    assert t.find("2001:db8:2::1") is None and t.find("198.51.100.1") is None and t.find("garbage") is None and t.find(None) is None
    hit = t.find("203.0.113.5")
    assert hit["city"] == "Frankfurt" and hit["cc"] == "DE" and isinstance(hit["lat"], float)
    hit["name"] = "changed"
    assert t.find("203.0.113.5")["name"] == "Example IX"                                    # a copy is returned
    assert len(t) == 3


def test_exchange_table_skips_invalid_rows_and_has_no_place_without_a_table():
    t = routes.exchange_table([["203.0.113.0/24", "Example IX", "Frankfurt", "DE"], ["bad", "x", "y", "DE"], ["192.0.2.0/24"], "junk",
                               ["192.0.2.0/24", "", "Frankfurt", "DE"], ["198.51.100.0/24", "Other IX", "Nowhereville", "ZZ"]])
    assert len(t) == 2
    assert "lat" not in t.find("203.0.113.1") and t.find("198.51.100.9")["name"] == "Other IX"
    assert len(routes.exchange_table(None)) == 0


@pytest.mark.parametrize("name, expect", [
    ("decix-gw.example.net", "DE-CIX"), ("ae1.de-cix.example.net", "DE-CIX"), ("peer1.amsix.example.com", "AMS-IX"),
    ("six.example.net", "SIX"), ("ae1.six1.example.net", "SIX"), ("any2.example.net", "Any2"),
    ("sixty.example.net", None), ("sixth.example.net", None), ("vi-x.example.net", None), ("vix.example.net", "VIX"),
    ("core1.fra2.example.net", None), ("example.net", None), ("decix.example.invalid", "DE-CIX"), ("a.decix", None), ("", None), (None, None)])
def test_exchange_from_name(name, expect):
    hit = routes.exchange_from_name(name, PLACES)
    assert (hit["name"] if hit else None) == expect


def test_exchange_from_name_carries_a_place_only_with_a_table():
    hit = routes.exchange_from_name("decix-gw.example.net", PLACES)
    assert hit["city"] == "Frankfurt" and hit["cc"] == "DE" and "lat" in hit
    assert "lat" not in routes.exchange_from_name("decix-gw.example.net")


# ---------------------------------------------------------------------------------------------------------- the chain
GEO = {
    "198.51.100.1": {"asn": 64496, "operator": "Example Broadband", "cc": "NL", "place": {"name": "Amsterdam", "lat": 52.37, "lon": 4.9}},
    "198.51.100.50": {"asn": 64497, "operator": "Example Transit", "cc": "DE", "place": None},
    "198.51.100.60": {"asn": 64497, "operator": "Example Transit", "cc": "DE", "place": None},
    "192.0.2.10": {"asn": 64498, "operator": "Example Cloud", "cc": "US", "place": None},
    "192.0.2.99": {"asn": 64498, "operator": "Example Cloud", "cc": "US", "place": None},
}
HOPS = [
    {"ttl": 1, "ip": "192.168.1.1", "rtt": 0.4, "name": None},
    {"ttl": 2, "ip": "10.20.0.1", "rtt": 1.2, "name": None},
    {"ttl": 3, "ip": "198.51.100.1", "rtt": 3.4, "name": "ae1.ams1.example.net"},
    {"ttl": 4, "ip": "203.0.113.1", "rtt": 4.0, "name": "decix-gw.example.net"},
    {"ttl": 5, "ip": None, "rtt": None, "name": None},
    {"ttl": 6, "ip": "198.51.100.50", "rtt": 6.5, "name": "ae1.fra2.example.net"},
    {"ttl": 7, "ip": "198.51.100.60", "rtt": 7.0, "name": "ae2.fra2.example.net"},
    {"ttl": 8, "ip": "192.0.2.10", "rtt": 47.0, "name": "core1.newyork.example.invalid"},
    {"ttl": 9, "ip": "192.0.2.99", "rtt": 48.0, "name": None, "reached": True},
]
EXTABLE = routes.exchange_table([["203.0.113.0/24", "Example IX", "Frankfurt", "DE"]], PLACES)


def geo(ip):
    return GEO.get(ip)


def chain(hops=HOPS, target=None, exchanges=EXTABLE, start=HOME):
    return routes.build_chain(hops, geo, exchanges, PLACES, start, target)


def test_chain_sections_in_order():
    c = chain(target={"cc": "US", "asn": 64498, "operator": "Example Cloud", "city": "Ashburn",
                      "place": {"name": "Ashburn", "lat": 38.9, "lon": -77.5}})
    assert [k["kind"] for k in c] == ["home", "provider", "exchange", "transit", "network", "target"]
    home, provider, ex, transit, net, tgt = c
    assert len(home["hops"]) == 2 and home["place"]["source"] == "start" and home["rtt"] == 1.2
    assert provider["asn"] == 64496 and provider["operator"] == "Example Broadband"
    assert ex["operator"] == "Example IX" and ex["cc"] == "DE" and ex["place"]["source"] == "exchange" and ex["place"]["sure"]
    assert transit["asn"] == 64497 and len(transit["hops"]) == 2                  # two hops of one network are merged
    assert net["asn"] == 64498 and net["rtt"] == 48.0                           # the last network is the destination's
    assert tgt["reached"] is True and tgt["rtt"] == 48.0 and tgt["asn"] == 64498 and tgt["city"] == "Ashburn"
    # the silent hop belongs to the running section, not a section of its own
    assert any(h["ip"] is None for h in ex["hops"])


def test_chain_places_checked_against_the_time():
    c = chain()
    net = [k for k in c if k["kind"] == "network"][0]
    # New York is about 5900 km from the start; 47 ms allow 4700 + 150 km: the name place is dropped
    assert net["place"] is None or net["place"]["name"] != "New York"
    transit = [k for k in c if k["kind"] == "transit"][0]
    assert transit["place"]["name"] == "Frankfurt" and transit["place"]["source"] == "name"


def test_chain_anycast_target_carries_nothing_about_the_destination():
    c = chain(target={"anycast": True, "cc": "US", "asn": 64498, "operator": "Example Cloud", "city": "Ashburn",
                      "place": {"name": "Ashburn", "lat": 38.9, "lon": -77.5}})
    t = c[-1]
    assert t["kind"] == "target" and t["anycast"] is True
    assert t["cc"] is None and t["asn"] is None and t["operator"] is None and t["city"] is None and t["place"] is None
    cond = routes.condense(c)[-1]
    assert cond["anycast"] is True and cond["cc"] is None and cond["asn"] is None and cond["operator"] is None and cond["city"] is None


def test_chain_without_target_or_with_bad_input():
    assert chain(target=None)[-1]["kind"] != "target"
    assert routes.build_chain("nonsense", geo, None, PLACES, HOME) == []
    c = routes.build_chain([None, 5, {"ttl": 1, "ip": "198.51.100.1"}, {"ip": "bad"}], None, None, PLACES, None)
    assert c[0]["kind"] == "home" and c[0]["place"] is None
    boom = routes.build_chain([{"ttl": 1, "ip": "198.51.100.1", "rtt": 3.0}], lambda ip: 1 / 0, None, PLACES, HOME)
    assert boom[0]["kind"] == "home" and len(boom) == 2


def test_chain_without_network_numbers_makes_one_section_per_country():
    g = {"198.51.100.1": {"cc": "NL"}, "198.51.100.2": {"cc": "DE"}, "198.51.100.3": {"cc": "DE"}, "198.51.100.4": {"cc": "US"}}
    hops = [{"ttl": i + 1, "ip": ip, "rtt": float(i + 1)} for i, ip in enumerate(g)]
    c = routes.build_chain(hops, g.get, None, {}, None)
    assert [(k["kind"], k["cc"]) for k in c[1:]] == [("provider", "NL"), ("transit", "DE"), ("network", "US")]


# ---------------------------------------------------------------------------------------------------------- condense
def test_condense_has_no_address_and_no_host_name():
    c = chain(target={"cc": "US", "asn": 64498, "operator": "Example Cloud", "city": "Ashburn"})
    text = json.dumps(routes.condense(c))
    for bad in DOC_ADDRS:
        assert bad not in text, bad
    assert "example." not in text and "decix" not in text.lower() and "ae1" not in text
    # even if foreign text is smuggled into the free fields, only printable short text stays
    c[2]["operator"] = "Evil\x00\x1b[31m" + "x" * 500
    s = routes.condense(c)[2]
    assert "\x00" not in s["operator"] and "\x1b" not in s["operator"] and len(s["operator"]) <= 80


def test_condense_shape_and_hop_counts():
    sec = routes.condense(chain(target={"cc": "US"}))
    assert [s["kind"] for s in sec] == ["home", "provider", "exchange", "transit", "network", "target"]
    assert all(set(s) >= {"kind", "asn", "operator", "cc", "place", "rtt", "hops"} for s in sec)
    assert all(isinstance(s["hops"], int) for s in sec) and sec[3]["hops"] == 2
    assert set(sec[-1]) >= {"anycast", "reached", "city"}
    assert routes.condense(None) == [] and routes.condense([None, 5, {"kind": "bogus"}]) == []
    p = [s for s in sec if s["place"]][0]["place"]
    assert set(p) == {"name", "cc", "lat", "lon", "source", "sure"}
    assert p["lat"] == round(p["lat"], 2)


@pytest.mark.parametrize("ms, band", [(3.4, 3), (0.2, 1), (0, 1), (9.4, 9), (47, 45), (47.4, 45), (123, 120), (299, 300), (333, 350), (None, None),
                                      ("x", None), (float("nan"), None), (-4, None), (1e9, None)])
def test_rtt_band(ms, band):
    assert routes.rtt_band(ms) == band


# ---------------------------------------------------------------------------------------------------------- signature, estimated
def _sec(kind, asn=None, operator=None, cc=None, place=None, rtt=None):
    return {"kind": kind, "asn": asn, "operator": operator, "cc": cc, "place": place, "rtt": rtt, "hops": 1}


def test_signature_ignores_rtt_and_place_but_not_the_order_of_networks():
    a = [_sec("home"), _sec("provider", 64496, rtt=5), _sec("transit", 64497, rtt=10), _sec("network", 64498, rtt=40), _sec("target")]
    b = [_sec("home", rtt=9), _sec("provider", 64496, rtt=7, place={"lat": 1.0, "lon": 2.0, "sure": True}),
         _sec("transit", 64497, rtt=99), _sec("network", 64498, rtt=400), _sec("target", rtt=3)]
    assert routes.signature(a) == routes.signature(b) == "p:64496>t:64497>n:64498"
    swapped = [a[0], a[1], a[3], a[2], a[4]]
    assert routes.signature(swapped) != routes.signature(a)
    extra = a[:3] + [_sec("exchange", operator="Example IX")] + a[3:]
    assert routes.signature(extra) != routes.signature(a) and "e:Example IX" in routes.signature(extra)
    assert routes.signature(None) == "" and routes.signature([None, 5]) == "" and len(routes.signature(a * 100)) <= 400


def test_signature_of_a_real_chain_is_stable_against_places():
    s1 = routes.condense(chain(target={"cc": "US"}))
    hops2 = [dict(h, rtt=(h["rtt"] * 1.3 if h["rtt"] else h["rtt"])) for h in HOPS]
    s2 = routes.condense(chain(hops2, target={"cc": "US"}))
    assert routes.signature(s1) == routes.signature(s2)
    assert "203.0.113" not in routes.signature(s1) and "example." not in routes.signature(s1)


def test_is_estimated_and_uncertain():
    sure = {"lat": 1.0, "lon": 2.0, "sure": True}
    rough = {"lat": 1.0, "lon": 2.0, "sure": False}
    assert routes.is_estimated(_sec("transit", 1, place=rough))
    assert not routes.is_estimated(_sec("transit", 1, place=sure))
    assert routes.is_estimated(_sec("transit", 1, cc="DE"))                       # only a country: estimated
    assert not routes.is_estimated(_sec("transit", 1))                            # nothing known: not drawn, not estimated
    assert not routes.is_estimated(_sec("target", 1, cc="DE"))                    # the destination without a place is not estimated
    assert routes.is_estimated(_sec("target", 1, cc="DE", place=rough))
    assert not routes.is_estimated(_sec("home", cc="NL", place=rough))
    assert not routes.is_estimated(None) and not routes.is_estimated("x")
    assert routes.uncertain([_sec("home"), _sec("transit", 1, place=sure), _sec("network", 2, place=rough)])
    assert not routes.uncertain([_sec("home"), _sec("transit", 1, place=sure)])
    assert not routes.uncertain(None) and not routes.uncertain([])
