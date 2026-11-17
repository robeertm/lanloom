"""Write half of the Proxmox VE family (stage B: guests). Loaded only by the executor service, never by the web app.

What it can change - nothing else, whatever a plan says:

* ``guest.nic``              bridge, VLAN tag and firewall flag of one network card of a guest
* ``guest.firewall``         switch a guest's own firewall on or off
* ``guest.fw_rule``          change, switch on or switch off one firewall rule of a guest
* ``guest.fw_rule_create``   add a firewall rule to a guest (appended at the end, marked as Lanloom's own)
* ``guest.fw_rule_delete``   remove a guest firewall rule Lanloom created (only those)
* ``guest.power``            start a guest, or shut it down cleanly (never a hard stop: a guest that does not shut
                             down within the time limit keeps running and the plan is rolled back)

Never: host networking (bridges, bonds), datacenter or node firewall, creating or removing guests, disks, backups,
migration, snapshots, accounts. Those are no plan step of this family (host bridges are stage C, refused).

The write half uses an API token of its own (separate from the read-only token of the app), ideally a token with
privilege separation and a role with ``VM.Audit, VM.PowerMgmt, VM.Config.Network, SDN.Use, Sys.Audit`` on a pool of
the guests it may change. Options file (mode 0600, see ``credentials.load_write_options``)::

    {"url": "https://192.168.1.10:8006", "write_token_id": "lanloom@pve!write", "write_token_secret": "...",
     "cert_sha256": "<SHA-256 of the host's certificate>",          # or a certificate the system trusts
     "protect_guests": [100, 101], "protect_addresses": ["192.168.1.53"]}   # optional, always critical

Safety below the adapter: every request goes through ``transport.Transport`` with a field-exact allow-list. The only
writes are ``PUT …/config`` with network card values and the config digest, ``POST …/status/start``,
``POST …/status/shutdown`` (``forceStop`` must be 0), ``PUT …/firewall/options`` (``enable`` only),
``POST|PUT|DELETE …/firewall/rules`` (plain fields; a new rule must carry the Lanloom mark). The token travels in a
header; there is no sign-in.

Before every write the half reads the object fresh and compares it with the plan's ``before`` (and sends the
host's ``digest``, so a parallel change on the host makes the write fail instead of overwriting it). Asynchronous
host tasks (start, shut down) are followed to their end; a failed or timed-out task stops the plan and the executor
rolls back. A network card change that the guest can only take after a restart is reverted at once (Lanloom never
restarts a guest on its own).

Lockout rules (``firewall/guests.py``): no stop, no network card change, no rule change and no firewall switched on for a guest
that runs Lanloom or the executor, the network's name server or gateway, or is listed/tagged as protected; never for
templates, locked guests or guests whose start and stop belong to the cluster's HA manager. Product names are used
only to identify compatible products; no affiliation or endorsement.
"""
import ipaddress
import json
import logging
import re
import time
from urllib.parse import quote

from . import WRITE_CAPABILITIES, model
from .. import capabilities
from ..credentials import Secret, vet_write_account
from ..transport import AllowList, Forbidden, Rule, Transport, TransportError
from ...firewall import guests as guard, steps as stepkinds
from ...firewall.adapter import ControllerAdapter

log = logging.getLogger("lanloom.executor")

URL_RE = re.compile(r"\A(https?)://([A-Za-z0-9.-]{1,253}|\[[0-9A-Fa-f:.]{2,45}\])(?::(\d{1,5}))?/?\Z", re.I)
TOKEN_RE = re.compile(r"\A([A-Za-z0-9._-]{1,64}@[A-Za-z0-9._-]{1,64})!([A-Za-z][A-Za-z0-9._-]{0,63})\Z")
SECRET_RE = re.compile(r"\A[A-Za-z0-9-]{8,128}\Z")
OPTIONS = ("url", "write_token_id", "write_token_secret", "allow_self_signed", "cert_sha256", "protect_guests",
           "protect_addresses", "shutdown_timeout_s", "task_timeout_s", "settle_s", "timeout", "state_dir")
API = "/api2/json"
_N = r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}"
_V = r"\d{1,9}"
_G = r"[A-Za-z][A-Za-z0-9_-]{0,31}"
_DIGEST = re.compile(r"\A[0-9a-f]{40}\Z")             # the host's SHA-1 digest (schema: at most 40 characters)
_UPID = re.compile(r"\AUPID:[A-Za-z0-9.-]{1,63}:[0-9A-Fa-f]{8}:[0-9A-Fa-f]{8}:[0-9A-Fa-f]{8}:[a-z0-9]{1,32}:[A-Za-z0-9._-]{0,64}:"
                   r"[A-Za-z0-9._@!-]{1,160}:\Z")
NIC_SLOTS = tuple("net%d" % i for i in range(32))
NIC_KEYS = frozenset(("virtio", "e1000", "e1000e", "rtl8139", "vmxnet3", "ne2k_pci", "ne2k_isa", "pcnet", "i82551", "i82557b",
                      "i82559er", "model", "macaddr", "bridge", "tag", "trunks", "firewall", "rate", "link_down", "mtu", "queues",
                      "name", "hwaddr", "ip", "gw", "ip6", "gw6", "type", "host-managed"))
