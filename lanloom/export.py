"""Exports for documentation, insurance and moving house: the inventory as CSV and JSON, and the import of own fields.

* ``rows(core)``            one record per device: the found facts and the own fields of the inventory (inventory.py), the
                            network it sits in, the device and port it hangs on ("connected via"), dates as ISO text.
* ``Redactor``              the *redact* option: hardware addresses, IP addresses, names (and everything a person typed:
                            note, purpose, shop, serial number, person) are replaced by stable pseudonyms, so that a file can be
                            attached to an issue without telling anybody what the network looks like. The same object is used for
                            the network plan (export_plan.py), so a plan and a list of the same download match.
* ``to_csv`` / ``to_json``  the files (a cell that a spreadsheet would run as a formula is neutralised on the way out and restored
                            on the way in, so the round trip is exact).
* ``parse`` / ``apply``     the import of the own columns (name, purpose, room, person, shop, note, serial number, dates, own
                            firmware, "connected via" with port and medium). A row is matched by ``id``, else by hardware address,
                            else by IP address; an empty cell leaves the value as it is; a row with a wrong value is skipped as a whole
                            and reported; ``dry_run`` only reports. A redacted file is refused.

Nothing here talks to the network and nothing is written outside the inventory tables.
"""
from __future__ import annotations

import csv
import datetime
import io
import ipaddress
import json
import re
from typing import Optional

from . import inventory as inv_mod

FORMAT = "lanloom-inventory/1"
MAX_ROWS = 5000
MAX_BYTES = 2_000_000
EXPORT_COLUMNS = ("id", "name", "kind", "ip", "mac", "network", "vendor", "model", "firmware", "firmware_own", "serial", "serial_reported",
                  "purpose", "room", "owner", "shop", "note", "purchase_date", "warranty_until", "eol_until", "state", "connected_via",
                  "connected_via_id", "port", "medium", "first_seen", "last_seen")
# columns the import reads (everything else in a file is ignored: found facts are never written)
IMPORT_COLUMNS = ("name", "purpose", "room", "owner", "shop", "note", "serial", "purchase_date", "warranty_until", "eol_until",
                  "firmware_own", "connected_via_id", "connected_via", "port", "medium")
_FORMULA = ("=", "+", "-", "@", "\t", "\r")
_IP_RX = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _iso(ts) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else ""
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


