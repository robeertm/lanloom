"""Inventory and life cycle: what a device is for, where it stands, when it was bought, how long it is covered, which
firmware it runs, when its updates end - and what became of it (missing, probably removed, decommissioned).

Two kinds of facts live side by side:

* **found** by Lanloom (name, kind, maker, model, firmware, address, last sign of life) - read from the device list, the
  identity facts (identity.py), the families' equipment lists and the hub's device registry; never written here;
* **own** (purpose, room, purchase date, warranty, shop and note, serial number, responsible person, own firmware or end
  date, state) - one JSON record per device in the table ``inventory``, written only by people (operators and up).

Life cycle states of a device::

    active          seen recently (or nothing is judged: see ``observing``)
    missing         no sign of life for ``missing_days`` days
    removed         missing and the heuristic finds enough reasons to suspect it is gone for good (``removal_hints``)
    decommissioned  retired by a person (date, replaced by, note); hidden from the map; stays here as history

An entry Lanloom made on its own is *provisional* until a person confirms it - explicitly, or by touching it (name, kind,
own fields, a manual link, hiding). A provisional entry that shows no sign of life for ``auto_remove_days`` days (7)
disappears (once a day, journalled for the nightly report). Gateway, this computer and every device a connected family or
hub knows are never removed this way. Nothing is judged while Lanloom itself has not observed the network for two days
(a stopped scan schedule must not turn the whole list "missing").

All time-dependent functions take ``now`` so the rules can be tested with a fixed clock.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import time
from typing import Callable, Optional

from . import eol as eol_mod

log = logging.getLogger("lanloom")

DAY = 86400.0
TEXT_FIELDS = {"purpose": 60, "room": 48, "shop": 80, "note": 400, "owner": 48, "serial": 48, "firmware": 40}
DATE_FIELDS = ("purchase_date", "warranty_until", "eol_until")
OWN_FIELDS = tuple(TEXT_FIELDS) + DATE_FIELDS
BULK_FIELDS = ("purpose", "room", "shop", "note", "owner", "purchase_date", "warranty_until")   # a serial number is per device
STATES = ("active", "missing", "removed", "decommissioned")
SORTS = ("title", "ip", "kind", "vendor", "room", "owner", "warranty_until", "eol_until", "last_seen", "state", "first_seen", "purpose")
ALWAYS_ON = ("router", "switch", "ap", "nas", "server", "hub", "broker", "printer", "camera", "tv")
MAX_BULK = 500
OBSERVE_WINDOW_S = 2 * DAY
LONG_LIVED_S = 14 * DAY
SUCCESSOR_WINDOW_S = 3 * DAY
REMOVED_SCORE = 0.5
DEFAULTS = {"missing_days": 7, "removed_days": 30, "auto_remove_days": 7, "warranty_warn_days": 30, "eol_warn_days": 180}
LIMITS = {"missing_days": (1, 365), "removed_days": (7, 730), "auto_remove_days": (0, 90),
          "warranty_warn_days": (1, 365), "eol_warn_days": (7, 1095)}
CTRL_RX = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
SERIAL_RX = re.compile(r"[^A-Za-z0-9 ._:/#+-]")


def _today(now: float) -> datetime.date:
    return datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date()


def days_until(date_str: Optional[str], now: float) -> Optional[int]:
    d = eol_mod.parse_date(date_str)
    return (d - _today(now)).days if d else None


def clean_text(value, limit: int) -> str:
    text = CTRL_RX.sub(" ", str(value if value is not None else "")).replace("\r", " ")
    return re.sub(r"[ \t]+", " ", text).strip()[:limit]


def _blank_record() -> dict:
    rec = {k: "" for k in TEXT_FIELDS}
    rec.update({k: "" for k in DATE_FIELDS})
    rec.update({"confirmed": False, "state": "active", "decom_at": "", "replaced_by": "", "decom_note": "",
                "hid": False, "kept_alive": 0.0})
    return rec


class Inventory:
    def __init__(self, core, clock: Callable[[], float] = time.time):
        self.core = core
        self.store = core.store
        self.clock = clock
        self.eol = eol_mod.EolTable(extra=(core.cfg.data_dir / "eol" / "eol.json") if getattr(core.cfg, "data_dir", None) else None)
        self._flushed = 0.0

    def table(self) -> eol_mod.EolTable:
        """The end-of-updates table: the bundled one, or the demo's fictional one."""
        t = getattr(getattr(self.core, "demo", None), "eol", None)
        return t if isinstance(t, eol_mod.EolTable) else self.eol

    # ------------------------------------------------------------------ settings
    def settings(self) -> dict:
        saved = self.store.get_setting("inventory", {}) or {}
        out = dict(DEFAULTS)
        for k, (lo, hi) in LIMITS.items():
            v = saved.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi:
                out[k] = v
        return out

    def update_settings(self, patch: dict) -> dict:
        cur = self.settings()
        for k, (lo, hi) in LIMITS.items():
            if k in patch:
                v = patch[k]
                if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                    raise ValueError("inventory.err.setting")
                cur[k] = v
        if 0 < cur["auto_remove_days"] < 3:
            raise ValueError("inventory.err.setting")
        self.store.set_setting("inventory", cur)
        return cur

    # ------------------------------------------------------------------ records
    def _records(self) -> dict:
        out = {}
        for r in self.store.query("SELECT device_id, data FROM inventory"):
            try:
                rec = json.loads(r["data"])
            except ValueError:
                rec = {}
            out[r["device_id"]] = dict(_blank_record(), **{k: v for k, v in rec.items() if k in _blank_record()})
        return out

    def record(self, device_id: str) -> dict:
        row = self.store.one("SELECT data FROM inventory WHERE device_id=?", (device_id,))
        rec = _blank_record()
        if row:
            try:
                rec.update({k: v for k, v in json.loads(row["data"]).items() if k in rec})
            except ValueError:
                pass
        return rec

    def _save(self, device_id: str, rec: dict) -> None:
        self.store.execute("INSERT INTO inventory(device_id, data, updated) VALUES(?,?,?)"
                           " ON CONFLICT(device_id) DO UPDATE SET data=excluded.data, updated=excluded.updated",
                           (device_id, json.dumps(rec, separators=(",", ":")), self.clock()))

    def rename_device(self, old: str, new: str) -> None:
        """A device first seen by address got its hardware address: the id changes, the entries follow."""
        self.store.execute("UPDATE OR IGNORE inventory SET device_id=? WHERE device_id=?", (new, old))
        self.store.execute("UPDATE OR IGNORE device_alive SET device_id=? WHERE device_id=?", (new, old))
        self.store.execute("UPDATE inventory SET data=replace(data, ?, ?) WHERE data LIKE ?",
                           ('"replaced_by":"%s"' % old, '"replaced_by":"%s"' % new, '%"replaced_by":"' + old + '"%'))

    def forget(self, device_id: str) -> None:
        self.store.execute("DELETE FROM inventory WHERE device_id=?", (device_id,))
        self.store.execute("DELETE FROM device_alive WHERE device_id=?", (device_id,))

    def people(self) -> list:
        return [r["username"] for r in self.store.query("SELECT username FROM users WHERE disabled=0 ORDER BY username COLLATE NOCASE")]

    # ------------------------------------------------------------------ signs of life
    def alive_at(self, row: dict, saved: Optional[dict] = None) -> float:
        did = row["id"]
        core = self.core
        t = max(row["last_seen"] or 0, core.presence.get(did, 0), core.flow_seen.get(did, 0))
        if saved is not None:
            t = max(t, saved.get(did, 0))
        return t

    def _saved_alive(self) -> dict:
        return {r["device_id"]: r["t"] for r in self.store.query("SELECT device_id, t FROM device_alive")}

    def observing(self, now: float, saved: Optional[dict] = None) -> bool:
        """Has Lanloom looked at the network in the last two days? (a scan, or any device answering)"""
        if getattr(self.core, "demo", None):
            return True                               # the sample network is always "just looked at"
        row = self.store.one("SELECT finished FROM scans WHERE status='done' ORDER BY id DESC LIMIT 1")
        t = (row or {}).get("finished") or 0
        t = max([t] + [v for v in self.core.presence.values()] + [v for v in (saved or {}).values()])
        return now - t < OBSERVE_WINDOW_S

    def flush_alive(self, now: Optional[float] = None) -> int:
        """Remember signs of life across restarts (the in-memory presence is lost when the process ends)."""
        now = self.clock() if now is None else now
        seen = {}
        for did, t in list(self.core.presence.items()) + list(self.core.flow_seen.items()):
            if t > seen.get(did, 0):
                seen[did] = t
        n = 0
        with self.store.transaction():
            for did, t in seen.items():
                if t <= now + 60:
                    self.store.execute("INSERT INTO device_alive(device_id, t) VALUES(?,?) ON CONFLICT(device_id) DO UPDATE SET t=MAX(t, excluded.t)", (did, t))
                    n += 1
        self._flushed = now
        return n

    # ------------------------------------------------------------------ found facts
    def _family_firmware(self) -> dict:
        """Firmware a connected family reports for its own equipment, by management address -> device id."""
        out = {}
        try:
            entries = self.core.live_configs()
        except Exception:
            entries = []
        for entry in entries:
            for eq in ((entry.get("state") or {}).get("equipment")) or []:
                ip, fw = eq.get("mgmt_ip"), eq.get("firmware")
                did = self.core.ip_to_device.get(ip) if ip else None
                if did and fw:
                    out.setdefault(did, (clean_text(fw, 40), "family"))
        return out

    def _hub_firmware(self) -> dict:
        out = {}
        try:
            by_net = (self.core.hub_merge() or {}).get("by_net") or {}
        except Exception:
            return out
        for did, matches in by_net.items():
            for m in matches:
                snap = (self.core.hub.get(m.get("integ")) or {}).get("snapshot") or {}
                for d in snap.get("devices") or []:
                    if d.get("id") == m.get("device_id") and d.get("sw"):
                        out.setdefault(did, (clean_text(d["sw"], 40), "hub"))
        return out

    @staticmethod
    def _reported_serial(data: dict) -> str:
        for s in data.get("ssdp") or []:
            if isinstance(s, dict) and s.get("serial"):
                return clean_text(SERIAL_RX.sub("", str(s["serial"])), 48)
        for m in data.get("mdns") or []:
            txt = (m.get("txt") or {}) if isinstance(m, dict) else {}
            for k in ("serialnumber", "serial", "sn"):
                v = txt.get(k) or txt.get(k.upper())
                if v:
                    return clean_text(SERIAL_RX.sub("", str(v)), 48)
        return ""

    # ------------------------------------------------------------------ heuristics
    def removal_hints(self, row: dict, view: dict, alive: float, now: float, cfg: dict, ctx: dict) -> dict:
        """Why a missing device might be gone for good. ``score`` >= 0.5 flags it as "removed?" in the list.

        long_absent  - missing for ``removed_days`` or more (0.5)
        successor    - a device of the same kind and maker first appeared within three days of its last sign of life
                       (0.35; it is offered as the replacement)
        port_reused  - its last known switch port is now used by another device (0.3)
        always_on    - it ran for at least two weeks and is the kind of device that normally stays on (0.2)
        """
        reasons, score, successor = [], 0.0, None
        absent = now - alive
        if absent >= cfg["removed_days"] * DAY:
            reasons.append("long_absent")
            score += 0.5
        for other in ctx["devices"]:
            if other["id"] == row["id"] or other["hidden"] or other.get("merged_into"):
                continue
            if other["kind"] == view["kind"] and other.get("vendor") and other.get("vendor") == view.get("vendor") \
                    and alive - DAY <= other["first_seen"] <= alive + SUCCESSOR_WINDOW_S and ctx["alive"].get(other["id"], 0) > now - DAY * 2:
                successor = other
                reasons.append("successor")
                score += 0.35
                break
        link = ctx["links"].get(row["id"]) or {}
        if link.get("parent") and link.get("port"):
            for oid, ol in ctx["links"].items():
                if oid != row["id"] and ol.get("parent") == link["parent"] and ol.get("port") == link["port"] and ctx["alive"].get(oid, 0) > alive:
                    reasons.append("port_reused")
                    score += 0.3
                    break
        if view["kind"] in ALWAYS_ON and alive - (row["first_seen"] or alive) >= LONG_LIVED_S:
            reasons.append("always_on")
            score += 0.2
        return {"score": round(score, 2), "reasons": reasons, "successor": successor}

    def _context(self, now: float) -> dict:
        views = self.core.devices()
        saved = self._saved_alive()
        alive = {}
        for r in self.store.query("SELECT id, last_seen, first_seen FROM devices"):
            alive[r["id"]] = self.alive_at(r, saved)
        try:
            links = (self.core.topology() or {}).get("links") or {}
        except Exception:
            links = {}
        return {"devices": views, "views": {v["id"]: v for v in views}, "alive": alive, "links": links, "saved": saved,
                "records": self._records(), "family": self._family_firmware(), "hub": self._hub_firmware(),
                "manual": self.core.manual_links(), "observing": self.observing(now, saved), "cfg": self.settings(), "now": now}

    # ------------------------------------------------------------------ one entry
    def _item(self, view: dict, ctx: dict) -> dict:
        now, cfg = ctx["now"], ctx["cfg"]
        did = view["id"]
        rec = ctx["records"].get(did) or _blank_record()
        row = {"id": did, "first_seen": view["first_seen"], "last_seen": view["last_seen"]}
        alive = ctx["alive"].get(did) or view["last_seen"] or 0
        absent_d = int((now - alive) // DAY) if alive else None
        data_serial = self._reported_serial(view)
        # firmware: own entry > family > hub > what the device itself announced
        if rec["firmware"]:
            fw, fw_src = rec["firmware"], "own"
        elif did in ctx["family"]:
            fw, fw_src = ctx["family"][did]
        elif did in ctx["hub"]:
            fw, fw_src = ctx["hub"][did]
        else:
            fw, fw_src = view.get("firmware"), view.get("firmware_source")
        state, hints, hint_view = "active", None, None
        if rec["state"] == "decommissioned":
            state = "decommissioned"
        elif ctx["observing"] and alive and now - alive >= cfg["missing_days"] * DAY:
            state = "missing"
            hints = self.removal_hints(row, view, alive, now, cfg, ctx)
            if hints["score"] >= REMOVED_SCORE and not (rec["kept_alive"] and alive <= rec["kept_alive"]):
                state = "removed"
            hint_view = {"score": hints["score"], "reasons": hints["reasons"],
                         "successor": ({"id": hints["successor"]["id"], "title": hints["successor"]["title"] or hints["successor"]["ip"]}
                                       if hints["successor"] else None)}
        touched = bool(rec["confirmed"] or view.get("auto_title") != view.get("title") or view.get("auto_kind") != view.get("kind")
                       or view["hidden"] or bool(view.get("sleepy")) or any(rec[k] for k in OWN_FIELDS) or did in ctx["manual"])
        protected = bool(view.get("is_gateway") or view.get("is_self") or view["kind"] in ("self", "internet") or did in ctx["family"]
                         or did in ctx["hub"] or view.get("merged_into"))
        provisional = not touched and rec["state"] != "decommissioned"
        warranty = None
        if rec["warranty_until"]:
            left = days_until(rec["warranty_until"], now)
            warranty = {"until": rec["warranty_until"], "days_left": left,
                        "state": "expired" if left is not None and left < 0 else "soon" if left is not None and left <= cfg["warranty_warn_days"] else "ok"}
        eol = None
        if rec["eol_until"]:
            eol = {"until": rec["eol_until"], "source": "own", "name": None, "id": None}
        else:
            hit = self.table().lookup(view.get("vendor"), view.get("model"), fw, (view.get("features") or {}).get("sys_descr"),
                                  ((view.get("ssdp") or [{}])[0] or {}).get("server"), view.get("title"))
            if hit:
                eol = {"until": hit["until"], "source": "table", "name": hit["name"], "id": hit["id"]}
        if eol:
            left = days_until(eol["until"], now)
            eol["days_left"] = left
            eol["state"] = "past" if left is not None and left < 0 else "soon" if left is not None and left <= cfg["eol_warn_days"] else "ok"
        replaced = None
        if rec["replaced_by"]:
            rv = ctx["views"].get(rec["replaced_by"])
            replaced = {"id": rec["replaced_by"], "title": (rv["title"] or rv["ip"]) if rv else None}
        seen_again = False
        if rec["state"] == "decommissioned" and rec["decom_at"]:
            d = eol_mod.parse_date(rec["decom_at"])
            if d and alive > (datetime.datetime.combine(d, datetime.time(0), datetime.timezone.utc).timestamp() + 2 * DAY):
                seen_again = True
        return {
            "id": did, "title": view["title"] or "", "kind": view["kind"], "ip": view["ip"], "mac": view["mac"], "hidden": view["hidden"],
            "status": view["status"], "vendor": view.get("vendor"), "model": view.get("model"),
            "firmware": fw, "firmware_source": fw_src, "serial_reported": data_serial or None,
            "purpose": rec["purpose"], "room": rec["room"], "shop": rec["shop"], "note": rec["note"], "owner": rec["owner"],
            "serial": rec["serial"], "purchase_date": rec["purchase_date"], "warranty_until": rec["warranty_until"],
            "eol_until": rec["eol_until"], "firmware_own": rec["firmware"],
            "confirmed": bool(rec["confirmed"]), "provisional": provisional, "protected": protected,
            "state": state, "missing_days": absent_d if state in ("missing", "removed") else None, "hints": hint_view,
            "decommissioned": {"at": rec["decom_at"], "replaced_by": replaced, "note": rec["decom_note"], "seen_again": seen_again}
            if rec["state"] == "decommissioned" else None,
            "warranty": warranty, "eol": eol, "alive_at": alive or None, "first_seen": view["first_seen"], "last_seen": view["last_seen"],
        }

    def get(self, device_id: str, now: Optional[float] = None) -> Optional[dict]:
        now = self.clock() if now is None else now
        ctx = self._context(now)
        view = ctx["views"].get(device_id)
        return self._item(view, ctx) if view else None

    # ------------------------------------------------------------------ the list
    def list(self, query: Optional[dict] = None, now: Optional[float] = None) -> dict:
        now = self.clock() if now is None else now
        q = query or {}
        ctx = self._context(now)
        items = [self._item(v, ctx) for v in ctx["devices"] if not v.get("merged_into")]
        everything = items
        facets = {"rooms": sorted({i["room"] for i in everything if i["room"]}, key=str.lower),
                  "owners": sorted({i["owner"] for i in everything if i["owner"]}, key=str.lower),
                  "kinds": sorted({i["kind"] for i in everything})}
        summary = {"total": len(everything)}
        for s in STATES:
            summary[s] = sum(1 for i in everything if i["state"] == s)
        summary["provisional"] = sum(1 for i in everything if i["provisional"] and i["state"] != "decommissioned")
        summary["warranty_soon"] = sum(1 for i in everything if i["warranty"] and i["warranty"]["state"] == "soon" and i["state"] != "decommissioned")
        summary["warranty_expired"] = sum(1 for i in everything if i["warranty"] and i["warranty"]["state"] == "expired" and i["state"] != "decommissioned")
        summary["eol_past"] = sum(1 for i in everything if i["eol"] and i["eol"]["state"] == "past" and i["state"] != "decommissioned")
        summary["eol_soon"] = sum(1 for i in everything if i["eol"] and i["eol"]["state"] == "soon" and i["state"] != "decommissioned")
        items = self.filter(items, q)
        items = self.sort(items, q.get("sort"), q.get("dir"))
        return {"items": items, "summary": summary, "facets": facets, "settings": ctx["cfg"], "people": self.people(),
                "observing": ctx["observing"], "eol_as_of": self.table().as_of, "now": now}

    @staticmethod
    def filter(items: list, q: dict) -> list:
        state = q.get("state")
        if state in STATES:
            items = [i for i in items if i["state"] == state]
        elif state == "open":                                   # everything that is not retired
            items = [i for i in items if i["state"] != "decommissioned"]
        elif state == "attention":                              # what needs a look
            items = [i for i in items if i["state"] in ("missing", "removed") or (i["warranty"] and i["warranty"]["state"] in ("soon", "expired"))
                     or (i["eol"] and i["eol"]["state"] in ("past", "soon"))]
        if q.get("provisional") in (True, "1", "true"):
            items = [i for i in items if i["provisional"] and i["state"] != "decommissioned"]
        if q.get("room") is not None and q.get("room") != "":
            items = [i for i in items if i["room"].lower() == str(q["room"]).lower()]
        if q.get("owner") is not None and q.get("owner") != "":
            items = [i for i in items if i["owner"].lower() == str(q["owner"]).lower()]
        if q.get("kind"):
            items = [i for i in items if i["kind"] == q["kind"]]
        w = q.get("warranty")
        if w in ("soon", "expired", "ok"):
            items = [i for i in items if i["warranty"] and i["warranty"]["state"] == w]
        elif w == "none":
            items = [i for i in items if not i["warranty"]]
        e = q.get("eol")
        if e in ("past", "soon", "ok"):
            items = [i for i in items if i["eol"] and i["eol"]["state"] == e]
        elif e == "any":
            items = [i for i in items if i["eol"] and i["eol"]["state"] in ("past", "soon")]
        text = str(q.get("q") or "").strip().lower()[:80]
        if text:
            words = text.split()

            def hay(i):
                return " ".join(str(i.get(k) or "") for k in ("title", "ip", "mac", "kind", "vendor", "model", "firmware", "serial", "serial_reported",
                                                              "purpose", "room", "shop", "note", "owner")).lower()
            items = [i for i in items if all(w in hay(i) for w in words)]
        return items

    @staticmethod
    def sort(items: list, key, direction) -> list:
        key = key if key in SORTS else "title"
        rev = direction == "desc"

        def val(i):
            v = i.get(key)
            if key in ("last_seen", "first_seen"):
                return v or 0
            return (v or "").lower() if isinstance(v, str) else (v if v is not None else "")
        empty = [i for i in items if not i.get(key)]
        full = [i for i in items if i.get(key)]
        full.sort(key=lambda i: (val(i), (i["title"] or i["ip"]).lower()), reverse=rev)
        empty.sort(key=lambda i: ((i["title"] or i["ip"]).lower(), i["ip"]))
        return full + empty                       # entries without a value always at the end

    # ------------------------------------------------------------------ changes (people only)
    def clean_patch(self, patch: dict, allowed: tuple = OWN_FIELDS) -> dict:
        out = {}
        for k, v in (patch or {}).items():
            if k not in allowed:
                if k in ("confirmed",):
                    continue
                raise ValueError("inventory.err.field")
            if k in DATE_FIELDS:
                v = clean_text(v, 10)
                if v and eol_mod.parse_date(v) is None:
                    raise ValueError("inventory.err.date")
                if k == "purchase_date" and v and eol_mod.parse_date(v) > _today(self.clock()) + datetime.timedelta(days=1):
                    raise ValueError("inventory.err.future")
            elif k == "serial":
                v = clean_text(SERIAL_RX.sub("", str(v or "")), TEXT_FIELDS[k])
            else:
                v = clean_text(v, TEXT_FIELDS[k])
            if k == "owner" and v and v.lower() not in {p.lower() for p in self.people()}:
                raise ValueError("inventory.err.owner")
            out[k] = v
        return out

    def _check_dates(self, rec: dict) -> None:
        p, w = eol_mod.parse_date(rec["purchase_date"]), eol_mod.parse_date(rec["warranty_until"])
        if p and w and w < p:
            raise ValueError("inventory.err.order")

    def set_fields(self, device_id: str, patch: dict, confirm: bool = True) -> Optional[dict]:
        if not self.store.device(device_id):
            return None
        clean = self.clean_patch(patch)
        rec = self.record(device_id)
        if "owner" in clean and clean["owner"]:
            clean["owner"] = next(p for p in self.people() if p.lower() == clean["owner"].lower())
        rec.update(clean)
        self._check_dates(rec)
        if clean and confirm:
            rec["confirmed"] = True               # whoever writes down what a device is, has looked at it
        if "confirmed" in (patch or {}) and isinstance(patch["confirmed"], bool):
            rec["confirmed"] = patch["confirmed"]
        self._save(device_id, rec)
        return self.get(device_id)

    def bulk(self, ids: list, patch: dict, confirm: bool = False) -> dict:
        """The same own fields for many devices; only fields that make sense for many, never name, kind or serial number."""
        if not isinstance(ids, list) or not ids or len(ids) > MAX_BULK or not all(isinstance(i, str) for i in ids):
            raise ValueError("inventory.err.bulk")
        clean = self.clean_patch(patch or {}, BULK_FIELDS)
        if not clean and not confirm:
            raise ValueError("inventory.err.bulk")
        if "owner" in clean and clean["owner"]:
            clean["owner"] = next(p for p in self.people() if p.lower() == clean["owner"].lower())
        done = 0
        with self.store.transaction():
            for did in dict.fromkeys(ids):
                if not self.store.device(did):
                    continue
                rec = self.record(did)
                trial = dict(rec, **clean)
                try:
                    self._check_dates(trial)
                except ValueError:
                    continue
                rec = trial
                if clean or confirm:
                    rec["confirmed"] = True
                self._save(did, rec)
                done += 1
        return {"changed": done, "skipped": len(set(ids)) - done}

    def lifecycle(self, device_id: str, action: str, at: str = "", replaced_by: str = "", note: str = "") -> Optional[dict]:
        dev = self.store.device(device_id)
        if not dev:
            return None
        now = self.clock()
        rec = self.record(device_id)
        if action == "confirm":
            rec["confirmed"] = True
        elif action == "unconfirm":
            rec["confirmed"] = False
        elif action == "keep":                    # "still here, thank you": the suggestion rests until the device shows up again
            ctx_alive = self.alive_at({"id": device_id, "last_seen": dev["last_seen"]}, self._saved_alive())
            rec["kept_alive"] = ctx_alive or now
            rec["confirmed"] = True
        elif action == "decommission":
            at = clean_text(at, 10) or _today(now).isoformat()
            d = eol_mod.parse_date(at)
            if d is None:
                raise ValueError("inventory.err.date")
            if d > _today(now) + datetime.timedelta(days=1):
                raise ValueError("inventory.err.future")
            if replaced_by:
                if replaced_by == device_id or not self.store.device(replaced_by):
                    raise ValueError("inventory.err.replacement")
            rec.update({"state": "decommissioned", "decom_at": at, "replaced_by": replaced_by or "",
                        "decom_note": clean_text(note, 200), "confirmed": True})
            if not dev["hidden"]:
                self.store.execute("UPDATE devices SET hidden=1 WHERE id=?", (device_id,))
                rec["hid"] = True
        elif action == "reactivate":
            if rec["state"] != "decommissioned":
                raise ValueError("inventory.err.state")
            rec.update({"state": "active", "decom_at": "", "replaced_by": "", "decom_note": "", "kept_alive": 0.0})
            if rec["hid"]:
                self.store.execute("UPDATE devices SET hidden=0 WHERE id=?", (device_id,))
                rec["hid"] = False
        else:
            raise ValueError("inventory.err.action")
        self._save(device_id, rec)
        self.core._refresh_index()
        return self.get(device_id)

    # ------------------------------------------------------------------ daily housekeeping
    def housekeeping(self, now: Optional[float] = None) -> dict:
        """Remember signs of life; let provisional automatic entries without a sign of life disappear."""
        now = self.clock() if now is None else now
        self.flush_alive(now)
        cfg = self.settings()
        removed = []
        days = cfg["auto_remove_days"]
        if days:
            ctx = self._context(now)
            if ctx["observing"]:
                for v in ctx["devices"]:
                    it = self._item(v, ctx)
                    alive = ctx["alive"].get(v["id"]) or 0
                    if it["provisional"] and not it["protected"] and alive and now - alive >= days * DAY and it["state"] != "decommissioned":
                        removed.append({"id": v["id"], "title": v["title"] or v["ip"], "kind": v["kind"], "ip": v["ip"], "since": alive})
        for r in removed:
            self.core.forget_device(r["id"])
            self.forget(r["id"])
            try:
                self.core.reports.journal.add("inv_expired", dict(r), now)
            except Exception:
                log.exception("inventory: journal entry failed")
        return {"removed": removed}

    async def loop(self) -> None:
        await asyncio.sleep(180)
        while True:
            try:
                await asyncio.to_thread(self.housekeeping)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("inventory housekeeping failed")
            await asyncio.sleep(3600)