_NIC_VALUE = re.compile(r"\A[A-Za-z0-9_-]{1,16}=[A-Za-z0-9:._/;+-]{0,64}(?:,[A-Za-z0-9_-]{1,16}=[A-Za-z0-9:._/;+-]{0,64}){0,23}\Z")
RULE_FIELDS = ("type", "action", "enable", "proto", "dport", "source", "comment")
SETTLE_NIC_S = 5
SETTLE_START_S = 20
POLL_TASK_S = 2.0


# ---------------------------------------------------------------------------- the allow-list (below the adapter)
def _nic_value(v):
    if not isinstance(v, str) or len(v) > 600 or not _NIC_VALUE.match(v):
        return False
    return all(part.split("=", 1)[0] in NIC_KEYS for part in v.split(","))


def _digest(v):
    return isinstance(v, str) and bool(_DIGEST.match(v))


def _revert(v):
    return isinstance(v, str) and v in NIC_SLOTS


def _zero_one(v):
    return v in (0, 1) and not isinstance(v, bool)


def _zero(v):
    return v == 0 and not isinstance(v, bool)


def _timeout(v):
    return isinstance(v, int) and not isinstance(v, bool) and 10 <= v <= 900


def _pos(v):
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10000


def _check(kind, value):
    try:
        stepkinds.KINDS["guest.fw_rule"].fields[kind](value)
        return True
    except stepkinds.FieldError:
        return False


def _rule_type(v):
    return v in ("in", "out")


def _rule_action(v):
    return v in ("ACCEPT", "DROP", "REJECT")


def _rule_proto(v):
    return v in ("tcp", "udp", "icmp")


def _rule_dport(v):
    return isinstance(v, str) and v != "" and _check("ports", v)


def _rule_source(v):
    return isinstance(v, str) and v != "" and _check("source", v)


def _comment(v):
    return isinstance(v, str) and len(v) <= 96 and not any(ord(c) < 32 or ord(c) == 127 for c in v)


def _own_comment(v):
    return _comment(v) and v.startswith(model.OWN_MARK)


def _delete_list(v):
    return isinstance(v, str) and bool(v) and set(v.split(",")) <= {"proto", "dport", "source", "comment"}


_GUEST = r"/nodes/%s/(?:qemu|lxc)/%s" % (_N, _V)
READ_PATHS = (
    API + r"/(?:version|cluster/status|cluster/resources|access/permissions|cluster/sdn/zones|cluster/sdn/vnets)",
    API + r"/nodes/%s/network" % _N,
    API + _GUEST + r"/(?:config\?current=1|pending|status/current|firewall/options|firewall/rules)",
    API + r"/nodes/%s/tasks/UPID%%3A[A-Za-z0-9%%._@!-]{10,400}/status" % _N,
)
NIC_VALUES = dict({s: _nic_value for s in NIC_SLOTS}, digest=_digest, revert=_revert)
RULE_VALUES = {"type": _rule_type, "action": _rule_action, "enable": _zero_one, "proto": _rule_proto, "dport": _rule_dport,
               "source": _rule_source, "digest": _digest}


class _MarkedAllowList(AllowList):
    """The generic rules check a field only when it is present; a new guest rule must always *carry* the Lanloom mark."""

    def check(self, method, path, body=None):
        rule = super().check(method, path, body)
        if rule.method == "POST" and rule.path.pattern.endswith(r"/firewall/rules)\Z") and not (isinstance(body, dict) and body.get("comment")):
            raise Forbidden("POST rule without the mark")
        return rule


ALLOW = _MarkedAllowList(
    [Rule("GET", p) for p in READ_PATHS]
    + [Rule("PUT", API + _GUEST + r"/config", fields=tuple(NIC_VALUES), values=NIC_VALUES, nonempty=True, form=True),
       Rule("POST", API + _GUEST + r"/status/start", fields=(), form=True),
       Rule("POST", API + _GUEST + r"/status/shutdown", fields=("timeout", "forceStop"),
            values={"timeout": _timeout, "forceStop": _zero}, form=True),
       Rule("PUT", API + _GUEST + r"/firewall/options", fields=("enable", "digest"), values={"enable": _zero_one, "digest": _digest},
            nonempty=True, form=True),
       Rule("POST", API + _GUEST + r"/firewall/rules", fields=RULE_FIELDS + ("pos", "digest"),
            values=dict(RULE_VALUES, comment=_own_comment, pos=_pos), form=True),
       Rule("PUT", API + _GUEST + r"/firewall/rules/\d{1,5}", fields=RULE_FIELDS + ("delete", "digest"),
            values=dict(RULE_VALUES, comment=_comment, delete=_delete_list), nonempty=True, form=True),
       Rule("DELETE", API + _GUEST + r"/firewall/rules/\d{1,5}(?:\?digest=[0-9a-f]{40})?")], write=True)

ACTION_OUT = {"allow": "ACCEPT", "block": "DROP", "reject": "REJECT"}


class WriteError(RuntimeError):
    """A write that cannot or did not happen. ``key`` is a short reason (never a secret, never an address)."""

    def __init__(self, key: str, detail: str = ""):
        super().__init__(key)
        self.key, self.detail = key, detail


# ---------------------------------------------------------------------------- network card values
def nic_pairs(value: str) -> list:
    out = []
    for part in str(value or "").split(","):
        k, sep, v = part.partition("=")
        if k:
            out.append((k.strip(), v.strip() if sep else None))
    return out


