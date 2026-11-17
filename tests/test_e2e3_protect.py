"""Protection list: classes, the shield of one scan, and proof that the scanner sends nothing it must not send."""
import asyncio
import ipaddress
import pathlib

import pytest

from lanloom.scan import banners, llmnr, make_plan, mdns, nbns, ndp, neighbors, netinfo, ports, protect, run, runner, snmpprobe, ssdp, wsd

PKG = pathlib.Path(runner.__file__).parent


def _ports(*nums):
    return {"ports": [{"port": n} for n in nums]}


# ---------------------------------------------------------------------- auto class
@pytest.mark.parametrize("data,klass", [
    (_ports(631), "printer"),
    (_ports(9100), "printer"),
    ({"mdns": [{"type": "_ipp._tcp"}]}, "printer"),
    ({"mdns": [{"type": "_pdl-datastream._tcp"}]}, "printer"),
    ({"ssdp": [{"model": "LaserJet X"}]}, "printer"),
    (_ports(502), "industrial"),
    (_ports(102), "industrial"),
    (_ports(44818), "industrial"),
    (_ports(47808), "industrial"),
    ({"mdns": [{"type": "_bacnet._udp"}]}, "industrial"),
    (_ports(104), "passive"),
    (_ports(2575), "passive"),
    (_ports(11112), "passive"),
    ({"mdns": [{"type": "_dicom._tcp"}]}, "passive"),
    ({"mdns": [{"type": "_pos-terminal._tcp"}]}, "passive"),
])
def test_auto_class_from_ports_and_announcements(data, klass):
    got, why = protect.auto_class(data)
    assert got == klass and why == "protect.why." + {"printer": "printer", "industrial": "industrial", "passive": "medical"}[klass]


def test_auto_class_none_for_ordinary_hosts():
    assert protect.auto_class({}) == (None, None)
    assert protect.auto_class(_ports(80, 443, 22)) == (None, None)
    assert protect.auto_class(_ports(20000)) == (None, None)            # alone it is also a plain web port
    assert protect.auto_class({"mdns": [{"type": "_http._tcp"}], "ssdp": [{"server": "x"}]}) == (None, None)
    assert protect.auto_class({"ports": [{"port": "502"}, "junk", {"proto": "tcp"}], "mdns": ["x", None]}) == (None, None)


def test_strongest_class_wins_when_signs_mix():
    assert protect.auto_class(_ports(631, 502))[0] == "industrial"
    assert protect.auto_class(_ports(631, 502, 104))[0] == "passive"


# ---------------------------------------------------------------------- resolve: mark, none, sticky
def test_mark_overrides_the_automatic_class():
    assert protect.resolve(_ports(502), "passive") == ("passive", "protect.why.mark", "mark")
    assert protect.resolve({}, "printer") == ("printer", "protect.why.mark", "mark")
    assert protect.resolve(_ports(631), "industrial")[0] == "industrial"


def test_mark_none_switches_the_automatic_class_off():
    assert protect.resolve(_ports(502), "none") == (None, None, None)
    sticky = {"protect": {"class": "printer", "source": "auto", "why": "protect.why.printer"}}
    assert protect.resolve(sticky, "none") == (None, None, None)


def test_automatic_class_is_taken_without_a_mark():
    assert protect.resolve(_ports(631)) == ("printer", "protect.why.printer", "auto")
    assert protect.resolve(_ports(631), "bogus") == ("printer", "protect.why.printer", "auto")
    assert protect.resolve({}) == (None, None, None)


def test_automatic_class_is_sticky_but_a_mark_class_is_not():
    stored = {"protect": {"class": "industrial", "source": "auto", "why": "protect.why.industrial"}}
    assert protect.resolve(stored) == ("industrial", "protect.why.industrial", "auto")
    assert protect.resolve({"protect": {"class": "passive", "source": "auto"}}) == ("passive", "protect.why.earlier", "auto")
    assert protect.resolve({"protect": {"class": "passive", "source": "mark", "why": "protect.why.mark"}}) == (None, None, None)
    assert protect.resolve({"protect": {"class": "bogus", "source": "auto"}}) == (None, None, None)
    assert protect.resolve({"protect": "junk"}) == (None, None, None)
    # what the host shows now wins over the stored class
    assert protect.resolve(dict(stored, **_ports(104)))[0] == "passive"


# ---------------------------------------------------------------------- shield
def _shield(klass, deep=False, ip="10.0.0.5"):
    return protect.Shield({ip: {"class": klass, "why": "w", "source": "mark"}} if klass else None, deep=deep), ip