# ---------------------------------------------------------------------------------------------------- redaction
class Redactor:
    """Stable pseudonyms within one export: the same device always gets the same name, address and hardware address.
    Addresses of one network stay in one pseudonym network (10.N.x.y), so the structure of the plan is still readable."""

    def __init__(self, nets: Optional[list] = None):
        self._ids, self._names, self._ips, self._macs, self._nets, self._people, self._rooms = {}, {}, {}, {}, {}, {}, {}
        self._kinds: dict = {}
        self._count: dict = {}
        self._known = [n for n in (nets or []) if isinstance(n, dict) and n.get("cidr")]

    def dev(self, device_id: str) -> str:
        return self._ids.setdefault(str(device_id), "dev-%04d" % (len(self._ids) + 1))

    def name(self, device_id: str, kind: str = "") -> str:
        if device_id not in self._names:
            kind = re.sub(r"[^a-z0-9]", "", str(kind or "device").lower()) or "device"
            self._kinds[kind] = self._kinds.get(kind, 0) + 1
            self._names[device_id] = "%s-%d" % (kind, self._kinds[kind])
        return self._names[device_id]

    def _net_index(self, cidr: str) -> int:
        if not cidr:
            return 200
        return self._nets.setdefault(cidr, len(self._nets) + 1)

    def net(self, cidr: str) -> str:
        cidr = str(cidr or "")
        if not cidr:
            return ""
        try:
            bits = ipaddress.ip_network(cidr, strict=False).prefixlen
        except ValueError:
            bits = 24
        return "10.%d.0.0/%d" % (self._net_index(cidr) % 256, bits if ":" not in cidr else 24)

    def ip(self, ip: str, cidr: str = "") -> str:
        ip = str(ip or "")
        if not ip:
            return ""
        if ip not in self._ips:
            k = self._net_index(cidr or _network_of(ip, self._known)) % 256
            c = self._count[k] = self._count.get(k, 0) + 1
            self._ips[ip] = "10.%d.%d.%d" % (k, (c - 1) // 250, (c - 1) % 250 + 1)
        return self._ips[ip]

    def mac(self, mac: str) -> str:
        mac = str(mac or "")
        if not mac:
            return ""
        if mac not in self._macs:
            k = len(self._macs) + 1
            self._macs[mac] = "02:00:00:00:%02x:%02x" % ((k >> 8) & 255, k & 255)
        return self._macs[mac]

    def person(self, who: str) -> str:
        return self._people.setdefault(who, "person-%d" % (len(self._people) + 1)) if who else ""

    def room(self, where: str) -> str:
        return self._rooms.setdefault(where, "room-%d" % (len(self._rooms) + 1)) if where else ""

    def text(self, value: str) -> str:
        """A free text of the network (subtitle of a pad): an address is replaced, anything else is dropped."""
        value = str(value or "")
        return self.ip(value) if _IP_RX.match(value) else ""


def redact_row(row: dict, rd: Redactor) -> dict:
    out = dict(row)
    did, kind = row["id"], row["kind"]
    out["id"] = rd.dev(did)
    out["name"] = rd.name(did, kind)
    out["ip"], out["mac"], out["network"] = rd.ip(row["ip"], row["network"]), rd.mac(row["mac"]), rd.net(row["network"])
    out["serial"] = "REDACTED" if row["serial"] else ""
    out["serial_reported"] = "REDACTED" if row["serial_reported"] else ""
    out["owner"], out["room"] = rd.person(row["owner"]), rd.room(row["room"])
    for k in ("purpose", "shop", "note", "purchase_date", "warranty_until"):
        out[k] = ""
    out["firmware_own"] = ""
    via = row["connected_via_id"]
    out["connected_via_id"] = rd.dev(via) if via else ""
    out["connected_via"] = rd.name(via, row.get("_via_kind") or "") if via else ""
    out["first_seen"], out["last_seen"] = row["first_seen"][:10], row["last_seen"][:10]
    return out


# ---------------------------------------------------------------------------------------------------- rows
def _network_of(ip: str, nets: list) -> str:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ""
    best = None
    for n in nets:
        try:
            net = ipaddress.ip_network(n["cidr"], strict=False)
        except (ValueError, KeyError):
            continue
        if addr in net and (best is None or net.prefixlen > best.prefixlen):
            best = net
    return str(best) if best else ""


def _parents(core, titles: dict) -> dict:
    """device id -> (parent id, port, medium): the resolved links; where none is known, the map's wired line to the gateway side."""
    out = {}
    try:
        for did, ln in ((core.topology() or {}).get("links") or {}).items():
            if ln.get("parent"):
                out[did] = (ln["parent"], ln.get("port") or "", ln.get("medium") or "")
    except Exception:
        pass
    if not out:                                           # nothing resolved (e.g. the sample network): the plan's own lines
        try:
            from . import mapmodel
            for t in mapmodel.cached_build(core, "network")["traces"]:
                if t.get("kind") == "lan" and t["b"] in titles and t["a"] in titles:
                    out.setdefault(t["b"], (t["a"], "", ""))
        except Exception:
            pass
    return out


def _safe_networks(core) -> list:
    try:
        return core.networks()
    except Exception:
        return []


def rows(core, now: Optional[float] = None) -> list:
    """One dict per device (decommissioned ones too: they are history, not gone), in the order of the device list."""
    inv = core.inventory
    items = inv.list({"sort": "title"}, now)["items"]
    nets = _safe_networks(core)
    titles = {i["id"]: (i["title"] or i["ip"] or i["id"]) for i in items}
    kinds = {i["id"]: i["kind"] for i in items}
    parents = _parents(core, titles)
    out = []
    for i in items:
        via, port, medium = parents.get(i["id"], ("", "", ""))
        out.append({
            "id": i["id"], "name": i["title"] or "", "kind": i["kind"], "ip": i["ip"] or "", "mac": i["mac"] or "",
            "network": _network_of(i["ip"] or "", nets), "vendor": i["vendor"] or "", "model": i["model"] or "",
            "firmware": i["firmware"] or "", "firmware_own": i["firmware_own"] or "", "serial": i["serial"] or "",
            "serial_reported": i["serial_reported"] or "", "purpose": i["purpose"], "room": i["room"], "owner": i["owner"],
            "shop": i["shop"], "note": i["note"], "purchase_date": i["purchase_date"], "warranty_until": i["warranty_until"],
            "eol_until": (i["eol"] or {}).get("until") or i["eol_until"] or "", "state": i["state"],
            "connected_via": titles.get(via, "") if via else "", "connected_via_id": via or "", "port": port or "", "medium": medium or "",
            "first_seen": _iso(i["first_seen"]), "last_seen": _iso(i["last_seen"]), "_via_kind": kinds.get(via, "") if via else "",
        })
    return out


def prepare(core, redact: bool, now: Optional[float] = None, rd: Optional[Redactor] = None) -> list:
    data = rows(core, now)
    if redact:
        rd = rd or Redactor(_safe_networks(core))
        data = [redact_row(r, rd) for r in data]
    for r in data:
        r.pop("_via_kind", None)
    return data


# ---------------------------------------------------------------------------------------------------- files
def _cell(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s.startswith(_FORMULA) else s


def _uncell(s: str) -> str:
    return s[1:] if len(s) > 1 and s[0] == "'" and s[1] in "=+-@\t\r" else s


def to_csv(data: list) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    w.writerow(EXPORT_COLUMNS)
    for r in data:
        w.writerow([_cell(r.get(c, "")) for c in EXPORT_COLUMNS])
    return "﻿" + buf.getvalue()           # the byte order mark makes spreadsheets read UTF-8


def to_json(data: list, redact: bool, version: str, now: Optional[float] = None) -> str:
    doc = {"format": FORMAT, "generator": "Lanloom " + version, "redacted": bool(redact), "created": _iso(now) if now else "",
           "columns": list(EXPORT_COLUMNS), "devices": [{c: r.get(c, "") for c in EXPORT_COLUMNS} for r in data]}
    return json.dumps(doc, ensure_ascii=False, indent=1) + "\n"


# ---------------------------------------------------------------------------------------------------- import
def parse(fmt: str, text: str) -> list:
    """The records of a CSV or JSON export, as list of dicts. Raises ValueError(i18n key)."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("export.err.empty")
    if len(text.encode("utf-8", "replace")) > MAX_BYTES:
        raise ValueError("export.err.too_big")
    text = text.lstrip("﻿")
    if fmt == "json":
        try:
            doc = json.loads(text)
        except ValueError:
            raise ValueError("export.err.json")
        if isinstance(doc, dict):
            if doc.get("redacted"):
                raise ValueError("export.err.redacted")
            recs = doc.get("devices")
        else:
            recs = doc
        if not isinstance(recs, list) or not all(isinstance(r, dict) for r in recs):
            raise ValueError("export.err.format")
        out = [{str(k): ("" if v is None else str(v)) for k, v in r.items() if not isinstance(v, (dict, list))} for r in recs]
    elif fmt == "csv":
        head = text.split("\n", 1)[0]
        delim = max(",;\t", key=lambda d: head.count(d))                # a spreadsheet may have written ; or tab; the quote is always "
        reader = csv.DictReader(io.StringIO(text), delimiter=delim, quotechar='"')
        if not reader.fieldnames or not ({"id", "mac", "ip"} & {(f or "").strip().lower() for f in reader.fieldnames}):
            raise ValueError("export.err.format")
        out = []
        for rec in reader:
            out.append({(k or "").strip().lower(): _uncell((v or "").strip()) for k, v in rec.items() if k})
            if len(out) > MAX_ROWS:
                break
    else:
        raise ValueError("export.err.format")
    if len(out) > MAX_ROWS:
        raise ValueError("export.err.too_many")
    if not out:
        raise ValueError("export.err.empty")
    return out


def _match(rec: dict, by_id: dict, by_mac: dict, by_ip: dict) -> Optional[dict]:
    rid = (rec.get("id") or "").strip()
    if rid and rid in by_id:
        return by_id[rid]
    mac = (rec.get("mac") or "").strip().lower()
    if mac and mac in by_mac:
        return by_mac[mac]
    ip = (rec.get("ip") or "").strip()
    if not rid and not mac and ip and ip in by_ip:
        return by_ip[ip]
    return None


def apply(core, records: list, dry_run: bool = True) -> dict:
    """Write the own columns of the records into the inventory (or only count what would change)."""
    inv = core.inventory
    current = {r["id"]: r for r in rows(core)}
    by_mac = {r["mac"].lower(): r for r in current.values() if r["mac"]}
    by_ip = {r["ip"]: r for r in current.values() if r["ip"]}
    by_title = {}
    for r in current.values():
        by_title.setdefault((r["name"] or "").lower(), []).append(r)
    report = {"rows": len(records), "matched": 0, "changed": 0, "unchanged": 0, "unmatched": 0, "errors": [], "changes": [], "dry_run": bool(dry_run)}
    for n, rec in enumerate(records, 1):
        hit = _match(rec, current, by_mac, by_ip)
        if not hit:
            report["unmatched"] += 1
            continue
        report["matched"] += 1
        did = hit["id"]
        patch, name_change, link = {}, None, None
        for col, field in (("purpose", "purpose"), ("room", "room"), ("owner", "owner"), ("shop", "shop"), ("note", "note"), ("serial", "serial"),
                           ("purchase_date", "purchase_date"), ("warranty_until", "warranty_until"), ("eol_until", "eol_until"),
                           ("firmware_own", "firmware")):
            v = (rec.get(col) or "").strip()
            if v and v != str(hit.get(col) or ""):
                patch[field] = v
        name = (rec.get("name") or "").strip()
        if name and name != hit["name"]:
            name_change = name
        via_id = (rec.get("connected_via_id") or "").strip()
        via_name = (rec.get("connected_via") or "").strip()
        port, medium = (rec.get("port") or "").strip(), (rec.get("medium") or "").strip() or None
        if not via_id and via_name:
            named = by_title.get(via_name.lower(), [])
            if len(named) == 1:
                via_id = named[0]["id"]
            elif via_name != hit["connected_via"]:
                report["errors"].append({"row": n, "id": did, "error": "export.err.via_name"})
                continue
        if via_id and (via_id != hit["connected_via_id"] or (port and port != hit["port"]) or (medium and medium != hit["medium"])):
            link = {"parent": via_id, "port": port or hit["port"] or None, "medium": medium or hit["medium"] or "wired"}
        try:
            clean = inv.clean_patch(patch) if patch else {}
            if clean:
                inv._check_dates(dict(inv.record(did), **clean))
            if "owner" in clean and clean["owner"]:
                clean["owner"] = next(p for p in inv.people() if p.lower() == clean["owner"].lower())
            if name_change is not None and len(name_change) > 48:
                raise ValueError("export.err.name")
            if link:
                _check_link(core, did, link)
        except ValueError as exc:
            report["errors"].append({"row": n, "id": did, "error": str(exc)})
            continue
        if not (clean or name_change is not None or link):
            report["unchanged"] += 1
            continue
        report["changed"] += 1
        if len(report["changes"]) < 50:               # which fields (never their values) of which device, for the preview
            report["changes"].append({"row": n, "id": did, "fields": sorted(list(clean) + (["name"] if name_change is not None else []) + (["connection"] if link else []))})
        if dry_run:
            continue
        if clean:
            inv.set_fields(did, clean)
        if name_change is not None or link:
            upd = {}
            if name_change is not None:
                upd["title"] = name_change
            if link:
                upd["link"] = link
            core.patch_device(did, upd)
            if name_change is not None:
                inv.set_fields(did, {})               # touching it confirms the entry (inventory rule)
    report["errors"] = report["errors"][:50]
    return report


def _check_link(core, device_id: str, link: dict) -> None:
    """The same rules as ``Core.set_manual_link`` without writing: a known parent, no loop."""
    from . import topology
    parent = link["parent"]
    if link["medium"] not in topology.MEDIA:
        raise ValueError("topo.err.invalid")
    topo = core.topology()
    known = bool(core.store.device(parent)) or parent in topo["nodes"]
    if not parent or parent == device_id or not known or parent == "internet":
        raise ValueError("topo.err.parent")
    manual = core.manual_links()
    manual[device_id] = {"parent": parent}
    node, guard = parent, 0
    while node and guard < 10000:
        if node == device_id:
            raise ValueError("topo.err.loop")
        node, guard = (manual.get(node) or topo["links"].get(node) or {}).get("parent"), guard + 1
