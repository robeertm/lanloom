"""Read half of the Proxmox VE family: reads one host or cluster through its local API, never writes.

Every request goes through ``transport.Transport`` with a *read* allow-list: GET on the paths below, nothing else -
no sign-in, no POST at all (the API token travels in the ``Authorization`` header). A path outside the list is
refused before a byte is sent.

Cadence: the light poll (cluster resources and status: guest state, load, traffic counters) every 60 s, the full
poll (node status and network, guest configurations and network cards, firewall at every level, software-defined
network, storage, backup jobs and backup volumes, last hour of traffic per guest) every 5 minutes. Events:

* ``{"type": "topology", "source": "hypervisor", ...}`` - nodes and guest network cards (core-internal, carries
  hardware addresses only to match inventory devices); the topology resolver hangs every guest below its node;
* ``{"type": "config", "source": "family", "family": "proxmox", "state": <neutral model v2>, "capabilities": ...}``
  - hosts, guests, bridges, firewall, storage and backups for the firewall tab and the component cards (read only).

Product names are used only to identify compatible products; no affiliation or endorsement.
"""
import asyncio
import json
import re
import time

from . import CAPABILITIES, model
from .. import capabilities
from ..credentials import SUPERUSER_PRINCIPALS, Secret
from ..transport import Forbidden, Transport, TransportError, read_allowlist
from ...sources._common import SourceError, StatusTracker, emit_event, now, run_with_backoff, short_test

MODULE = "family"
SOURCE = "family"
URL_RE = re.compile(r"\A(https?)://([A-Za-z0-9.-]{1,253}|\[[0-9A-Fa-f:.]{2,45}\])(?::(\d{1,5}))?/?\Z", re.I)
TOKEN_RE = re.compile(r"\A([A-Za-z0-9._-]{1,64}@[A-Za-z0-9._-]{1,64})!([A-Za-z][A-Za-z0-9._-]{0,63})\Z")
SECRET_RE = re.compile(r"\A[A-Za-z0-9-]{8,128}\Z")
LIGHT_S = 60.0
FULL_S = 300.0
MAX_REQUESTS = 1500              # request budget of one full poll (large clusters: the rest waits for the next poll)
MAX_GUEST_DETAILS = 400
TASK_LIMIT = 100
READ_PRIVS = re.compile(r"\A(?:[A-Za-z]+\.)+Audit\Z|\ASys\.Syslog\Z")      # what a read-only token may hold

API = "/api2/json"
_N = r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}"          # node name
_V = r"\d{1,9}"                                   # guest number
_G = r"[A-Za-z][A-Za-z0-9_-]{0,31}"               # security group, IP set, vnet name
_S = r"[A-Za-z][A-Za-z0-9_.-]{0,99}"              # storage name
GET_PATHS = (
    API + r"/(?:version|cluster/status|cluster/resources|cluster/backup|cluster/backup-info/not-backed-up|storage|access/permissions)",
    API + r"/cluster/firewall/(?:options|rules|groups|aliases|ipset)",
    API + r"/cluster/firewall/(?:groups|ipset)/%s" % _G,
    API + r"/cluster/sdn/(?:zones|vnets)",
    API + r"/cluster/sdn/vnets/%s/subnets" % _G,
    API + r"/nodes/%s/(?:status|network|storage|firewall/options|firewall/rules)" % _N,
    API + r"/nodes/%s/tasks\?typefilter=vzdump&limit=\d{1,4}" % _N,
    API + r"/nodes/%s/storage/%s/content\?content=backup" % (_N, _S),
    API + r"/nodes/%s/(?:qemu|lxc)/%s/(?:config|firewall/options|firewall/rules|firewall/aliases|firewall/ipset)" % (_N, _V),
    API + r"/nodes/%s/(?:qemu|lxc)/%s/rrddata\?timeframe=hour&cf=AVERAGE" % (_N, _V),
    API + r"/nodes/%s/qemu/%s/agent/network-get-interfaces" % (_N, _V),
    API + r"/nodes/%s/lxc/%s/interfaces" % (_N, _V),
)
ALLOW = read_allowlist(GET_PATHS)


def _secret(value) -> Secret:
    return value if isinstance(value, Secret) else Secret(value)


class _Budget(Exception):
    pass


