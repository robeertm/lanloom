"""Rule-based classifier: a discovered host -> device kind, title and confidence.

Deterministic, no language model and no cloud lookup. Evidence is scored per kind; strongest first:
  * role facts (this host, default gateway),
  * DNS-SD service types announced by the device itself,
  * SSDP/UPnP device types,
  * open service ports (combinations weigh more than single ports),
  * words in the web page title, SSDP model text and host names.
Result: kind, confidence 0..1, the reasons that led there and the runner-up. Confidence >= SURE counts as
"sure", >= UNSURE as "probable"; below that the device stays "unknown" (the user can always override).
"""
import re
from typing import Optional

SURE = 0.6
UNSURE = 0.3

KINDS = ("internet", "router", "switch", "ap", "nas", "server", "computer", "phone", "printer", "tv", "media", "camera",
         "hub", "iot", "service", "broker", "self", "unknown")

# DNS-SD service type -> (kind, weight)
MDNS_RULES = {
    "_ipp._tcp": ("printer", 0.7), "_ipps._tcp": ("printer", 0.7), "_printer._tcp": ("printer", 0.7),
    "_pdl-datastream._tcp": ("printer", 0.7), "_uscan._tcp": ("printer", 0.4), "_scanner._tcp": ("printer", 0.4),
    "_smb._tcp": ("nas", 0.35), "_afpovertcp._tcp": ("nas", 0.35), "_nfs._tcp": ("nas", 0.45), "_webdav._tcp": ("nas", 0.2),
    "_ssh._tcp": ("server", 0.25), "_sftp-ssh._tcp": ("server", 0.25), "_workstation._tcp": ("computer", 0.35),
    "_rfb._tcp": ("computer", 0.35), "_device-info._tcp": ("computer", 0.15),
    "_mqtt._tcp": ("broker", 0.7), "_rtsp._tcp": ("camera", 0.45), "_coap._udp": ("iot", 0.4),
    "_raop._tcp": ("media", 0.5),                 # identifier: DNS-SD type of a common audio streaming receiver
    "_airplay._tcp": ("tv", 0.45),                # identifier: DNS-SD type of a common screen-mirroring receiver
    "_googlecast._tcp": ("tv", 0.55),             # identifier: DNS-SD type of a common cast receiver
    "_spotify-connect._tcp": ("media", 0.45),     # identifier: DNS-SD type of a music streaming receiver
    "_hap._tcp": ("iot", 0.5),                    # identifier: DNS-SD type of a smart-home accessory protocol
    "_home-assistant._tcp": ("hub", 0.9),         # identifier: DNS-SD type announced by a home-automation hub
    "_matter._tcp": ("iot", 0.45),                # identifier: DNS-SD type of a smart-home standard (operational)
    "_matterc._udp": ("iot", 0.45),               # identifier: DNS-SD type of a smart-home standard (commissioning)
    "_meshcop._udp": ("hub", 0.45),               # identifier: DNS-SD type of a low-power mesh border router
    "_esphomelib._tcp": ("iot", 0.6),             # identifier: DNS-SD type of a firmware for small sensor boards
    "_companion-link._tcp": ("phone", 0.3),       # identifier: DNS-SD type of a phone/tablet companion service
}

# SSDP/UPnP device-type fragments -> (kind, weight)
SSDP_RULES = (
    ("internetgatewaydevice", "router", 0.8), ("wanconnectiondevice", "router", 0.6), ("wandevice", "router", 0.5),
    ("wlanaccesspointdevice", "ap", 0.7), ("printer", "printer", 0.6), ("mediarenderer", "media", 0.45),
    ("mediaserver", "nas", 0.3), ("dial-multiscreen", "tv", 0.55), ("digitalsecuritycamera", "camera", 0.7),
    ("zoneplayer", "media", 0.5), ("hvac", "iot", 0.4), ("lighting", "iot", 0.4), ("binarylight", "iot", 0.5),
    ("dimmablelight", "iot", 0.5), ("sensormanagement", "iot", 0.4),
)

