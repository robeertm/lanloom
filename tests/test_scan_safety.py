import pytest

from lanloom.scan import make_plan, safety, validate_plan
from lanloom.scan.runner import R_PLAN_INVALID


@pytest.mark.parametrize("cidr", ["192.168.1.0/24", "10.0.0.0/22", "172.16.5.0/24", "192.168.1.10",
                                  "10.1.2.3/24", "169.254.10.0/24", "172.31.255.0/23"])
def test_allowed_gentle(cidr):
    assert safety.check_cidr(cidr) == (True, safety.OK)


@pytest.mark.parametrize("cidr,key", [
    ("8.8.8.0/24", "scan.unsafe.public"),
    ("1.1.1.1", "scan.unsafe.public"),
    ("192.0.2.0/24", "scan.unsafe.public"),          # documentation range is not private LAN space
    ("172.32.0.0/24", "scan.unsafe.public"),
    ("172.15.255.0/24", "scan.unsafe.public"),
    ("0.0.0.0/24", "scan.unsafe.public"),
    ("255.255.255.255", "scan.unsafe.public"),
    ("240.0.0.0/24", "scan.unsafe.public"),
    ("100.64.0.0/24", "scan.unsafe.cgnat"),
    ("100.127.255.0/24", "scan.unsafe.cgnat"),
    ("127.0.0.0/24", "scan.unsafe.loopback"),
    ("224.0.0.0/24", "scan.unsafe.multicast"),
    ("239.255.255.250", "scan.unsafe.multicast"),
    ("10.0.0.0/8", "scan.unsafe.too_large"),
    ("192.168.0.0/16", "scan.unsafe.too_large"),
    ("192.168.0.0/21", "scan.unsafe.too_large"),
    ("169.254.0.0/16", "scan.unsafe.too_large"),
    ("2001:db8::/64", "scan.unsafe.ipv6"),
    ("fe80::/64", "scan.unsafe.ipv6"),
    ("fd00::1", "scan.unsafe.ipv6"),
    ("not-an-ip", "scan.unsafe.invalid"),
    ("", "scan.unsafe.invalid"),
    (None, "scan.unsafe.invalid"),
    (12345, "scan.unsafe.invalid"),
    ("10.0.0.0/33", "scan.unsafe.invalid"),
    ("172.0.0.0/8", "scan.unsafe.public"),
])
def test_refused(cidr, key):
    ok, reason = safety.check_cidr(cidr)
    assert not ok
    assert reason == key


def test_deep_size_limit():
    assert safety.check_cidr("192.168.1.0/24", deep=True) == (True, safety.OK)
    assert safety.check_cidr("192.168.0.0/23", deep=True) == (False, safety.R_TOO_LARGE)
    assert safety.check_cidr("192.168.0.0/23", deep=False) == (True, safety.OK)


def test_plan_requires_consent_for_deep():
    with pytest.raises(ValueError) as ei:
        make_plan(["192.168.1.0/24"], deep=True, consent=False)
    assert str(ei.value) == "scan.unsafe.consent"
    with pytest.raises(ValueError):
        make_plan(["192.168.1.0/24"], deep=True, consent="yes")
    plan = make_plan(["192.168.1.0/24"], deep=True, consent=True)
    assert plan["mode"] == "deep" and len(plan["ports"]) >= 190


def test_plan_gentle_defaults():
    plan = make_plan(["192.168.1.0/24"])
    assert plan["mode"] == "gentle"
    assert plan["rate"] == 40.0 and plan["concurrency"] == 24 and plan["per_host"] == 4
    assert plan["connect_timeout"] == 0.8
    assert 20 <= len(plan["ports"]) <= 30
    assert validate_plan(plan)


def test_plan_refuses_unsafe_and_empty():
    for bad, key in ((["8.8.8.0/24"], "scan.unsafe.public"), ([], "scan.unsafe.empty"),
                     ("192.168.1.0/24", "scan.unsafe.empty"),
                     (["192.168.1.0/24", "100.64.0.0/24"], "scan.unsafe.cgnat"),
                     ([f"10.0.{i}.0/24" for i in range(9)], "scan.unsafe.too_many")):
        with pytest.raises(ValueError) as ei:
            make_plan(bad)
        assert str(ei.value) == key


def test_plan_total_size_cap_and_dedupe():
    plan = make_plan(["192.168.1.0/24", "192.168.1.0/25", "192.168.1.7"])
    assert plan["cidrs"] == ["192.168.1.0/24"]
    with pytest.raises(ValueError) as ei:
        make_plan([f"10.{i}.0.0/22" for i in range(5)])
    assert str(ei.value) == "scan.unsafe.too_large"


def test_plan_overrides_validated():
    plan = make_plan(["192.168.1.0/24"], rate=10, mdns=False)
    assert plan["rate"] == 10.0 and plan["mdns"] is False
    for kw in ({"rate": 1000}, {"rate": 0}, {"rate": True}, {"per_host": 50}, {"bogus": 1}, {"mdns": 1},
               {"connect_timeout": "1"}):
        with pytest.raises(ValueError) as ei:
            make_plan(["192.168.1.0/24"], **kw)
        assert str(ei.value) == R_PLAN_INVALID


def test_validate_plan_detects_tampering():
    plan = make_plan(["192.168.1.0/24"])
    for mutate in (lambda p: p.update(cidrs=["8.8.8.0/24"]),
                   lambda p: p.update(mode="deep"),
                   lambda p: p.update(ports=[80, 6666]),
                   lambda p: p.update(ports=[]),
                   lambda p: p.update(rate=1e6),
                   lambda p: p.update(version=99),
                   lambda p: p.pop("banners")):
        p = dict(plan)
        mutate(p)
        with pytest.raises(ValueError):
            validate_plan(p)
    with pytest.raises(ValueError):
        validate_plan("nope")


def test_host_addresses():
    import ipaddress
    assert len(safety.host_addresses(ipaddress.ip_network("192.168.1.0/24"))) == 254
    assert safety.host_addresses(ipaddress.ip_network("192.168.1.5/32")) == ["192.168.1.5"]
    nets = [ipaddress.ip_network("192.168.1.0/24")]
    assert safety.is_scannable_ip("192.168.1.9", nets)
    assert not safety.is_scannable_ip("192.168.2.9", nets)
    assert not safety.is_scannable_ip("garbage", nets)