@pytest.mark.parametrize("deep", [False, True])
def test_printer_never_gets_a_raw_printing_connection(deep):
    s, ip = _shield("printer", deep)
    for port in (9100, 9101, 9102, 515):
        assert not s.allow_port(ip, port), port
    assert s.allow_port(ip, 80) and s.allow_port(ip, 631) and s.allow_port(ip, 443)
    assert s.allow_banner(ip, 80) and s.allow_datagram(ip)
    assert s.allow_port(ip, 502) is deep                                 # sensitive ports only in a deep scan


@pytest.mark.parametrize("deep", [False, True])
def test_industrial_host_gets_only_the_web_ports(deep):
    s, ip = _shield("industrial", deep)
    assert s.allow_port(ip, 80) and s.allow_port(ip, 443)
    for port in (22, 502, 102, 20000, 44818, 47808, 631, 9100, 8080):
        assert not s.allow_port(ip, port), port
    assert s.allow_banner(ip, 80) and s.allow_banner(ip, 443) and not s.allow_banner(ip, 8080)
    assert not s.allow_datagram(ip)


@pytest.mark.parametrize("deep", [False, True])
def test_passive_host_gets_nothing(deep):
    s, ip = _shield("passive", deep)
    assert not any(s.allow_port(ip, p) for p in (22, 80, 443, 502, 631, 9100))
    assert not s.allow_banner(ip, 80) and not s.allow_datagram(ip)


def test_unclassified_host_sensitive_ports_only_when_deep():
    s, ip = _shield(None)
    assert s.allow_port(ip, 80) and s.allow_banner(ip, 80) and s.allow_datagram(ip)
    for port in sorted(protect.SENSITIVE_PORTS):
        assert not s.allow_port(ip, port), port
        assert protect.Shield(deep=True).allow_port(ip, port), port


def test_shield_observe_only_raises_and_respects_marks():
    s = protect.Shield({"10.0.0.2": {"class": "printer", "source": "auto"},
                        "10.0.0.3": {"class": "printer", "source": "mark"},
                        "10.0.0.4": {"source": "none"}})
    s.observe("10.0.0.2", _ports(502))
    assert s.klass("10.0.0.2") == "industrial"
    s.observe("10.0.0.2", _ports(631))
    assert s.klass("10.0.0.2") == "industrial"                           # never lowered
    s.observe("10.0.0.3", _ports(104))
    assert s.klass("10.0.0.3") == "printer"                              # the user's mark stays
    s.observe("10.0.0.4", _ports(104))
    assert s.klass("10.0.0.4") is None                                   # "none" stays off
    s.note_open("10.0.0.9", 104)
    assert s.klass("10.0.0.9") == "passive" and s.allow_port("10.0.0.9", 80) is False
    assert s.result("10.0.0.9")["class"] == "passive" and s.result("10.0.0.10") is None
    assert s.skipped(["10.0.0.2", "10.0.0.3", "10.0.0.4", "10.0.0.9"]) == 3


def test_shield_ignores_junk_entries():
    s = protect.Shield({"10.0.0.2": "junk", "10.0.0.3": {"class": "bogus"}, "10.0.0.4": None})
    assert all(s.klass(ip) is None for ip in ("10.0.0.2", "10.0.0.3", "10.0.0.4"))


# ---------------------------------------------------------------------- the runner with a fake network
NET = "192.168.78.0/24"
PRINTER, PASSIVE, PLAIN = "192.168.78.11", "192.168.78.10", "192.168.78.13"
OPEN = {(PASSIVE, 80), (PRINTER, 631), (PRINTER, 9100), (PRINTER, 80), (PLAIN, 80), (PLAIN, 9100)}
ALIVE = {"192.168.78.1", PASSIVE, PRINTER, PLAIN, "192.168.78.50"}