# single open ports -> (kind, weight); combinations below
PORT_RULES = {
    631: ("printer", 0.45), 9100: ("printer", 0.55), 515: ("printer", 0.4),
    554: ("camera", 0.5), 1883: ("broker", 0.6), 8883: ("broker", 0.5),
    8123: ("hub", 0.55),        # identifier: default port of a common home-automation hub
    32400: ("media", 0.45),     # identifier: default port of a common media server
    3389: ("computer", 0.5), 5900: ("computer", 0.3), 548: ("nas", 0.35), 2049: ("nas", 0.4), 873: ("nas", 0.25),
    8008: ("tv", 0.3), 8009: ("tv", 0.35), 1400: ("media", 0.35), 161: ("switch", 0.15), 23: ("switch", 0.1),
}

PORT_COMBOS = (
    ({445, 5000}, "nas", 0.45), ({445, 5001}, "nas", 0.5), ({139, 445, 3389}, "computer", 0.35),
    ({53, 80}, "router", 0.35), ({53, 443}, "router", 0.3), ({22, 80, 443}, "server", 0.25), ({22, 445}, "nas", 0.2),
)

# neutral words in titles / names / model strings -> (kind, weight)
WORD_RULES = (
    (r"\b(router|gateway|modem|firewall)\b", "router", 0.45), (r"\bswitch\b", "switch", 0.45),
    (r"\b(access ?point|wlan|wireless|mesh node)\b", "ap", 0.4), (r"\b(nas|storage|file ?server)\b", "nas", 0.45),
    (r"\b(printer|laser|inkjet|mfp|print)\b", "printer", 0.45), (r"\b(camera|cam|nvr|doorbell)\b", "camera", 0.45),
    (r"\b(tv|television|display|projector)\b", "tv", 0.4), (r"\b(speaker|soundbar|receiver|audio|media player|music)\b", "media", 0.4),
    (r"\b(laptop|notebook|desktop|workstation|pc)\b", "computer", 0.4), (r"\b(phone|tablet|handset)\b", "phone", 0.4),
    (r"\b(plug|socket|bulb|lamp|light|thermostat|sensor|relay|blind|shutter|vacuum)\b", "iot", 0.4),
    (r"\b(hub|bridge|home automation|controller)\b", "hub", 0.3), (r"\b(server|hypervisor)\b", "server", 0.35),
    (r"\b(broker|mqtt)\b", "broker", 0.45), (r"\b(ups|power supply)\b", "iot", 0.25),
)
_WORD_RX = [(re.compile(p, re.I), k, w) for p, k, w in WORD_RULES]

WEB_PORTS = {80, 81, 443, 3000, 5000, 5001, 8080, 8081, 8443, 9000}


def _add(scores: dict, reasons: dict, kind: str, weight: float, why: str) -> None:
    # independent evidence combines like probabilities: 1 - (1-a)(1-b)
    scores[kind] = 1 - (1 - scores.get(kind, 0.0)) * (1 - weight)
    reasons.setdefault(kind, []).append(why)