def nic_value_with(value: str, bridge=None, vlan="keep", firewall=None) -> str:
    """The card's config value with bridge, VLAN tag and firewall flag changed; every other key unchanged."""
    pairs = nic_pairs(value)
    keys = [k for k, _ in pairs]

    def put(key, v):
        if v is None:
            pairs[:] = [(k, x) for k, x in pairs if k != key]
        elif key in keys:
            pairs[:] = [(k, (v if k == key else x)) for k, x in pairs]
        else:
            idx = next((i + 1 for i, (k, _) in enumerate(pairs) if k == "bridge"), len(pairs))
            pairs.insert(idx, (key, v))
        keys[:] = [k for k, _ in pairs]

    if bridge is not None:
        put("bridge", bridge)
    if vlan != "keep":
        put("tag", None if vlan is None else str(int(vlan)))
    if firewall is not None:
        put("firewall", "1" if firewall else None)
    return ",".join(k if v is None else "%s=%s" % (k, v) for k, v in pairs)


def nic_fields(value: str) -> dict:
    kv = model.parse_kv(value)
    tag = model.num(kv.get("tag"))
    return {"bridge": model.text(kv.get("bridge"), 32), "vlan": tag if isinstance(tag, int) and 1 <= tag <= 4094 else None,
            "firewall": model.flag(kv.get("firewall"))}


def rule_fields(r: dict) -> dict:
    """The neutral fields of a raw guest rule (the ones a plan step compares and writes)."""
    n = model._rule(r, "guest", "-", 0)
    return {"name": n["name"], "direction": n["direction"], "action": n["action"], "proto": n["proto"],
            "ports": n["ports"], "source": n["source"] or "", "enabled": n["enabled"]}