class ProxmoxReader:
    family = "proxmox"
    name = SOURCE

    def __init__(self, url=None, token_id=None, token_secret=None, allow_self_signed=False,
                 interval=LIGHT_S, full_interval=FULL_S, timeout=10.0):
        m = URL_RE.match(str(url or "").strip())
        if not m:
            raise SourceError("source.family.config", "address missing or invalid")
        port = int(m.group(3)) if m.group(3) else 8006
        if not 0 < port < 65536:
            raise SourceError("source.family.config", "port invalid")
        self._host = m.group(2)
        self._base = "%s://%s:%d" % (m.group(1).lower(), self._host, port)
        tid = str(token_id or "").strip()
        tm = TOKEN_RE.match(tid)
        if not tm:
            raise SourceError("source.family.config", "token id must look like user@realm!name")
        self._token_id = tid
        self._principal = tm.group(1)
        self._secret = _secret(token_secret)
        if not SECRET_RE.match(self._secret.reveal()):
            raise SourceError("source.family.config", "token secret missing or invalid")
        self._t = Transport(self._base, ALLOW, auth_headers=self._auth, allow_self_signed=bool(allow_self_signed), timeout=timeout)
        self.interval = max(5.0, float(interval))
        self.full_interval = max(self.interval, float(full_interval))
        self._caps = capabilities.validate(CAPABILITIES, self.family)
        self._raw = {}                 # last full poll (guest configs, firewall, ...), refreshed by light polls
        self._counters = {}            # guest key -> (t, netin, netout) for traffic rates
        self._denied = set()           # areas the token may not read (403), narrowed in the capability list
        self._left = MAX_REQUESTS
        self.last_raw_keys = ()

    def __repr__(self):
        return "ProxmoxReader(%s)" % self._host

    __str__ = __repr__

    def _auth(self) -> dict:
        return {"Authorization": "PVEAPIToken=%s=%s" % (self._token_id, self._secret.reveal())}

    # ------------------------------------------------------------------ one request
    def _get(self, rel: str, area: str = None, *, budget: bool = True):
        """The ``data`` of one GET below /api2/json, or None when this area is not offered / not allowed."""
        if budget:
            if self._left <= 0:
                raise _Budget()
            self._left -= 1
        try:
            status, _, data = self._t.request("GET", API + rel)
        except Forbidden:
            raise SourceError("source.family.refused", "request outside the allow-list") from None
        except TransportError as exc:
            raise SourceError("source.family.unreachable" if exc.key.endswith("unreachable") else "source.family.protocol") from None
        if status == 401:
            raise SourceError("source.family.token_refused", "API token refused")
        if status == 403:
            if area:
                self._denied.add(area)
            return None
        if status in (400, 404, 405, 500, 501, 595, 596):
            return None                     # not offered here (older release, no agent, node down, feature off)
        if not 200 <= status < 300:
            raise SourceError("source.family.protocol", "unexpected status %d" % status)
        try:
            body = json.loads(data.decode("utf-8")) if data else None
        except (ValueError, UnicodeDecodeError):
            raise SourceError("source.family.protocol", "reply is not JSON") from None
        return body.get("data") if isinstance(body, dict) else None

    # ------------------------------------------------------------------ polls
    def _light(self, raw: dict) -> None:
        raw["api_host"] = self._host
        version = self._get("/version", budget=False)
        if version is None:
            raise SourceError("source.family.protocol", "no version answer")
        raw["version"] = version
        raw["cluster"] = self._get("/cluster/status", "inventory", budget=False)
        raw["resources"] = self._get("/cluster/resources", "inventory", budget=False)
        raw["traffic"] = self._rates(raw.get("resources"))

    def _rates(self, resources) -> dict:
        t = time.monotonic()
        out, seen = {}, set()
        for r in resources if isinstance(resources, list) else []:
            if not isinstance(r, dict) or r.get("type") not in ("qemu", "lxc"):
                continue
            key = model.guest_key(r["type"], r.get("vmid"))
            cin, cout = model.num(r.get("netin")), model.num(r.get("netout"))
            if not isinstance(cin, (int, float)) or not isinstance(cout, (int, float)):
                continue
            seen.add(key)
            prev = self._counters.get(key)
            self._counters[key] = (t, cin, cout)
            if prev and t - prev[0] >= 5 and cin >= prev[1] and cout >= prev[2] and r.get("status") == "running":
                dt = t - prev[0]
                out[key] = [round((cin - prev[1]) / dt), round((cout - prev[2]) / dt)]
        for key in set(self._counters) - seen:
            self._counters.pop(key, None)
        return out

    def _full(self, raw: dict) -> None:
        res = raw.get("resources") if isinstance(raw.get("resources"), list) else []
        nodes = sorted({model.text(r.get("node"), 64) for r in res if isinstance(r, dict) and r.get("type") == "node"
                        and str(r.get("status") or "") == "online" and re.fullmatch(_N, str(r.get("node") or ""))} - {None})
        for e in raw.get("cluster") or []:
            if isinstance(e, dict) and e.get("type") == "node" and model.flag(e.get("online")) and re.fullmatch(_N, str(e.get("name") or "")):
                nodes = sorted(set(nodes) | {e["name"]})
        try:
            raw["storage_cfg"] = self._get("/storage", "storage")
            raw["backup_jobs"] = self._get("/cluster/backup", "backups")
            raw["not_backed_up"] = self._get("/cluster/backup-info/not-backed-up", "backups")
            raw["dc_fw"] = self._dc_firewall()
            raw["sdn"] = self._sdn()
            raw["nodes"] = {n: self._node(n) for n in nodes}
            raw["backups"] = self._backup_volumes(raw)
            raw["guests"] = self._guests(res)
        except _Budget:
            pass                                    # keep what was read; the next full poll starts afresh

    def _dc_firewall(self) -> dict:
        out = {"options": self._get("/cluster/firewall/options", "firewall"), "rules": self._get("/cluster/firewall/rules", "firewall"),
               "groups": self._get("/cluster/firewall/groups", "firewall"), "aliases": self._get("/cluster/firewall/aliases", "firewall"),
               "ipsets": self._get("/cluster/firewall/ipset", "firewall"), "group_rules": {}, "ipset_members": {}}
        for g in out["groups"] or []:
            name = str((g or {}).get("group") or "")
            if re.fullmatch(_G, name):
                out["group_rules"][name] = self._get("/cluster/firewall/groups/%s" % name, "firewall")
        for s in out["ipsets"] or []:
            name = str((s or {}).get("name") or "")
            if re.fullmatch(_G, name):
                out["ipset_members"][name] = self._get("/cluster/firewall/ipset/%s" % name, "firewall")
        return out

    def _sdn(self) -> dict:
        zones = self._get("/cluster/sdn/zones", "networks")
        vnets = self._get("/cluster/sdn/vnets", "networks")
        subnets = {}
        for v in vnets or []:
            name = str((v or {}).get("vnet") or "")
            if re.fullmatch(_G, name):
                subnets[name] = self._get("/cluster/sdn/vnets/%s/subnets" % name, "networks")
        return {"zones": zones, "vnets": vnets, "subnets": subnets}

    def _node(self, n: str) -> dict:
        return {"status": self._get("/nodes/%s/status" % n, "inventory"), "network": self._get("/nodes/%s/network" % n, "bridges"),
                "storage": self._get("/nodes/%s/storage" % n, "storage"),
                "fw_options": self._get("/nodes/%s/firewall/options" % n, "firewall"),
                "fw_rules": self._get("/nodes/%s/firewall/rules" % n, "firewall"),
                "tasks": self._get("/nodes/%s/tasks?typefilter=vzdump&limit=%d" % (n, TASK_LIMIT), "backups")}

    def _backup_volumes(self, raw: dict) -> list:
        """Metadata of backup volumes, every backup storage read once (a shared one from one node only)."""
        out, done = [], set()
        for node, data in sorted((raw.get("nodes") or {}).items()):
            for s in (data or {}).get("storage") or []:
                if not isinstance(s, dict):
                    continue
                name = str(s.get("storage") or "")
                content = str(s.get("content") or "").split(",")
                if "backup" not in content or not model.flag(s.get("active")) or not re.fullmatch(_S, name):
                    continue
                key = name if model.flag(s.get("shared")) else "%s/%s" % (node, name)
                if key in done:
                    continue
                done.add(key)
                for v in self._get("/nodes/%s/storage/%s/content?content=backup" % (node, name), "backups") or []:
                    if isinstance(v, dict):
                        out.append({"vmid": v.get("vmid"), "ctime": v.get("ctime"), "size": v.get("size"), "storage": name,
                                    "verification": v.get("verification") if isinstance(v.get("verification"), dict) else None})
        return out[:model.MAX_ITEMS * 4]

    def _guests(self, res: list) -> dict:
        out = {}
        guests = [r for r in res if isinstance(r, dict) and r.get("type") in ("qemu", "lxc") and re.fullmatch(_V, str(r.get("vmid") or ""))
                  and re.fullmatch(_N, str(r.get("node") or ""))]
        guests.sort(key=lambda r: (r.get("status") != "running", int(r["vmid"])))
        for r in guests[:MAX_GUEST_DETAILS]:
            kind, vmid, node = r["type"], int(r["vmid"]), r["node"]
            base = "/nodes/%s/%s/%d" % (node, kind, vmid)
            key = model.guest_key(kind, vmid)
            out[key] = {"node": node}               # partial entries survive a budget stop
            g = out[key]
            g["config"] = self._get(base + "/config", "guests")
            if model.flag(r.get("template")):
                continue
            g["fw_options"] = self._get(base + "/firewall/options", "firewall")
            g["fw_rules"] = self._get(base + "/firewall/rules", "firewall")
            if g["fw_options"] is not None:
                g["fw_aliases"] = self._get(base + "/firewall/aliases", "firewall")
                g["fw_ipsets"] = self._get(base + "/firewall/ipset", "firewall")
            if r.get("status") == "running":
                cfg = g["config"] if isinstance(g["config"], dict) else {}
                if kind == "qemu" and str(model.parse_kv(cfg.get("agent")).get("_") or model.parse_kv(cfg.get("agent")).get("enabled") or "0") == "1":
                    g["agent"] = self._get(base + "/agent/network-get-interfaces")
                elif kind == "lxc":
                    g["ifaces"] = self._get(base + "/interfaces")
                g["rrd"] = self._get(base + "/rrddata?timeframe=hour&cf=AVERAGE", "stats")
        return out

    def collect(self, light: bool = False) -> dict:
        """Blocking: every reply of this poll merged into the cache (keys see ``model``). Call from a thread."""
        self._left = MAX_REQUESTS
        if not light:
            self._denied = set()           # a full poll asks every area again (the light part runs first)
        raw = dict(self._raw)
        self._light(raw)
        if not light:
            self._full(raw)
        self._raw = raw
        self.last_raw_keys = tuple(sorted(k for k, v in raw.items() if v is not None))
        return raw

    def capabilities(self) -> dict:
        return self._caps

    def _refine(self, raw: dict, state: dict) -> dict:
        missing = set(self._denied)
        if not state.get("guests"):
            missing.add("guests")
        if not state.get("sdn_vnets") and not state.get("sdn_zones"):
            missing.add("networks")
        if not state.get("bridges"):
            missing.add("bridges")
        caps = capabilities.refine(capabilities.validate(CAPABILITIES, self.family), version=state["controller"]["version"] or "",
                                   read_missing=missing)
        self._caps = caps
        return caps

    def config_event(self, raw: dict, t: float = None) -> dict:
        t = now() if t is None else t
        state = model.to_neutral(raw, t)
        return {"type": "config", "source": SOURCE, "family": self.family, "t": t, "state": state,
                "capabilities": self._refine(raw, state)}

    async def poll(self, light: bool = False) -> list:
        raw = await asyncio.to_thread(self.collect, light)
        t = now()
        return [model.topology_event(raw, t), self.config_event(raw, t)]

    # ------------------------------------------------------------------ source contract
    async def run(self, emit):
        async def one_session(emit_, status: StatusTracker):
            last_full = None                      # the first poll is always a full one
            while True:
                full = last_full is None or time.monotonic() - last_full >= self.full_interval
                for ev in await self.poll(light=not full):
                    await emit_event(emit_, ev)
                if full:
                    last_full = time.monotonic()
                await status.set(emit_, True, "source.family.ok")
                await asyncio.sleep(self.interval)

        await run_with_backoff(SOURCE, MODULE, emit, one_session)

    async def test(self):
        async def probe():
            raw = await asyncio.to_thread(self.collect, False)
            st = model.to_neutral(raw, now())
            return {"version": st["controller"]["version"] or None, "nodes": len(st["hosts"]),
                    "guests": len(st["guests"]), "bridges": len(st["bridges"]), "storage": len(st["storage"]),
                    "rules": len(st["fw_rules"]) + len(st["guest_rules"])}

        return await short_test(MODULE, probe(), self._t.timeout * 30 + 10)

    def scope(self) -> str:
        """Blocking: read_only | can_write | unknown, from the token's own permission list."""
        if SUPERUSER_PRINCIPALS.match(self._principal):
            return "can_write"
        perms = self._get("/access/permissions", budget=False)
        if not isinstance(perms, dict):
            return "unknown"
        granted = set()
        for path, privs in perms.items():
            if isinstance(privs, dict):
                granted |= {str(k) for k, v in privs.items() if v not in (0, "0", False, None)}
        if not granted:
            return "unknown"
        return "read_only" if all(READ_PRIVS.match(p) for p in granted) else "can_write"

    async def account_scope(self) -> str:
        """A token that can change anything is refused by the app (read-only tokens only)."""
        return await asyncio.to_thread(self.scope)