@pytest.fixture
def fake_net(monkeypatch):
    calls = {"nudged": [], "probed": [], "bannered": [], "datagrams": []}
    monkeypatch.setattr(netinfo, "detect_networks", lambda: [
        {"cidr": NET, "iface": "eth0", "self_ip": "192.168.78.50", "gateway": "192.168.78.1",
         "private": True, "size": 256, "host_mode": True}])
    state = {"arp": {}}
    monkeypatch.setattr(neighbors, "read_arp", lambda path=None: dict(state["arp"]))

    async def nudge(addrs, bucket, port=9):
        calls["nudged"].extend(addrs)
        state["arp"] = {ip: {"mac": "02:00:00:00:00:%02x" % int(ip.rsplit(".", 1)[1]), "iface": "eth0"}
                        for ip in ALIVE - {"192.168.78.50"}}
        return len(addrs)

    async def browse(duration, networks=None, iface_ips=(), progress=None, **kw):
        return ({PRINTER: {"services": [{"type": "_ipp._tcp", "name": "Printer", "port": 631, "txt": {}}],
                           "hostnames": {"printer.local"}}}, 1)

    async def search(duration, networks=(), iface_ips=(), progress=None, **kw):
        return {}, 0

    async def tcp_probe(ip, port, timeout):
        calls["probed"].append((ip, port))
        assert ipaddress.ip_address(ip) in ipaddress.ip_network(NET)
        if (ip, port) in OPEN:
            return ports.OPEN, 0.5
        return (ports.REFUSED, 0.3) if ip in ALIVE else (ports.TIMEOUT, None)

    async def fetch_banner(ip, port, tls, timeout=2.0):
        calls["bannered"].append((ip, port))
        return {"title": "T", "server": "S"}

    async def rnames(ips, timeout):
        return {}

    async def no_wsd(*a, **kw):
        return {}, 0

    def recording(kind):
        async def answers(ips, *a, **kw):
            calls["datagrams"].extend((kind, ip) for ip in ips)
            return {}
        return answers

    monkeypatch.setattr(neighbors, "nudge", nudge)
    monkeypatch.setattr(mdns, "browse", browse)
    monkeypatch.setattr(ssdp, "search", search)
    monkeypatch.setattr(ports, "tcp_probe", tcp_probe)
    monkeypatch.setattr(banners, "fetch_banner", fetch_banner)
    monkeypatch.setattr(runner, "_reverse_names", rnames)
    monkeypatch.setattr(wsd, "probe", no_wsd)
    monkeypatch.setattr(ndp, "read_ndp", lambda timeout=1.0: {})
    monkeypatch.setattr(nbns, "query", recording("nbns"))
    monkeypatch.setattr(llmnr, "reverse", recording("llmnr"))
    monkeypatch.setattr(snmpprobe, "query", recording("snmp"))
    return calls


def _plan(**kw):
    return make_plan([NET], settle_s=0.0, rate=100, **kw)


def test_host_announcing_a_printer_gets_no_raw_port_connection(fake_net):
    res = asyncio.run(run(_plan(snmp_public=True)))
    hosts = {h["ip"]: h for h in res["hosts"]}
    assert (PRINTER, 9100) not in fake_net["probed"]
    assert (PLAIN, 9100) in fake_net["probed"]                           # the guard is per host, not per port
    assert (PRINTER, 631) in fake_net["probed"] and (PRINTER, 80) in fake_net["probed"]
    assert [p["port"] for p in hosts[PRINTER]["ports"]] == [80, 631]
    assert hosts[PRINTER]["protect"]["class"] == "printer" and hosts[PRINTER]["protect"]["why"] == "protect.why.printer"
    assert hosts[PLAIN]["protect"]["class"] == "printer"                 # an open printing port is a sign itself
    assert ("snmp", PRINTER) in fake_net["datagrams"]                    # a printer still answers name questions


def test_marked_passive_host_is_not_touched_at_all(fake_net):
    known = {PASSIVE: {"class": "passive", "why": "protect.why.mark", "source": "mark"}}
    res = asyncio.run(run(_plan(snmp_public=True), protected=known))
    hosts = {h["ip"]: h for h in res["hosts"]}
    assert PASSIVE in hosts                                              # still listed (the neighbour table showed it)
    assert not [c for c in fake_net["probed"] if c[0] == PASSIVE]
    assert not [c for c in fake_net["bannered"] if c[0] == PASSIVE]
    assert not [c for c in fake_net["datagrams"] if c[1] == PASSIVE]
    assert PASSIVE not in fake_net["nudged"]
    assert hosts[PASSIVE]["ports"] == [] and hosts[PASSIVE]["protect"] == {
        "class": "passive", "why": "protect.why.mark", "source": "mark"}
    # the others are still scanned and asked
    assert (PLAIN, 80) in fake_net["probed"] and (PLAIN, 80) in fake_net["bannered"]
    assert {("nbns", PLAIN), ("llmnr", PLAIN), ("snmp", PLAIN)} <= set(fake_net["datagrams"])


def test_industrial_host_only_gets_web_ports_and_no_datagram(fake_net):
    known = {PLAIN: {"class": "industrial", "why": "protect.why.industrial", "source": "auto"}}
    res = asyncio.run(run(_plan(snmp_public=True), protected=known))
    probed = {p for ip, p in fake_net["probed"] if ip == PLAIN}
    assert probed == {80, 443}
    assert not [c for c in fake_net["datagrams"] if c[1] == PLAIN]
    assert {h["ip"]: h for h in res["hosts"]}[PLAIN]["protect"]["class"] == "industrial"