class ProxmoxWriter(ControllerAdapter):
    label = "Proxmox VE (local API)"
    kind = "controller"
    family = "proxmox"

    def __init__(self, url=None, write_token_id=None, write_token_secret=None, allow_self_signed=False, cert_sha256=None,
                 protect_guests=(), protect_addresses=(), shutdown_timeout_s=120, task_timeout_s=None, settle_s=None,
                 timeout=15.0, state_dir=None, *, clock=time.time, sleep=time.sleep, identity=None, **unknown):
        if unknown:
            raise ValueError("unknown option(s): %s (known: %s)" % (", ".join(sorted(unknown)), ", ".join(OPTIONS)))
        m = URL_RE.match(str(url or "").strip())
        if not m:
            raise ValueError("url: https://<address of a node>:8006")
        port = int(m.group(3)) if m.group(3) else 8006
        if not 0 < port < 65536:
            raise ValueError("url: port")
        self._host = m.group(2).strip("[]")
        base = "%s://%s:%d" % (m.group(1).lower(), m.group(2), port)
        tid = str(write_token_id or "").strip()
        tm = TOKEN_RE.match(tid)
        if not tm:
            raise ValueError("write_token_id must look like user@realm!name")
        self._token_id, self._principal = tid, tm.group(1)
        self._secret = write_token_secret if isinstance(write_token_secret, Secret) else Secret(write_token_secret)
        if not SECRET_RE.match(self._secret.reveal()):
            raise ValueError("write_token_secret missing or invalid")
        ok, _ = vet_write_account({"principal": self._principal})
        if not ok:
            raise ValueError("the superuser's token is refused: create a token of a user with a limited role "
                             "(see docs/integrations/proxmox.md)")
        if allow_self_signed and not cert_sha256:
            log.warning("proxmox: allow_self_signed without cert_sha256 - the connection is not bound to the host's "
                        "certificate; set cert_sha256 (see docs/integrations/proxmox.md)")
        self._t = Transport(base, ALLOW, auth_headers=self._auth, allow_self_signed=bool(allow_self_signed),
                            timeout=float(timeout), pin_sha256=cert_sha256 or None)
        self._protect_vmids = tuple(int(v) for v in (protect_guests or ()) if str(v).isdigit())
        self._protect_addrs = {}
        for a in protect_addresses or ():
            try:
                self._protect_addrs[str(ipaddress.ip_address(str(a)))] = "hv.why.listed"
            except ValueError:
                raise ValueError("protect_addresses: %r is not an address" % (a,)) from None
        self.shutdown_timeout_s = max(10, min(900, int(shutdown_timeout_s or 120)))
        self.task_timeout_s = int(task_timeout_s) if task_timeout_s else self.shutdown_timeout_s + 60
        self.settle_s = SETTLE_START_S if settle_s is None else max(0, int(settle_s))
        self.clock, self.sleep = clock, sleep
        self._identity = identity            # callable(host) -> ({address: reason}, {mac hash: reason}); see use_identity
        self._ident_cache = None
        self._account_ok = None
        self._raw = {}
        self._refs = {}                      # neutral id -> (area, guest key, node, slot/pos)
        self._state = None
        self._probe_fail = 0
        self._touched = None                 # ids a step of the running plan reached (None: unknown, e.g. after a crash)
        self.writes = []                     # (method, path without the node/guest part) of every write, for tests
        self._dead = None

    def __repr__(self):
        return "ProxmoxWriter(%s)" % self._host

    __str__ = __repr__

    def _auth(self) -> dict:
        return {"Authorization": "PVEAPIToken=%s=%s" % (self._token_id, self._secret.reveal())}

    # ------------------------------------------------------------------ contract basics
    def capabilities(self) -> dict:
        return capabilities.validate(WRITE_CAPABILITIES, self.family)

    def use_identity(self, identity) -> None:
        """What the executor knows about itself and its network (``lanloom.executor.local_identity``): a callable
        ``(api host) -> ({address: reason}, {mac hash: reason})``. Without it only listed and tagged guests are
        critical, so the executor always sets it."""
        self._identity = identity
        self._ident_cache = None

    def endpoints(self) -> list:
        return [self._host]

    # ------------------------------------------------------------------ requests
    def _send(self, method: str, rel: str, body=None):
        try:
            status, _, data = self._t.request(method, API + rel, body)
        except Forbidden:
            raise WriteError("proxmox.refused_by_allow_list") from None
        except TransportError as exc:
            raise WriteError(exc.key) from None
        if status == 401:
            self._dead = "401"
            raise WriteError("proxmox.token_refused")
        if status == 403:
            raise WriteError("proxmox.forbidden")         # the token lacks a right on this object
        parsed = None
        if data:
            try:
                parsed = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                parsed = None
        return status, parsed.get("data") if isinstance(parsed, dict) else None, parsed

    def _get(self, rel: str, optional: bool = False):
        status, data, _ = self._send("GET", rel)
        if status in (400, 404, 405, 500, 501, 595, 596) and optional:
            return None
        if not 200 <= status < 300:
            raise WriteError("proxmox.read_failed", "status %d" % status)
        return data

    def _write(self, method: str, rel: str, body=None):
        status, data, parsed = self._send(method, rel, body)
        if not 200 <= status < 300:
            why = ""
            if isinstance(parsed, dict) and isinstance(parsed.get("errors"), dict):
                why = ",".join(sorted(str(k)[:24] for k in parsed["errors"]))[:80]
            raise WriteError("proxmox.write_refused", "status %d %s" % (status, why))
        self.writes.append((method, re.sub(r"/nodes/[^/]+/(qemu|lxc)/\d+", r"/<\1>", rel.split("?", 1)[0])))
        return data

    # ------------------------------------------------------------------ account check
    def account(self) -> tuple:
        """(ok, warnings): refused when the token can change nothing this half needs; a token that could change much
        more (system settings, permissions, guests' existence) gets a warning."""
        perms = self._get("/access/permissions", optional=True)
        granted = set()
        broad = set()
        for path, privs in (perms or {}).items() if isinstance(perms, dict) else ():
            if not isinstance(privs, dict):
                continue
            on = {str(k) for k, v in privs.items() if v not in (0, "0", False, None)}
            granted |= on
            broad |= on & {"Sys.Modify", "Permissions.Modify", "VM.Allocate", "Datastore.Allocate", "User.Modify", "Sys.PowerMgmt"}
        warnings = ["owner_role"] if broad else []
        if not granted & {"VM.PowerMgmt", "VM.Config.Network"}:
            return False, warnings + ["cannot_write"]
        return True, warnings

    # ------------------------------------------------------------------ reading
    def _guest_rel(self, kind: str, node: str, vmid) -> str:
        if kind not in ("qemu", "lxc") or not re.fullmatch(_N, str(node or "")) or not re.fullmatch(_V, str(vmid)):
            raise WriteError("proxmox.guest_unknown")
        return "/nodes/%s/%s/%s" % (node, kind, vmid)

    def _read_guest(self, kind: str, node: str, vmid) -> dict:
        base = self._guest_rel(kind, node, vmid)
        out = {"node": node, "config": self._get(base + "/config?current=1", optional=True)}
        out["fw_options"] = self._get(base + "/firewall/options", optional=True)
        out["fw_rules"] = self._get(base + "/firewall/rules", optional=True)
        return out

    def _read_raw(self) -> dict:
        raw = {"api_host": self._host, "version": self._get("/version"), "cluster": self._get("/cluster/status", optional=True),
               "resources": self._get("/cluster/resources", optional=True)}
        res = raw["resources"] if isinstance(raw["resources"], list) else []
        nodes = sorted({str(r.get("node")) for r in res if isinstance(r, dict) and r.get("type") == "node"
                        and str(r.get("status") or "") == "online" and re.fullmatch(_N, str(r.get("node") or ""))})
        raw["nodes"] = {n: {"network": self._get("/nodes/%s/network" % n, optional=True)} for n in nodes}
        raw["sdn"] = {"zones": self._get("/cluster/sdn/zones", optional=True), "vnets": self._get("/cluster/sdn/vnets", optional=True),
                      "subnets": {}}
        guests = {}
        for r in res:
            if not isinstance(r, dict) or r.get("type") not in ("qemu", "lxc") or not re.fullmatch(_V, str(r.get("vmid") or "")):
                continue
            if str(r.get("node") or "") not in nodes:
                continue
            key = model.guest_key(r["type"], r["vmid"])
            if len(guests) < 400:
                guests[key] = self._read_guest(r["type"], r["node"], r["vmid"])
        raw["guests"] = guests
        return raw

    def _index(self, raw: dict, state: dict) -> None:
        refs = {}
        res = {model.guest_key(r["type"], r["vmid"]): r for r in raw.get("resources") or []
               if isinstance(r, dict) and r.get("type") in ("qemu", "lxc") and r.get("vmid") is not None}
        for key, r in res.items():
            kind, vmid = key.split("/")
            gid = model.oid("g", key)
            refs[gid] = ("guests", kind, r.get("node"), int(vmid), None)
            cfg = (raw.get("guests") or {}).get(key, {}).get("config") or {}
            for slot in cfg if isinstance(cfg, dict) else ():
                if slot in NIC_SLOTS:
                    refs[model.oid("nic", "%s/%s" % (gid, slot))] = ("guest_nics", kind, r.get("node"), int(vmid), slot)
            for i, rule in enumerate((raw.get("guests") or {}).get(key, {}).get("fw_rules") or []):
                if isinstance(rule, dict):
                    pos = model.num(rule.get("pos"))
                    pos = pos if isinstance(pos, int) else i
                    refs[model.rule_id("guest", gid, pos)] = ("guest_rules", kind, r.get("node"), int(vmid), pos)
        self._refs = refs

    def read(self) -> dict:
        raw = self._read_raw()
        state = model.to_neutral(raw, self.clock())
        state["controller"] = dict(state.get("controller") or {}, label=self.label, kind="hypervisor")
        state["side_effects"] = {}
        self._raw, self._state = raw, state
        self._index(raw, state)
        return state

    def health(self) -> tuple:
        if self._dead:
            return False, "token refused"
        try:
            self._get("/version")
            if self._account_ok is None:
                ok, warnings = self.account()
                self._account_ok = ok
                if "owner_role" in warnings:
                    log.warning("proxmox: the write token could change much more than guests (system, permissions, "
                                "guest creation); a role on a pool of guests is enough (see docs/integrations/proxmox.md)")
            if not self._account_ok:
                return False, "token cannot write"
        except WriteError as exc:
            return False, exc.key
        return True, ""

    # ------------------------------------------------------------------ lockout
    def _critical(self, state: dict) -> dict:
        if self._ident_cache is None:
            addrs, macs = self._identity(self._host) if self._identity is not None else ({}, {})
            addrs = dict(addrs)
            addrs.update(self._protect_addrs)
            addrs.pop(self._host, None)                      # the host's own API address is not a guest
            self._ident_cache = (addrs, dict(macs))
        addrs, macs = self._ident_cache
        return guard.critical_guests(state, addrs, macs, self._protect_vmids)

    def lockout(self, state: dict, step: dict):
        if not step.get("kind", "").startswith("guest."):
            return "fw.block.capability"
        why = guard.step_lockout(state, step, self._critical(state))
        if why:
            return why
        if step["kind"] in ("guest.fw_rule", "guest.fw_rule_create"):
            rule = dict(self._rule_now(step) or {}, **step["after"])
            if rule.get("ports") and rule.get("proto") not in ("tcp", "udp"):
                return "fw.block.hv_rule_ports"
        return None

    def plan_lockout(self, state: dict, steps: list):
        return guard.plan_lockout(state, steps)

    def _rule_now(self, step):
        if step["kind"] == "guest.fw_rule_create":
            return {}
        return next((r for r in (self._state or {}).get("guest_rules") or [] if r["id"] == step.get("object")), None)

    # ------------------------------------------------------------------ helpers per object
    def _ref(self, oid: str, area: str) -> tuple:
        ref = self._refs.get(oid)
        if not ref or ref[0] != area:
            self.read()
            ref = self._refs.get(oid)
        if not ref or ref[0] != area:
            raise WriteError("proxmox.object_unknown")
        return ref

    def _config(self, kind, node, vmid) -> dict:
        cfg = self._get(self._guest_rel(kind, node, vmid) + "/config?current=1")
        if not isinstance(cfg, dict):
            raise WriteError("proxmox.read_failed")
        return cfg

    def _status(self, kind, node, vmid) -> dict:
        st = self._get(self._guest_rel(kind, node, vmid) + "/status/current")
        if not isinstance(st, dict):
            raise WriteError("proxmox.read_failed")
        return st

    def _rules(self, kind, node, vmid) -> list:
        rules = self._get(self._guest_rel(kind, node, vmid) + "/firewall/rules")
        return [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []

    def _options(self, kind, node, vmid) -> dict:
        o = self._get(self._guest_rel(kind, node, vmid) + "/firewall/options")
        return o if isinstance(o, dict) else {}

    @staticmethod
    def _rule_at(rules: list, pos: int):
        for i, r in enumerate(rules):
            p = model.num(r.get("pos"))
            if (p if isinstance(p, int) else i) == pos:
                return r
        return None

    @staticmethod
    def _digest_of(obj) -> dict:
        d = obj.get("digest") if isinstance(obj, dict) else None
        return {"digest": d} if _digest(d) else {}

    def _rules_digest(self, rules: list) -> dict:
        for r in rules:
            d = self._digest_of(r)
            if d:
                return d
        return {}

    def _wait_task(self, node: str, upid) -> None:
        if not isinstance(upid, str) or not _UPID.match(upid):
            raise WriteError("proxmox.task_unknown")
        rel = "/nodes/%s/tasks/%s/status" % (node, quote(upid, safe=""))
        until = self.clock() + self.task_timeout_s
        while True:
            st = self._get(rel)
            if isinstance(st, dict) and st.get("status") == "stopped":
                if str(st.get("exitstatus") or "") != "OK":
                    raise WriteError("proxmox.task_failed", str(st.get("exitstatus") or "")[:60])
                return
            if self.clock() >= until:
                raise WriteError("proxmox.task_timeout")
            self.sleep(POLL_TASK_S)

    def _check_pending(self, kind, node, vmid, slot: str) -> None:
        """A card change the running guest cannot take now stays pending on the host: revert it and stop."""
        pend = self._get(self._guest_rel(kind, node, vmid) + "/pending", optional=True)
        for p in pend if isinstance(pend, list) else []:
            if isinstance(p, dict) and p.get("key") == slot and ("pending" in p or p.get("delete")):
                cfg = self._config(kind, node, vmid)
                self._write("PUT", self._guest_rel(kind, node, vmid) + "/config", dict({"revert": slot}, **self._digest_of(cfg)))
                raise WriteError("proxmox.needs_restart")

    # ------------------------------------------------------------------ plan steps
    def apply(self, step: dict):
        kind = step["kind"]
        if self._touched is not None:        # before the first byte: a write that fails half-way is still put back
            self._touched.add(step["after"].get("guest") if kind == "guest.fw_rule_create" else stepkinds.step_object(step))
        if kind == "guest.nic":
            return self._apply_nic(step)
        if kind == "guest.power":
            return self._apply_power(step["object"], step["before"], step["after"]["status"])
        if kind == "guest.firewall":
            return self._apply_firewall(step["object"], step["before"], step["after"]["firewall"])
        if kind == "guest.fw_rule":
            return self._apply_rule(step)
        if kind == "guest.fw_rule_create":
            return self._create_rule(step["after"])
        if kind == "guest.fw_rule_delete":
            return self._delete_rule(step["object"], step["before"])
        raise WriteError("proxmox.kind_not_supported")

    def _apply_nic(self, step: dict):
        _, kind, node, vmid, slot = self._ref(step["object"], "guest_nics")
        cfg = self._config(kind, node, vmid)
        if cfg.get("lock"):
            raise WriteError("proxmox.locked")
        value = cfg.get(slot)
        if not isinstance(value, str):
            raise WriteError("proxmox.object_unknown")
        now = nic_fields(value)
        after = step["after"]
        if all(now.get(f) == v for f, v in after.items()):
            return None                                       # already as planned (idempotent)
        if any(now.get(f) != v for f, v in step["before"].items()):
            raise WriteError("proxmox.changed")               # changed on the host since the plan was made
        new = nic_value_with(value, bridge=after.get("bridge"), vlan=after["vlan"] if "vlan" in after else "keep",
                             firewall=after.get("firewall"))
        self._write("PUT", self._guest_rel(kind, node, vmid) + "/config", dict({slot: new}, **self._digest_of(cfg)))
        self._check_pending(kind, node, vmid, slot)
        got = nic_fields(self._config(kind, node, vmid).get(slot))
        if any(got.get(f) != v for f, v in after.items()):
            raise WriteError("proxmox.not_applied")
        return None

    def _apply_power(self, gid: str, before: dict, status: str):
        _, kind, node, vmid, _ = self._ref(gid, "guests")
        cur = self._status(kind, node, vmid)
        now = "running" if cur.get("status") == "running" else "stopped" if cur.get("status") == "stopped" else str(cur.get("status"))
        if now == status:
            return None
        if before and now != before.get("status"):
            raise WriteError("proxmox.changed")
        if cur.get("lock"):
            raise WriteError("proxmox.locked")
        if cur.get("ha") and isinstance(cur["ha"], dict) and cur["ha"].get("managed"):
            raise WriteError("proxmox.ha_managed")
        rel = self._guest_rel(kind, node, vmid)
        if status == "running":
            upid = self._write("POST", rel + "/status/start", {})
        else:
            upid = self._write("POST", rel + "/status/shutdown", {"timeout": self.shutdown_timeout_s, "forceStop": 0})
        self._wait_task(node, upid)
        until = self.clock() + 30
        while True:
            if self._status(kind, node, vmid).get("status") == status:
                return None
            if self.clock() >= until:
                raise WriteError("proxmox.not_applied")
            self.sleep(POLL_TASK_S)

    def _apply_firewall(self, gid: str, before: dict, on: bool):
        _, kind, node, vmid, _ = self._ref(gid, "guests")
        opts = self._options(kind, node, vmid)
        now = model.flag(opts.get("enable"))
        if now == on:
            return None
        if before and before.get("firewall") is not None and now != before["firewall"]:
            raise WriteError("proxmox.changed")
        rel = self._guest_rel(kind, node, vmid) + "/firewall/options"
        self._write("PUT", rel, dict({"enable": 1 if on else 0}, **self._digest_of(opts)))
        if model.flag(self._options(kind, node, vmid).get("enable")) != on:
            raise WriteError("proxmox.not_applied")
        return None

    @staticmethod
    def _rule_body(fields: dict, own: bool, current: dict = None) -> dict:
        """Neutral rule fields -> request fields (``delete`` lists the fields that become empty)."""
        body, delete = {}, []
        for f, v in fields.items():
            if f == "enabled":
                body["enable"] = 1 if v else 0
            elif f == "action":
                body["action"] = ACTION_OUT[v]
            elif f == "direction":
                body["type"] = v
            elif f == "proto":
                if v == "any":
                    delete.append("proto")
                else:
                    body["proto"] = v
            elif f == "ports":
                if v:
                    body["dport"] = v
                else:
                    delete.append("dport")
            elif f == "source":
                if v:
                    body["source"] = v
                else:
                    delete.append("source")
            elif f == "name":
                text = ("%s %s" % (model.OWN_MARK, v)).strip() if own else v
                if text:
                    body["comment"] = text[:96]
                else:
                    delete.append("comment")
        if current is not None:
            delete = [d for d in delete if (current.get(d) not in (None, ""))]
        if delete:
            body["delete"] = ",".join(sorted(set(delete)))
        return body

    def _apply_rule(self, step: dict):
        _, kind, node, vmid, pos = self._ref(step["object"], "guest_rules")
        rules = self._rules(kind, node, vmid)
        raw = self._rule_at(rules, pos)
        if raw is None:
            raise WriteError("proxmox.object_unknown")
        now = rule_fields(raw)
        after = step["after"]
        if all(now.get(f) == v for f, v in after.items()):
            return None
        if any(now.get(f) != v for f, v in step["before"].items()):
            raise WriteError("proxmox.changed")
        own = bool(str(raw.get("comment") or "").startswith(model.OWN_MARK))
        body = self._rule_body(after, own, raw)
        if not body:
            return None
        body.update(self._digest_of(raw) or self._rules_digest(rules))
        self._write("PUT", self._guest_rel(kind, node, vmid) + "/firewall/rules/%d" % pos, body)
        got = self._rule_at(self._rules(kind, node, vmid), pos)
        if got is None or any(rule_fields(got).get(f) != v for f, v in after.items()):
            raise WriteError("proxmox.not_applied")
        return None

    def _create_rule(self, after: dict):
        _, kind, node, vmid, _ = self._ref(after["guest"], "guests")
        rules = self._rules(kind, node, vmid)
        pos = len(rules)
        fields = {f: after[f] for f in ("direction", "action", "enabled", "proto", "ports", "source") if f in after}
        body = self._rule_body(fields, True)
        body.pop("delete", None)                              # a new rule has nothing to clear
        body["comment"] = ("%s %s" % (model.OWN_MARK, after.get("name") or "")).strip()[:96]
        body["pos"] = pos
        body.update(self._rules_digest(rules))
        self._write("POST", self._guest_rel(kind, node, vmid) + "/firewall/rules", body)
        got = self._rule_at(self._rules(kind, node, vmid), pos)
        if got is None or str(got.get("comment") or "") != body["comment"] or any(rule_fields(got).get(f) != v for f, v in fields.items()):
            raise WriteError("proxmox.not_applied")
        gid = model.oid("g", model.guest_key(kind, vmid))
        new_id = model.rule_id("guest", gid, pos)
        self._refs[new_id] = ("guest_rules", kind, node, vmid, pos)
        return {"id": new_id}

    def _delete_rule(self, oid: str, before: dict, require_own: bool = True):
        _, kind, node, vmid, pos = self._ref(oid, "guest_rules")
        rules = self._rules(kind, node, vmid)
        raw = self._rule_at(rules, pos)
        if raw is None:
            return None                                       # already gone (idempotent)
        if require_own and not str(raw.get("comment") or "").startswith(model.OWN_MARK):
            raise WriteError("proxmox.not_own")
        now = rule_fields(raw)
        if before and any(now.get(f) != v for f, v in before.items() if f != "guest"):
            raise WriteError("proxmox.changed")
        dg = self._digest_of(raw) or self._rules_digest(rules)
        self._write("DELETE", self._guest_rel(kind, node, vmid) + "/firewall/rules/%d%s" % (pos, "?digest=" + dg["digest"] if dg else ""))
        if len(self._rules(kind, node, vmid)) != len(rules) - 1:
            raise WriteError("proxmox.not_applied")
        return None

    # ------------------------------------------------------------------ backup and the way back
    def snapshot(self, ids) -> dict:
        """The touched objects as stored now, read fresh (the way back writes exactly these)."""
        out = {"guests": [], "guest_nics": [], "guest_rules": []}
        self._touched = set()
        if not self._refs:
            self.read()
        for oid in ids:
            if not isinstance(oid, str) or oid.startswith("new:"):
                continue
            ref = self._refs.get(oid)
            if not ref:
                self.read()
                ref = self._refs.get(oid)
            if not ref:
                raise WriteError("proxmox.object_unknown")
            area, kind, node, vmid, extra = ref
            item = {"id": oid, "kind": kind, "node": node, "vmid": vmid}
            if area == "guests":
                st = self._status(kind, node, vmid)
                item.update(status="running" if st.get("status") == "running" else "stopped",
                            firewall=model.flag(self._options(kind, node, vmid).get("enable")))
            elif area == "guest_nics":
                item.update(slot=extra, value=self._config(kind, node, vmid).get(extra))
            else:
                rules = self._rules(kind, node, vmid)
                raw = self._rule_at(rules, extra)
                if raw is None:
                    raise WriteError("proxmox.object_unknown")
                item.update(pos=extra, count=len(rules), rule={k: raw.get(k) for k in RULE_FIELDS + ("macro", "dest", "sport", "iface", "log") if raw.get(k) is not None})
            out[area].append(item)
        return out

    def restore(self, snapshot: dict) -> None:
        """Put every touched object back as saved: rules the plan created go first, then rules, cards, firewall
        switches, and the power state last. A failure is retried once after the others."""
        snap = snapshot or {}
        reached = self._touched

        def mine(item):
            # an object no step reached was not changed by this plan: leave it alone (someone else may have changed it
            # since the backup); after a crash nothing is known, and everything saved goes back
            return reached is None or item.get("id") in reached

        snap = dict(snap, **{area: [i for i in snap.get(area) or [] if isinstance(i, dict) and mine(i)]
                             for area in ("guests", "guest_nics", "guest_rules")})
        todo = []
        for cid in snap.get("created") or []:
            todo.append(("created", cid))
        for item in snap.get("guest_rules") or []:
            todo.append(("rule", item))
        for item in snap.get("guest_nics") or []:
            todo.append(("nic", item))
        for item in snap.get("guests") or []:
            todo.append(("firewall", item))
        for item in snap.get("guests") or []:
            todo.append(("power", item))
        failed = []
        for job in todo:
            try:
                self._restore_one(*job)
            except WriteError:
                failed.append(job)
        for job in failed:
            self._restore_one(*job)

    def _restore_one(self, what: str, item) -> None:
        if what == "created":
            if item not in self._refs:
                self.read()
            if item in self._refs:
                self._delete_rule(item, None)
            return
        kind, node, vmid = item.get("kind"), item.get("node"), item.get("vmid")
        if what == "nic":
            cfg = self._config(kind, node, vmid)
            slot, value = item.get("slot"), item.get("value")
            if slot in NIC_SLOTS and isinstance(value, str) and cfg.get(slot) != value:
                self._write("PUT", self._guest_rel(kind, node, vmid) + "/config", dict({slot: value}, **self._digest_of(cfg)))
                self._check_pending(kind, node, vmid, slot)
        elif what == "firewall":
            if item.get("firewall") is not None:
                gid = item["id"]
                self._apply_firewall(gid, None, bool(item["firewall"]))
        elif what == "power":
            self._apply_power(item["id"], None, item.get("status") or "stopped")
        elif what == "rule":
            pos, saved = item.get("pos"), item.get("rule") or {}
            rules = self._rules(kind, node, vmid)
            raw = self._rule_at(rules, pos)
            own = str(saved.get("comment") or "").startswith(model.OWN_MARK)
            if own and (raw is None or len(rules) < int(item.get("count") or 0)):
                # removed by the plan: back again at its old place (only Lanloom's own rules can have been removed)
                body = {k: saved[k] for k in RULE_FIELDS if k in saved}
                body.update(pos=pos, **self._rules_digest(rules))
                self._write("POST", self._guest_rel(kind, node, vmid) + "/firewall/rules", body)
                return
            if raw is None:
                raise WriteError("proxmox.object_unknown")
            want, now = rule_fields(saved), rule_fields(raw)
            diff = {f: v for f, v in want.items() if now.get(f) != v}
            if diff:
                body = self._rule_body(diff, own, raw)
                body.update(self._digest_of(raw) or self._rules_digest(rules))
                self._write("PUT", self._guest_rel(kind, node, vmid) + "/firewall/rules/%d" % pos, body)

    # ------------------------------------------------------------------ after the change
    def settle(self, steps: list) -> None:
        wait = 0
        for st in steps:
            if st["kind"] == "guest.power" and st["after"].get("status") == "running":
                wait = max(wait, self.settle_s)
            elif st["kind"] == "guest.nic":
                wait = max(wait, SETTLE_NIC_S)
        if wait:
            self.sleep(wait)

    def probe(self, steps: list) -> dict:
        """Watch: every touched object still as planned (a started guest still running, a card still on its bridge and
        VLAN, a rule still as written). Two failed reads of the host in a row count as a global failure."""
        missing = []
        try:
            for st in steps:
                oid = stepkinds.step_object(st)
                kind = st["kind"]
                if kind == "guest.fw_rule_create" or kind == "guest.fw_rule_delete":
                    continue                                   # checked right after writing; positions may move later
                if kind == "guest.power":
                    _, k, n, v, _ = self._ref(oid, "guests")
                    if self._status(k, n, v).get("status") != st["after"]["status"]:
                        missing.append(oid)
                elif kind == "guest.firewall":
                    _, k, n, v, _ = self._ref(oid, "guests")
                    if model.flag(self._options(k, n, v).get("enable")) != st["after"]["firewall"]:
                        missing.append(oid)
                elif kind == "guest.nic":
                    _, k, n, v, slot = self._ref(oid, "guest_nics")
                    got = nic_fields(self._config(k, n, v).get(slot))
                    if any(got.get(f) != x for f, x in st["after"].items()):
                        missing.append(oid)
                elif kind == "guest.fw_rule":
                    _, k, n, v, pos = self._ref(oid, "guest_rules")
                    raw = self._rule_at(self._rules(k, n, v), pos)
                    if raw is None or any(rule_fields(raw).get(f) != x for f, x in st["after"].items()):
                        missing.append(oid)
        except WriteError:
            self._probe_fail += 1
            return {"global_ok": self._probe_fail < 2, "missing": []}
        self._probe_fail = 0
        return {"global_ok": True, "missing": missing}

