"""Classifier, byte conservation, live grades and the read-only promises of the core."""
import ast
import random
from pathlib import Path

from lanloom import classify, live
from lanloom.demo import HOSTS, scan_host

ROOT = Path(__file__).resolve().parent.parent


def test_demo_hosts_classify_as_expected():
    expect = {"192.168.50.1": "router", "192.168.50.10": "nas", "192.168.50.11": "self", "192.168.50.13": "broker",
              "192.168.50.14": "hub", "192.168.50.40": "printer", "192.168.50.50": "camera", "192.168.50.30": "tv",
              "192.168.50.21": "computer", "192.168.50.3": "ap", "192.168.50.99": "unknown"}
    got = {h["ip"]: classify.classify(scan_host(h))["kind"] for h in HOSTS}
    for ip, kind in expect.items():
        assert got[ip] == kind, (ip, got[ip])


def test_classifier_confidence_bands():
    v = classify.classify({"ip": "10.0.0.5", "ports": [{"port": 631, "service": "ipp"}, {"port": 9100, "service": "printer-raw"}],
                           "mdns": [{"type": "_ipp._tcp", "name": "Printer"}]})
    assert v["kind"] == "printer" and v["certainty"] == "sure"
    v = classify.classify({"ip": "10.0.0.6", "ports": []})
    assert v["kind"] == "unknown" and v["certainty"] == "unknown"


def test_device_id_prefers_mac():
    assert classify.device_id({"ip": "10.0.0.1", "mac": "02:AA:bb:cc:dd:ee"}) == "d-02aabbccddee"
    assert classify.device_id({"ip": "10.0.0.1", "mac": None}) == "d-ip-10-0-0-1"


def test_pretty_host_names():
    assert classify.pretty_host("living-room-tv") == "Living room tv"
    assert classify.pretty_host("NAS01") == "NAS01"
    assert classify.pretty_host("192") == "192"


def test_byte_conservation_with_changing_quantum():
    rng = random.Random(3)
    q = live.Quantizer()
    total, dots = [0, 0], [0, 0]
    t = 1000.0
    for i in range(3000):
        b0, b1 = rng.randint(0, 3_000_000), rng.randint(0, 50_000)
        n0, n1 = q.book("x", b0, b1, t)
        dots[0] += n0 * q.q
        dots[1] += n1 * q.q
        total[0] += b0
        total[1] += b1
        t += 1
        if i % 30 == 0:
            q.check_q(t)
    for d in (0, 1):
        assert dots[d] + q.carry.get(("x", d), 0) == total[d]
        assert q.carry.get(("x", d), 0) < q.q


def test_quantum_targets_a_few_dots_per_second():
    q = live.Quantizer()
    t = 0.0
    for _ in range(200):
        q.book("a", 4_000_000, 0, t)
        t += 1
    q.q_since = -999
    q.check_q(t)
    assert 2 <= 4_000_000 / q.q <= 20


def test_grade_rules():
    assert live.grade_for(1, 0.5) == "realtime"
    assert live.grade_for(10, 5) == "near"
    assert live.grade_for(120, 60) == "periodic"
    assert live.grade_for(1200, 60) == "sluggish"
    assert live.grade_for(1, 30) == "interrupted"
    assert live.grade_for(None, None) == "unknown"


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_hub_source_has_no_way_to_change_anything():
    src = _source("lanloom/sources/hub.py")
    assert "call_service" not in src and "fire_event" not in src


def test_snmp_builds_only_read_pdus():
    from lanloom.sources import snmp
    assert snmp._READ_PDUS == frozenset({0xA0, 0xA1, 0xA5})       # Get, GetNext, GetBulk - never Set (0xA3)
    assert "0xA3" not in _source("lanloom/sources/snmp.py").replace("0xa3", "0xA3")


def test_mqtt_cannot_publish():
    from lanloom.sources import mqtt
    assert mqtt.ALLOWED_PACKETS == frozenset({mqtt.CONNECT, mqtt.SUBSCRIBE, mqtt.PINGREQ, mqtt.DISCONNECT})


def test_hub_allow_list_is_read_only():
    from lanloom.sources import hub
    assert hub.ALLOWED_EVENTS == frozenset({"state_changed"})
    for t in hub.ALLOWED_TYPES:
        assert t in ("auth", "get_states", "get_config", "config/device_registry/list", "config/entity_registry/list",
                     "config/area_registry/list", "subscribe_events"), t


def test_no_subprocess_or_shell_in_product():
    for path in list((ROOT / "lanloom").rglob("*.py")) + list((ROOT / "updater").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                assert not any(m.split(".")[0] in ("subprocess", "pty") for m in mods), path
            if isinstance(node, ast.Attribute) and node.attr in ("system", "popen") and getattr(node.value, "id", "") == "os":
                raise AssertionError("os.%s in %s" % (node.attr, path))
