import asyncio
import ipaddress
import socket
import threading

from lanloom.scan import dnsmsg, mdns
from lanloom.scan.dnsmsg import T_A, T_PTR, T_SRV, T_TXT

NETS = [ipaddress.ip_network("192.168.1.0/24")]


def _resp(answers, additionals=()):
    return dnsmsg.build_response(list(answers), list(additionals))


def test_collector_full_chain():
    col = mdns.MdnsCollector(types=())
    qs = col.next_questions()
    assert (mdns.SERVICES_META, T_PTR) in qs
    col.feed(_resp([dnsmsg.build_record(mdns.SERVICES_META, T_PTR, dnsmsg.rdata_ptr("_ipp._tcp.local"))]),
             ("192.168.1.30", 5353))
    qs = col.next_questions()
    assert ("_ipp._tcp.local", T_PTR) in qs
    inst = "Office printer._ipp._tcp.local"
    col.feed(_resp([dnsmsg.build_record("_ipp._tcp.local", T_PTR, dnsmsg.rdata_ptr(inst))]), ("192.168.1.30", 5353))
    qs = col.next_questions()
    assert (inst, T_SRV) in qs and (inst, T_TXT) in qs
    col.feed(_resp([dnsmsg.build_record(inst, T_SRV, dnsmsg.rdata_srv(631, "printer-7.local"))]),
             ("192.168.1.30", 5353))
    qs = col.next_questions()
    assert ("printer-7.local", T_A) in qs
    col.feed(_resp([dnsmsg.build_record("printer-7.local", T_A, dnsmsg.rdata_a("192.168.1.30")),
                    dnsmsg.build_record(inst, T_TXT, dnsmsg.rdata_txt({"rp": "ipp/print"}))]),
             ("192.168.1.30", 5353))
    res = col.results(NETS)
    assert set(res) == {"192.168.1.30"}
    svc = res["192.168.1.30"]["services"]
    assert svc == [{"type": "_ipp._tcp", "name": "Office printer", "port": 631, "txt": {"rp": "ipp/print"}}]
    assert "printer-7.local" in res["192.168.1.30"]["hostnames"]


def test_out_of_scope_answers_dropped():
    col = mdns.MdnsCollector(types=())
    inst = "Far away._http._tcp.local"
    col.feed(_resp([dnsmsg.build_record("_http._tcp.local", T_PTR, dnsmsg.rdata_ptr(inst))],
                   [dnsmsg.build_record(inst, T_SRV, dnsmsg.rdata_srv(80, "far.local")),
                    dnsmsg.build_record("far.local", T_A, dnsmsg.rdata_a("10.9.9.9"))]),
             ("10.9.9.9", 5353))
    assert col.results(NETS) == {}


def test_queries_and_garbage_ignored():
    col = mdns.MdnsCollector(types=())
    col.feed(dnsmsg.build_query([("_http._tcp.local", T_PTR)]), ("192.168.1.2", 5353))
    col.feed(b"\xff" * 40, ("192.168.1.2", 5353))
    assert col.instances == {} and col.bad_packets == 1


def test_caps_hold():
    col = mdns.MdnsCollector(types=())
    for i in range(mdns.MAX_TYPES + 50):
        col.feed(_resp([dnsmsg.build_record(mdns.SERVICES_META, T_PTR, dnsmsg.rdata_ptr(f"_t{i}._tcp.local"))]),
                 ("192.168.1.2", 5353))
    assert len(col.types) <= mdns.MAX_TYPES


def test_browse_against_localhost_fake_responder():
    """A fake responder on 127.0.0.1 answers unicast; browse() must resolve the full chain."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    srv.settimeout(0.2)
    port = srv.getsockname()[1]
    stop = threading.Event()
    inst = "Test panel._http._tcp.local"

    def responder():
        while not stop.is_set():
            try:
                data, addr = srv.recvfrom(9000)
            except OSError:
                continue
            try:
                q = dnsmsg.parse_message(data)
            except dnsmsg.DNSError:
                continue
            for question in q["questions"]:
                if question["name"] == "_http._tcp.local" and question["type"] == T_PTR:
                    pkt = _resp([dnsmsg.build_record("_http._tcp.local", T_PTR, dnsmsg.rdata_ptr(inst))],
                                [dnsmsg.build_record(inst, T_SRV, dnsmsg.rdata_srv(80, "panel.local")),
                                 dnsmsg.build_record(inst, T_TXT, dnsmsg.rdata_txt({"path": "/"})),
                                 dnsmsg.build_record("panel.local", T_A, dnsmsg.rdata_a("127.0.0.1"))])
                    srv.sendto(pkt, addr)
                    srv.sendto(b"\x00garbage", addr)

    th = threading.Thread(target=responder, daemon=True)
    th.start()
    try:
        res, sent = asyncio.run(mdns.browse(0.8, networks=[ipaddress.ip_network("127.0.0.0/8")],
                                            target=("127.0.0.1", port), join=False))
    finally:
        stop.set()
        th.join(2)
        srv.close()
    assert sent >= 1
    assert res["127.0.0.1"]["services"][0]["name"] == "Test panel"
    assert res["127.0.0.1"]["services"][0]["port"] == 80
    assert "panel.local" in res["127.0.0.1"]["hostnames"]
