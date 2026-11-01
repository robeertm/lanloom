import asyncio
import http.server
import ipaddress
import socket
import threading

import pytest

from lanloom.scan import ssdp

DESC = b"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>Living room media player</friendlyName>
    <manufacturer>Example Corp</manufacturer>
    <modelName>MP-100</modelName>
    <deviceList><device><friendlyName>Inner</friendlyName></device></deviceList>
  </device>
</root>"""


def test_msearch_format():
    pkt = ssdp.msearch()
    assert pkt.startswith(b"M-SEARCH * HTTP/1.1\r\n")
    assert b"MAN: \"ssdp:discover\"" in pkt and b"MX: 2" in pkt and b"ST: ssdp:all" in pkt
    assert pkt.endswith(b"\r\n\r\n")


def test_parse_response_headers_case_insensitive():
    raw = (b"HTTP/1.1 200 OK\r\nCache-Control: max-age=1800\r\nlocation: http://192.168.1.5:1400/d.xml\r\n"
           b"SERVER: ExampleOS/1.0 UPnP/1.0\r\nst: upnp:rootdevice\r\nUSN: uuid:1::upnp:rootdevice\r\n\r\n")
    h = ssdp.parse_response(raw)
    assert h["location"] == "http://192.168.1.5:1400/d.xml"
    assert h["st"] == "upnp:rootdevice" and h["server"].startswith("ExampleOS")


@pytest.mark.parametrize("raw", [b"", b"garbage", b"HTTP/1.1 404 Not Found\r\nST: x\r\n\r\n",
                                 b"HTTP/1.1 200 OK\r\nLOCATION: x\r\n\r\n", b"M-SEARCH * HTTP/1.1\r\n\r\n",
                                 b"HTTP/1.1 200 OK\r\n" + b"X: y\r\n" * 5000, b"\xff\xfe\x00" * 50])
def test_parse_response_rejects(raw):
    assert ssdp.parse_response(raw) is None


def test_parse_notify():
    h = ssdp.parse_response(b"NOTIFY * HTTP/1.1\r\nNT: upnp:rootdevice\r\nNTS: ssdp:alive\r\n\r\n")
    assert h["nt"] == "upnp:rootdevice"


def test_description_parse():
    d = ssdp.parse_description(DESC)
    assert d == {"friendly_name": "Living room media player", "manufacturer": "Example Corp",
                 "model": "MP-100", "device_type": "urn:schemas-upnp-org:device:MediaRenderer:1"}


@pytest.mark.parametrize("raw", [
    b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]><root>&lol2;</root>',
    b'<?xml version="1.0"?><!doctype root SYSTEM "http://192.0.2.1/x.dtd"><root/>',
    b'<root><!ENTITY x SYSTEM "file:///etc/passwd"></root>',
    b'<root><device><friendlyName>unterminated',
    b'',
    b'<root>' + b'a' * (70 * 1024) + b'</root>',
    b'<?xml version="1.0"?><root><nodevice/></root>',
])
def test_xml_hardening(raw):
    assert ssdp.parse_description(raw) is None


def test_description_fields_sanitized():
    raw = DESC.replace(b"Living room media player", b"Bad\t\n  name " + b"x" * 500)
    d = ssdp.parse_description(raw)
    assert d["friendly_name"].startswith("Bad name x") and len(d["friendly_name"]) <= ssdp.MAX_FIELD
    # raw control characters are invalid XML and the document is refused as a whole
    assert ssdp.parse_description(DESC.replace(b"Living", b"\x1b[31mLiving")) is None


def test_location_allowed():
    nets = [ipaddress.ip_network("192.168.1.0/24")]
    assert ssdp.location_allowed("http://192.168.1.5:1400/d.xml", "192.168.1.5", nets) == \
        ("192.168.1.5", 1400, "/d.xml", False)
    assert ssdp.location_allowed("http://192.168.1.5/d.xml?a=1", "192.168.1.5", nets)[2] == "/d.xml?a=1"
    assert ssdp.location_allowed("http://8.8.8.8/d.xml", "8.8.8.8", nets) is None           # public
    assert ssdp.location_allowed("http://192.168.1.6/d.xml", "192.168.1.5", nets) is None   # other host
    assert ssdp.location_allowed("http://device.local/d.xml", "192.168.1.5", nets) is None  # names
    assert ssdp.location_allowed("file:///etc/passwd", "192.168.1.5", nets) is None
    assert ssdp.location_allowed("http://192.168.1.5:99999/", "192.168.1.5", nets) is None


def test_st_rank_grouping_order():
    assert ssdp._st_rank("urn:schemas-upnp-org:device:MediaRenderer:1") < ssdp._st_rank("upnp:rootdevice")
    assert ssdp._st_rank("upnp:rootdevice") < ssdp._st_rank("uuid:1") < ssdp._st_rank(
        "urn:schemas-upnp-org:service:AVTransport:1")


def test_search_against_localhost_fakes():
    """Fake SSDP responder + description server on 127.0.0.1; one device, grouped into one entry."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = DESC if self.path == "/desc.xml" else b""
            self.send_response(200 if body else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
    hport = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    udp.settimeout(0.2)
    uport = udp.getsockname()[1]
    stop = threading.Event()

    def responder():
        while not stop.is_set():
            try:
                data, addr = udp.recvfrom(4096)
            except OSError:
                continue
            if not data.startswith(b"M-SEARCH"):
                continue
            for st in ("upnp:rootdevice", "uuid:1", "urn:schemas-upnp-org:device:MediaRenderer:1"):
                udp.sendto((f"HTTP/1.1 200 OK\r\nLOCATION: http://127.0.0.1:{hport}/desc.xml\r\n"
                            f"SERVER: ExampleOS/1.0\r\nST: {st}\r\n\r\n").encode(), addr)
            udp.sendto(b"\x00\x01garbage", addr)

    th = threading.Thread(target=responder, daemon=True)
    th.start()
    try:
        res, sent = asyncio.run(ssdp.search(0.8, networks=[ipaddress.ip_network("127.0.0.0/8")],
                                            target=("127.0.0.1", uport)))
    finally:
        stop.set()
        th.join(2)
        udp.close()
        httpd.shutdown()
        httpd.server_close()
    assert sent >= 2
    entries = res["127.0.0.1"]
    assert len(entries) == 1
    e = entries[0]
    assert e["st"].endswith("MediaRenderer:1")
    assert e["friendly_name"] == "Living room media player" and e["model"] == "MP-100"
    assert e["manufacturer"] == "Example Corp" and e["server"] == "ExampleOS/1.0"


def test_search_out_of_scope_ignored():
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    udp.settimeout(0.2)
    uport = udp.getsockname()[1]
    stop = threading.Event()

    def responder():
        while not stop.is_set():
            try:
                _, addr = udp.recvfrom(4096)
            except OSError:
                continue
            udp.sendto(b"HTTP/1.1 200 OK\r\nST: upnp:rootdevice\r\nLOCATION: http://127.0.0.1:1/x\r\n\r\n", addr)

    th = threading.Thread(target=responder, daemon=True)
    th.start()
    try:
        res, _ = asyncio.run(ssdp.search(0.5, networks=[ipaddress.ip_network("192.168.1.0/24")],
                                         target=("127.0.0.1", uport)))
    finally:
        stop.set()
        th.join(2)
        udp.close()
    assert res == {}
