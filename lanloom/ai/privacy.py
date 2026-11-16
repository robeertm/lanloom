"""Privacy layer for public AI services: placeholders out, real values back in.

Before a text goes to a public service, every IP address, MAC address, host name, wireless network name, serial
number and location in it is replaced by a placeholder such as ``[IP-1]`` or ``[HOST-2]``. The same value always
gets the same placeholder within one request, so the model can still tell devices apart. The answer is translated
back before anyone sees it. A final check refuses to send when an address pattern survived (fail closed).

Two sources feed the replacement:

* **known values** from Lanloom's own data (device names, host names, mDNS names, wireless network names, serial
  numbers, site names) - replaced wherever they appear, longest first, case-insensitive;
* **patterns** for what Lanloom may not know (any IPv4/IPv6/MAC, names under private suffixes such as ``.local`` or
  ``.lan``, ``serial: …`` notes, coordinates).

A local AI service gets the real text: it stays inside the network.
"""
import ipaddress
import re
from typing import Iterable

KINDS = ("IP", "MAC", "HOST", "SSID", "SERIAL", "PLACE")

MAC_RX = re.compile(r"(?<![0-9A-Fa-f:.-])(?:[0-9A-Fa-f]{2}([:-]))(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}(?![0-9A-Fa-f])(?![:.-][0-9A-Fa-f])"
                    r"|(?<![0-9A-Fa-f.])[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}(?![0-9A-Fa-f])(?!\.[0-9A-Fa-f])")
IPV4_RX = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?![\d])(?!\.\d)")
IPV6_CAND = re.compile(r"(?<![0-9A-Za-z:])[0-9A-Fa-f:]*:[0-9A-Fa-f:]*:[0-9A-Fa-f:.]*(?:%[0-9A-Za-z]+)?(?:/\d{1,3})?(?![0-9A-Za-z:])")
PRIVATE_SUFFIX = r"(?:local|lan|home\.arpa|home|internal|intranet|localdomain|box|corp|private|localnet)"
HOSTNAME_RX = re.compile(r"(?<![\w.-])[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9-]{1,63})*\.%s(?![\w-])" % PRIVATE_SUFFIX,
                         re.I)
SERIAL_RX = re.compile(r"(?i)\b(serial(?:\s*(?:no\.?|number))?|s/n|sn)(\s*[:#=]?\s*)([A-Z0-9][A-Z0-9-]{4,39})\b")
COORD_RX = re.compile(r"(?<![\d.])-?\d{1,2}\.\d{3,}\s*,\s*-?\d{1,3}\.\d{3,}(?![\d.])")
PLACEHOLDER_RX = re.compile(r"\[?\b(IP|MAC|HOST|SSID|SERIAL|PLACE)[-_ ](\d{1,4})\b\]?")
WORD_CHARS = r"[\w]"
SAFE_LINE = re.compile(r"^(Subject|Kind|Status|Role|Identification|Level|Action|Protocol|Enabled|Built in|Scheduled|Connection states): "
                       r"[A-Za-z0-9 ,()./:'-]*$")


def _is_ip6(token: str) -> bool:
    core = token.split("/")[0].split("%")[0]
    if core.count(":") < 2:
        return False
    try:
        ipaddress.IPv6Address(core)
        return True
    except ValueError:
        return False


def _is_ip4(token: str) -> bool:
    try:
        ipaddress.IPv4Address(token.split("/")[0])
        return True
    except ValueError:
        return False


def _ip6_core(token: str) -> str:
    """The address inside a candidate: a sentence's full stop or a colon behind it ("...::1: refused") is not part of it."""
    for cand in (token, token.rstrip("."), token.rstrip(".:"), token.rstrip(":")):
        if cand and _is_ip6(cand):
            return cand
    return ""