def _clean_host(name: str) -> str:
    name = (name or "").strip().rstrip(".")
    for suffix in (".local", ".lan", ".home", ".localdomain", ".home.arpa", ".internal"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
    return name


def classify(host: dict) -> dict:
    """Return {"kind","confidence","certainty","reasons","runner_up","title","subtitle"} for one ScanResult host."""
    scores, reasons = {}, {}
    ports = {p.get("port") for p in host.get("ports") or [] if isinstance(p.get("port"), int)}

    if host.get("is_self"):
        _add(scores, reasons, "self", 1.0, "role:self")
    if host.get("is_gateway"):
        _add(scores, reasons, "router", 0.85, "role:gateway")

    for svc in host.get("mdns") or []:
        rule = MDNS_RULES.get((svc.get("type") or "").lower())
        if rule:
            _add(scores, reasons, rule[0], rule[1], "mdns:" + svc["type"])

    texts = []
    for dev in host.get("ssdp") or []:
        st = " ".join(str(dev.get(k) or "") for k in ("st", "device_type")).lower()
        for fragment, kind, weight in SSDP_RULES:
            if fragment in st:
                _add(scores, reasons, kind, weight, "ssdp:" + fragment)
                break
        texts += [dev.get("friendly_name"), dev.get("model")]

    for port in sorted(ports):
        rule = PORT_RULES.get(port)
        if rule:
            _add(scores, reasons, rule[0], rule[1], "port:%d" % port)
    for combo, kind, weight in PORT_COMBOS:
        if combo <= ports:
            _add(scores, reasons, kind, weight, "ports:" + "+".join(str(p) for p in sorted(combo)))

    for p in host.get("ports") or []:
        banner = p.get("banner") or {}
        texts += [banner.get("title"), banner.get("server")]
    texts += [_clean_host(h) for h in host.get("hostnames") or []]
    texts += [svc.get("name") for svc in host.get("mdns") or []]
    blob = " ".join(t for t in texts if t)[:2000]
    if blob:
        seen = set()
        for rx, kind, weight in _WORD_RX:
            if kind not in seen and rx.search(blob.replace("-", " ").replace("_", " ")):
                seen.add(kind)
                _add(scores, reasons, kind, weight, "word:" + kind)

    if not scores and ports:
        if ports & WEB_PORTS and len(ports) <= 2:
            _add(scores, reasons, "iot", 0.2, "ports:web-only")
        elif 22 in ports:
            _add(scores, reasons, "server", 0.25, "port:22")

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    kind, conf = ranked[0] if ranked else ("unknown", 0.0)
    runner = ranked[1] if len(ranked) > 1 else None
    if host.get("is_self"):
        kind, conf = "self", 1.0
    elif conf < UNSURE:
        kind = "unknown"
    certainty = "sure" if conf >= SURE and (not runner or conf - runner[1] >= 0.08) else ("probable" if conf >= UNSURE else "unknown")
    title, subtitle = make_title(host, kind)
    return {
        "kind": kind, "confidence": round(conf, 3), "certainty": certainty,
        "reasons": reasons.get(kind, [])[:6],
        "runner_up": {"kind": runner[0], "confidence": round(runner[1], 3)} if runner else None,
        "title": title, "subtitle": subtitle,
    }


def pretty_host(name: str) -> str:
    """'living-room-tv' -> 'Living room tv'; names with dots, digits only or mixed case stay as they are."""
    if not name or "." in name or not re.fullmatch(r"[a-z0-9_-]+", name) or not re.search(r"[a-z]", name):
        return name
    words = re.split(r"[-_]+", name)
    text = " ".join(w for w in words if w)
    return text[:1].upper() + text[1:]


def make_title(host: dict, kind: str) -> tuple:
    """Best human name: SSDP friendly name > DNS-SD instance > host name > web title > neutral fallback."""
    candidates = []
    for dev in host.get("ssdp") or []:
        candidates.append(dev.get("friendly_name"))
    for svc in host.get("mdns") or []:
        candidates.append(svc.get("name"))
    candidates += [pretty_host(_clean_host(h)) for h in host.get("hostnames") or []]
    for p in host.get("ports") or []:
        candidates.append((p.get("banner") or {}).get("title"))
    for c in candidates:
        c = re.sub(r"\s+", " ", (c or "")).strip()
        if c and not re.fullmatch(r"[\d.:]+", c) and len(c) <= 80 and c.lower() not in ("index", "login", "home", "welcome"):
            return c[:48], host.get("ip") or ""
    return "", host.get("ip") or ""


def device_id(host: dict) -> str:
    """Stable identity: MAC address when known (survives address changes), else the IP address."""
    mac = (host.get("mac") or "").lower()
    if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac) and mac != "00:00:00:00:00:00":
        return "d-" + mac.replace(":", "")
    return "d-ip-" + (host.get("ip") or "").replace(".", "-").replace(":", "-")


def services_of(host: dict) -> list:
    """Short service labels for badges, most telling first."""
    order = ("mqtt", "mqtts", "ipp", "printer-raw", "rtsp", "smb", "ssh", "https", "http", "dns", "remote-desktop", "vnc",
             "media-server", "upnp", "web-alt", "web-alt-tls", "http-alt", "https-alt")
    names = {p.get("service") for p in host.get("ports") or [] if p.get("service")}
    ranked = [n for n in order if n in names] + sorted(n for n in names if n not in order)
    labels = {"printer-raw": "RAW", "remote-desktop": "RDP", "media-server": "MEDIA", "web-alt": "WEB", "web-alt-tls": "WEB",
              "http-alt": "HTTP", "https-alt": "HTTPS"}
    out = []
    for n in ranked:
        lab = labels.get(n, n.upper())
        if lab not in out:
            out.append(lab)
    return out
