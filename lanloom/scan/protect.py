"""Protection list: devices that must not be poked by an active scan.

Some devices react badly to unsolicited connections: a printer may print whatever reaches its raw port, a controller of
a machine or a building may stop, and a medical or payment device must not be touched by a stranger at all. The scan
therefore treats three classes with fixed rules (not configurable, only the class of one device can be changed):

``printer``
    Never touched on the raw printing ports (9100-9102) and the line printer port (515): no connection, not even one
    that is closed right away. Everything else (web page, IPP) is as for any device.
``industrial``
    Gentle scan: only the web ports 80 and 443 and no name datagrams. The industrial protocol ports (102, 502, 20000,
    44818, 47808) are probed only by a deep scan with the user's consent, and only on hosts that are not protected.
``passive``
    Medical and payment systems and everything the user marks: no packet is sent to the device at all (no port probe,
    no banner, no name or SNMP datagram). What the device announces on its own (neighbour table, mDNS, SSDP,
    WS-Discovery) is still read.

How a class is chosen, strongest first: the user's mark on the component card (``printer``, ``industrial``,
``passive`` or ``none`` to switch the automatic class off), then what the device announced or what an earlier scan saw
(open ports, DNS-SD types, UPnP text). An automatic class is sticky: a later scan that sees nothing does not clear it.
"""
from __future__ import annotations

CLASSES = ("printer", "industrial", "passive")
MARKS = CLASSES + ("none",)

PRINTER_RAW = frozenset({9100, 9101, 9102, 515})
INDUSTRIAL_PORTS = frozenset({102, 502, 20000, 44818, 47808})
MEDICAL_PORTS = frozenset({104, 11112, 2575})
SENSITIVE_PORTS = INDUSTRIAL_PORTS | MEDICAL_PORTS
INDUSTRIAL_WEB = frozenset({80, 443})                  # the only ports a gentle scan opens on an industrial host

_PRINTER_PORTS = frozenset({631}) | PRINTER_RAW
_PRINTER_MDNS = ("_ipp._tcp", "_ipps._tcp", "_printer._tcp", "_pdl-datastream._tcp", "_scanner._tcp")
_MEDICAL_MDNS = ("_dicom", "_hl7", "_mllp", "_pos-")
_INDUSTRIAL_MDNS = ("_modbus", "_bacnet", "_opcua", "_s7")
_PRINTER_WORDS = ("printer", "mfp", "laserjet", "inkjet")
_UNIQUE_INDUSTRIAL = frozenset({102, 502, 44818, 47808})   # 20000 alone is not enough: it is also a plain web port


def _types(data: dict) -> list:
    return [str(m.get("type", "")).lower() for m in data.get("mdns") or [] if isinstance(m, dict)]


def _open(data: dict) -> set:
    return {p.get("port") for p in data.get("ports") or [] if isinstance(p, dict) and isinstance(p.get("port"), int)}


def auto_class(data: dict):
    """(class or None, reason key) from what is known about a host (a stored device record or a scan host)."""
    ports, types = _open(data), _types(data)
    if ports & MEDICAL_PORTS or any(t.startswith(_MEDICAL_MDNS) for t in types):
        return "passive", "protect.why.medical"
    if ports & _UNIQUE_INDUSTRIAL or any(t.startswith(_INDUSTRIAL_MDNS) for t in types):
        return "industrial", "protect.why.industrial"
    ssdp = " ".join(str(v) for s in data.get("ssdp") or [] if isinstance(s, dict) for v in s.values()).lower()
    if ports & _PRINTER_PORTS or any(t in _PRINTER_MDNS for t in types) or any(w in ssdp for w in _PRINTER_WORDS):
        return "printer", "protect.why.printer"
    return None, None


def resolve(data: dict, mark=None):
    """(class or None, reason key, source) with the user's mark applied. ``source`` is "mark", "auto" or None."""
    if mark == "none":
        return None, None, None
    if mark in CLASSES:
        return mark, "protect.why.mark", "mark"
    klass, why = auto_class(data)
    stored = data.get("protect") if isinstance(data.get("protect"), dict) else {}
    if klass is None and stored.get("class") in CLASSES and stored.get("source") == "auto":
        return stored["class"], stored.get("why") or "protect.why.earlier", "auto"       # sticky
    return (klass, why, "auto") if klass else (None, None, None)


class Shield:
    """Decides, for one scan, which packets may be sent to which host. Seeded with the classes the inventory already
    knows (``known``: {ip: {"class", "why", "source"}}), refined with what this scan sees on the way."""

    def __init__(self, known: dict | None = None, deep: bool = False):
        self.deep = bool(deep)
        self._cls: dict = {}
        for ip, entry in (known or {}).items():
            if isinstance(entry, dict) and entry.get("class") in CLASSES:
                self._cls[ip] = (entry["class"], entry.get("why"), entry.get("source") or "auto")
        self._seen_mark = {ip for ip, e in (known or {}).items() if isinstance(e, dict) and e.get("source") == "mark"}
        self._refused_none = {ip for ip, e in (known or {}).items() if isinstance(e, dict) and e.get("source") == "none"}

    def klass(self, ip):
        e = self._cls.get(ip)
        return e[0] if e else None

    def observe(self, ip: str, data: dict) -> None:
        """Take what the quick phases (mDNS, SSDP, WS-Discovery, an earlier scan) say about a host."""
        if ip in self._seen_mark or ip in self._refused_none:
            return
        klass, why = auto_class(data)
        if klass and self._rank(klass) > self._rank(self.klass(ip)):
            self._cls[ip] = (klass, why, "auto")

    def note_open(self, ip: str, port: int) -> None:
        """A port that answered during this scan: it may change the class of the host for the rest of the scan."""
        self.observe(ip, {"ports": [{"port": port}]})

    @staticmethod
    def _rank(klass) -> int:
        return {None: 0, "printer": 1, "industrial": 2, "passive": 3}[klass]

    def allow_port(self, ip: str, port: int) -> bool:
        klass = self.klass(ip)
        if klass == "passive":
            return False
        if klass == "industrial":
            return port in INDUSTRIAL_WEB
        if klass == "printer" and port in PRINTER_RAW:
            return False
        if port in SENSITIVE_PORTS and not self.deep:
            return False
        return True

    def allow_banner(self, ip: str, port: int) -> bool:
        return self.klass(ip) != "passive" and (self.klass(ip) != "industrial" or port in INDUSTRIAL_WEB)

    def allow_datagram(self, ip: str) -> bool:
        return self.klass(ip) not in ("passive", "industrial")

    def result(self, ip: str):
        """The class to store with a scanned host: {"class", "why", "source"} or None."""
        e = self._cls.get(ip)
        return {"class": e[0], "why": e[1], "source": e[2]} if e else None

    def skipped(self, ips) -> int:
        return sum(1 for ip in ips if self.klass(ip))