def test_a_class_found_during_the_scan_stops_further_probes(fake_net):
    plan = make_plan([NET], deep=True, consent=True, settle_s=0.0, rate=100, concurrency=1, per_host=1,
                     mdns=False, ssdp=False)
    plan["ports"] = [22, 80, 104, 502, 631]
    fake_net_open = {(PLAIN, 104)}
    asyncio.run(_run_with_open(plan, fake_net_open))


async def _run_with_open(plan, extra_open):
    seen = []
    orig = ports.tcp_probe

    async def tcp_probe(ip, port, timeout):
        seen.append((ip, port))
        return (ports.OPEN, 0.5) if (ip, port) in extra_open else (ports.REFUSED, 0.3)
    ports.tcp_probe = tcp_probe
    try:
        res = await run(plan)
    finally:
        ports.tcp_probe = orig
    mine = [p for ip, p in seen if ip == PLAIN]
    assert 104 in mine and max(mine) == 104                        # nothing after the medical port answered
    assert {h["ip"]: h for h in res["hosts"]}[PLAIN]["protect"]["class"] == "passive"


def test_deep_scan_probes_sensitive_ports_only_on_unprotected_hosts(fake_net):
    plan = make_plan([NET], deep=True, consent=True, settle_s=0.0, rate=100)
    plan["ports"] = [22, 80, 104, 502, 515, 631, 9100]
    known = {PRINTER: {"class": "printer", "why": "protect.why.printer", "source": "auto"},
             PASSIVE: {"class": "passive", "why": "protect.why.mark", "source": "mark"}}
    asyncio.run(run(plan, protected=known))
    probed = set(fake_net["probed"])
    assert (PLAIN, 502) in probed and (PLAIN, 104) in probed and (PLAIN, 515) in probed
    assert (PRINTER, 502) in probed and (PRINTER, 515) not in probed and (PRINTER, 9100) not in probed
    assert not [c for c in probed if c[0] == PASSIVE]


# ---------------------------------------------------------------------- static guarantees
def test_gentle_list_has_no_industrial_or_medical_port_and_deep_list_has_them():
    sensitive = protect.INDUSTRIAL_PORTS | protect.MEDICAL_PORTS
    assert sensitive == {102, 104, 502, 2575, 11112, 44818, 47808, 20000}
    assert not sensitive & set(ports.GENTLE_PORTS)
    assert sensitive <= set(ports.DEEP_PORTS)
    assert {9100, 515} <= set(ports.DEEP_PORTS)


def test_web_ports_never_include_printing_or_sensitive_ports():
    assert not set(ports.WEB_PORTS) & (protect.PRINTER_RAW | protect.SENSITIVE_PORTS)


def test_no_payload_is_ever_sent_to_the_printing_ports():
    """Only plain connects reach 9100 and 515 (and only on hosts that are not printers): banners and datagrams are built
    for fixed other ports, and no scan module names these ports besides the port tables and the protection list."""
    for p in PKG.glob("*.py"):
        if p.name in ("ports.py", "protect.py"):
            continue
        text = p.read_text()
        for raw in (":9100", ", 9100", "9101", "9102", ":515", ", 515", "(515"):
            assert raw not in text, "%s mentions %s" % (p.name, raw)
    banner_src = (PKG / "banners.py").read_text()
    assert "fetch_banner" in banner_src and "ports." not in banner_src.replace("ports.WEB", "")
    ports_src = (PKG / "ports.py").read_text()
    assert ports_src.count(".send(") + ports_src.count(".write(") + ports_src.count("sendall(") == 0, \
        "the port probe only connects"


def test_runner_has_no_other_way_to_open_a_port_than_the_shielded_jobs():
    src = (PKG / "runner.py").read_text()
    assert src.count("banners.fetch_banner(") == 1 and src.count("ports.tcp_probe(") == 2     # sweep + shielded jobs
    assert "shield.allow_port(ip, port)" in src and "shield.allow_banner(ip, p)" in src
    assert src.count("shield.allow_datagram") >= 2


def test_routed_range_sweep_skips_a_protected_host(fake_net, monkeypatch):
    monkeypatch.setattr(netinfo, "detect_networks", lambda: [])
    known = {PASSIVE: {"class": "passive", "why": "protect.why.mark", "source": "mark"}}
    asyncio.run(run(make_plan(["192.168.78.0/28"], settle_s=0.0, rate=100, mdns=False, ssdp=False), protected=known))
    assert not [c for c in fake_net["probed"] if c[0] == PASSIVE]