class Pseudonymizer:
    """One request's mapping. ``out(text)`` replaces, ``back(text)`` restores, ``mapping()`` lists what was hidden
    (placeholder -> kind, never the value, for the preview's legend)."""

    def __init__(self, known: dict = None):
        self._fwd = {}            # (kind, normalised value) -> placeholder
        self._back = {}           # placeholder -> original value
        self._count = {k: 0 for k in KINDS}
        self._known = []          # (value, kind), longest first
        for kind, values in (known or {}).items():
            if kind not in KINDS:
                continue
            for v in values or ():
                v = str(v or "").strip()
                if len(v) >= 3 and not v.isdigit() and v.lower() not in _TOO_COMMON:
                    self._known.append((v, kind))
        self._known.sort(key=lambda x: -len(x[0]))

    def _ph(self, kind: str, value: str) -> str:
        key = (kind, value.lower())
        ph = self._fwd.get(key)
        if ph is None:
            self._count[kind] += 1
            ph = "[%s-%d]" % (kind, self._count[kind])
            self._fwd[key] = ph
            self._back[ph] = value
        return ph

    def out(self, text: str) -> str:
        text = str(text or "")
        # order matters: MACs before IPv6 (a MAC with colons looks like an address part), names before patterns
        text = MAC_RX.sub(lambda m: self._ph("MAC", m.group(0)), text)
        if self._known:
            # fixed-vocabulary lines Lanloom writes itself (kind, status, role …) hold no names: a device called
            # "Storage" must not turn "Kind: Network storage" into a placeholder. Address patterns still apply below.
            lines = text.split("\n")
            for i, line in enumerate(lines):
                if SAFE_LINE.match(line):
                    continue
                for value, kind in self._known:
                    rx = re.compile(r"(?<!%s)%s(?!%s)" % (WORD_CHARS, re.escape(value), WORD_CHARS), re.I)
                    line = rx.sub(lambda m, k=kind: self._ph(k, m.group(0)), line)
                lines[i] = line
            text = "\n".join(lines)
        text = IPV4_RX.sub(lambda m: self._ph("IP", m.group(0)) if _is_ip4(m.group(0)) else m.group(0), text)
        text = IPV6_CAND.sub(self._ip6, text)
        text = HOSTNAME_RX.sub(lambda m: self._ph("HOST", m.group(0)), text)
        text = SERIAL_RX.sub(lambda m: m.group(1) + m.group(2) + self._ph("SERIAL", m.group(3)), text)
        text = COORD_RX.sub(lambda m: self._ph("PLACE", m.group(0)), text)
        return text

    def _ip6(self, m) -> str:
        tok = m.group(0)
        core = _ip6_core(tok)
        if core:
            return self._ph("IP", core) + tok[len(core):]
        return tok

    def back(self, text: str) -> str:
        def repl(m):
            ph = "[%s-%s]" % (m.group(1), m.group(2))
            return self._back.get(ph, m.group(0))
        return PLACEHOLDER_RX.sub(repl, str(text or ""))

    def mapping(self) -> list:
        return [{"placeholder": ph, "kind": ph[1:].split("-")[0]} for ph in self._back]


_TOO_COMMON = {"router", "gateway", "switch", "printer", "camera", "phone", "laptop", "server", "default", "unknown",
               "device", "host", "this", "internet", "network", "lan", "wan", "guest", "home", "tv", "nas"}


def leaks(text: str) -> list:
    """Kinds of raw identifiers still present in a text that is about to leave (empty = clean)."""
    found = []
    if MAC_RX.search(text):
        found.append("MAC")
    if any(_is_ip4(m.group(0)) for m in IPV4_RX.finditer(text)):
        found.append("IP")
    if any(_ip6_core(m.group(0)) for m in IPV6_CAND.finditer(text)):
        found.append("IP")
    if HOSTNAME_RX.search(text):
        found.append("HOST")
    return sorted(set(found))


def known_values(core) -> dict:
    """Identifiers Lanloom knows from its own data, by kind (never sent anywhere; only used to hide them)."""
    out = {k: set() for k in KINDS}
    try:
        devices = core.devices()
    except Exception:
        devices = []
    for d in devices:
        for name in [d.get("title"), d.get("auto_title")] + list(d.get("hostnames") or []):
            if name:
                out["HOST"].add(str(name))
        for m in d.get("mdns") or []:
            if isinstance(m, dict):
                for k in ("name", "host", "instance"):
                    if m.get(k):
                        out["HOST"].add(str(m[k]).rstrip("."))
        if d.get("ip"):
            out["IP"].add(str(d["ip"]))
        if d.get("mac"):
            out["MAC"].add(str(d["mac"]))
    try:
        integs = core.integrations()
    except Exception:
        integs = []
    for i in integs:
        if i.get("name"):
            out["HOST"].add(str(i["name"]))
        site = (i.get("config") or {}).get("site")
        if site:
            out["PLACE"].add(str(site))
    try:
        live = core.live_configs() if callable(getattr(core, "live_configs", None)) else []
    except Exception:
        live = []
    for entry in live or []:
        st = (entry or {}).get("state") or {}
        for s in st.get("ssids") or st.get("wireless") or []:
            if isinstance(s, dict) and s.get("name"):
                out["SSID"].add(str(s["name"]))
        for dev in st.get("devices") or []:
            if isinstance(dev, dict):
                if dev.get("serial"):
                    out["SERIAL"].add(str(dev["serial"]))
                if dev.get("name"):
                    out["HOST"].add(str(dev["name"]))
    return {k: sorted(v) for k, v in out.items() if v}


def add_known(known: dict, kind: str, values: Iterable) -> dict:
    known = {k: list(v) for k, v in (known or {}).items()}
    known.setdefault(kind, [])
    known[kind].extend(str(v) for v in values if v)
    return known
