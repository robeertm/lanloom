"""Write half of the OPNsense family (stage B: firewall rules). Loaded only by the executor service, never by the app.

What it can change - nothing else, whatever a plan says:

* ``policy.create``   add a rule to the rule API (on one interface, going in, appended after the existing rules,
                      marked as Lanloom's own with ``[lanloom]`` at the start of its description)
* ``policy.update``   change name, action, protocol, destination port, source/destination zone, or switch a rule on
                      and off (only simple rules: one interface, going in, no aliases, schedule or policy routing)
* ``policy.delete``   remove a rule Lanloom created (only those)

Never: interfaces, VLANs, NAT, aliases, address service, VPN, users, system settings, the classic rules and the
automatic ones (anti-lockout, ...). Those are no plan step of this family.

The write half uses an API key of its own (a user separate from the read-only one of the app) with only the
privilege for the rule API (see docs/integrations/opnsense.md). Options file (mode 0600, see
``credentials.load_write_options``)::

    {"url": "https://192.168.1.1", "write_api_key": "...", "write_api_secret": "...",
     "cert_sha256": "<SHA-256 of the firewall's certificate>"}        # or a certificate the system trusts

Safety below the adapter: every request goes through ``transport.Transport`` with a field-exact allow-list. The only
writes are ``POST .../filter/addRule`` (a complete rule of the fields below, the description must carry the mark),
``POST .../filter/setRule/<uuid>`` (some of those fields), ``POST .../filter/delRule/<uuid>``, and the steps of
putting rules live: ``savepoint``, ``apply`` (with or without a savepoint revision) and ``cancelRollback/<revision>``.
Every value is checked (one interface name, an interface's network or address, ``any``, a port or one range, ...).

The way back has two layers:

1. Lanloom's own: before the first write every touched rule is read and kept; on any trouble the executor writes the
   kept rules back (rules the plan created are deleted) and applies.
2. The firewall's own, on the releases that have it (``model.native_rollback``: the rule API plug-in of 23.x, built
   in from 24.1 up to 26.1): a savepoint before the first write, ``apply/<revision>`` starts a 60 second timer on the
   firewall that puts the rules of the savepoint back by itself, and only after the half has read every changed rule
   back as planned does it send ``cancelRollback``. If the executor dies or loses its way to the firewall in between,
   the firewall reverts on its own. Releases from 26.7 on dropped the savepoint: there only layer 1 applies, and the
   capability list says so (``rollback.device_native`` false). Unknown releases: the half asks for a savepoint and
   takes the answer.

Writes go into the configuration first and become live only in ``settle`` (one apply for the whole plan), so a plan
that fails half-way never puts half a rule set live.

Lockout rules on top of the neutral ones (``firewall/zonefw.py``: no blocking rule on the gateway zone, the path from
the executor to the firewall stays open): a rule may only sit on an interface (not a group, not floating), its target
is an interface's network, the firewall itself or any (never a WAN interface's network: a target on the internet side
is written with aliases, which this half does not touch). Product names are used only to identify compatible
products; no affiliation or endorsement.
"""
import base64
import ipaddress
import json
import logging
import re
import time

from . import WRITE_CAPABILITIES, model
from .. import capabilities
from ..credentials import Secret
from ..transport import AllowList, Forbidden, Rule, Transport, TransportError
from ...firewall import steps as stepkinds, zonefw
from ...firewall.adapter import ControllerAdapter

log = logging.getLogger("lanloom.executor")

URL_RE = re.compile(r"\A(https?)://([A-Za-z0-9.-]{1,253}|\[[0-9A-Fa-f:.]{2,45}\])(?::(\d{1,5}))?/?\Z", re.I)
KEY_RE = re.compile(r"\A[A-Za-z0-9+/=]{16,128}\Z")
OPTIONS = ("url", "write_api_key", "write_api_secret", "allow_self_signed", "cert_sha256", "settle_s", "timeout")
API = "/api/firewall/filter"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_REV = r"\d{9,11}(?:\.\d{1,6})?"
REV_RE = re.compile(r"\A%s\Z" % _REV)
SETTLE_S = 3
READ_PATHS = (
    re.escape(API + "/get"),
    re.escape(API + "/getRule/") + _UUID,
    re.escape("/api/interfaces/overview/interfacesInfo"),
    re.escape("/api/firewall/group/searchItem"),
    re.escape("/api/firewall/alias/get"),
    re.escape("/api/diagnostics/system/systemInformation"),
    re.escape("/api/core/firmware/status"),
)

# ---------------------------------------------------------------------------- the rule fields the half may write
_IFACE = re.compile(r"\A[a-z][a-z0-9_]{0,31}\Z")
_TEXT_BAD = re.compile(r"[\x00-\x1f\x7f]")


def _net_value(v) -> bool:
    """``any``, the firewall itself, an interface's network (``opt1``) or an interface's address (``opt1ip``)."""
    return isinstance(v, str) and (v in ("any", "(self)") or bool(_IFACE.match(v)))


def _port_value(v) -> bool:
    if v == "":
        return True
    m = re.fullmatch(r"(\d{1,5})(?::(\d{1,5}))?", v) if isinstance(v, str) else None
    return bool(m) and 1 <= int(m.group(1)) <= 65535 and (not m.group(2) or int(m.group(1)) <= int(m.group(2)) <= 65535)


def _description(v) -> bool:
    return isinstance(v, str) and len(v) <= 255 and not _TEXT_BAD.search(v)


FIELD_CHECKS = {
    "enabled": lambda v: v in ("0", "1"),
    "action": lambda v: v in ("pass", "block", "reject"),
    "quick": lambda v: v == "1",
    "interface": lambda v: isinstance(v, str) and bool(_IFACE.match(v)),
    "direction": lambda v: v == "in",
    "ipprotocol": lambda v: v in ("inet", "inet6", "inet46"),
    "protocol": lambda v: v in model.PROTOS_OUT.values(),
    "source_net": _net_value,
    "source_not": lambda v: v == "0",
    "destination_net": _net_value,
    "destination_not": lambda v: v == "0",
    "destination_port": _port_value,
    "description": _description,
    "sequence": lambda v: isinstance(v, str) and v.isdigit() and 1 <= int(v) <= 999999,
}
CREATE_KEYS = frozenset(FIELD_CHECKS)
UPDATE_KEYS = frozenset(FIELD_CHECKS) - {"quick", "direction", "source_not", "destination_not", "ipprotocol"}


def _rule_body(check_keys, exact: bool, own: bool):
    def check(rule) -> bool:
        if not isinstance(rule, dict) or not rule or not set(rule) <= check_keys:
            return False
        if exact and set(rule) != check_keys:
            return False
        if own and not str(rule.get("description") or "").startswith(model.OWN_MARK):
            return False
        return all(FIELD_CHECKS[k](v) is True for k, v in rule.items())
    return check


ALLOW = AllowList(
    [Rule("GET", p) for p in READ_PATHS]
    + [Rule("POST", re.escape(API + "/addRule"), fields=("rule",), values={"rule": _rule_body(CREATE_KEYS, True, True)}, exact=True),
       Rule("POST", re.escape(API + "/setRule/") + _UUID, fields=("rule",), values={"rule": _rule_body(UPDATE_KEYS, False, False)},
            exact=True),
       Rule("POST", re.escape(API + "/delRule/") + _UUID, fields=()),
       Rule("POST", re.escape(API + "/savepoint"), fields=()),
       Rule("POST", re.escape(API + "/apply") + "(?:/%s)?" % _REV, fields=()),
       Rule("POST", re.escape(API + "/cancelRollback/") + _REV, fields=())], write=True)


class WriteError(RuntimeError):
    """A write that cannot or did not happen. ``key`` is a short reason (never a secret, never an address)."""

    def __init__(self, key: str, detail: str = ""):
        super().__init__(key)
        self.key, self.detail = key, detail


def plain_rule(r: dict) -> dict:
    """The writable fields of a rule as the API returns it (``getRule`` / ``get`` shape) in the plain form they are
    written in."""
    out = {}
    for k in FIELD_CHECKS:
        if k not in r:
            continue
        v = r[k]
        if isinstance(v, dict):
            v = ",".join(model.values(v))
        elif isinstance(v, list):
            v = ",".join(str(x) for x in v)
        out[k] = "" if v is None else str(v)
    return out


class OpnsenseWriter(ControllerAdapter):
    label = "OPNsense (local API)"
    kind = "controller"
    family = "opnsense"

    def __init__(self, url=None, write_api_key=None, write_api_secret=None, allow_self_signed=False, cert_sha256=None,
                 settle_s=None, timeout=15.0, state_dir=None, *, clock=time.time, sleep=time.sleep, **unknown):
        if unknown:
            raise ValueError("unknown option(s): %s (known: %s)" % (", ".join(sorted(unknown)), ", ".join(OPTIONS)))
        m = URL_RE.match(str(url or "").strip())
        if not m:
            raise ValueError("url: https://<address of the firewall>")
        port = int(m.group(3)) if m.group(3) else (443 if m.group(1).lower() == "https" else 80)
        if not 0 < port < 65536:
            raise ValueError("url: port")
        self._host, self._port = m.group(2).strip("[]"), port
        base = "%s://%s:%d" % (m.group(1).lower(), m.group(2), port)
        key = str(write_api_key.reveal() if isinstance(write_api_key, Secret) else write_api_key or "").strip()
        if not KEY_RE.match(key):
            raise ValueError("write_api_key missing or invalid")
        self._key = key
        self._secret = write_api_secret if isinstance(write_api_secret, Secret) else Secret(write_api_secret)
        if not KEY_RE.match(self._secret.reveal()):
            raise ValueError("write_api_secret missing or invalid")
        if allow_self_signed and not cert_sha256:
            log.warning("opnsense: allow_self_signed without cert_sha256 - the connection is not bound to the firewall's "
                        "certificate; set cert_sha256 (see docs/integrations/opnsense.md)")
        self._t = Transport(base, ALLOW, auth_headers=self._auth, allow_self_signed=bool(allow_self_signed),
                            timeout=float(timeout), pin_sha256=cert_sha256 or None)
        self.settle_s = SETTLE_S if settle_s is None else max(0, int(settle_s))
        self.clock, self.sleep = clock, sleep
        self._raw = {}
        self._state = None
        self._topo = None
        self._aliases = {}
        self._refs = {}                     # policy id -> rule uuid (rules of the rule API only)
        self._version = ""
        self._native = None                 # True/False after the version probe or the savepoint answer
        self._rev = None                    # savepoint revision of the running plan (device way back armed)
        self._armed = False                 # apply/<revision> sent and not confirmed yet: the firewall's timer runs
        self._staged = False                # a write went into the configuration and is not live yet
        self._touched = None                # policy ids a step of the running plan reached (None: unknown)
        self._wrote = {}                    # uuid -> rule fields this plan wrote
        self._created = {}                  # new: id of a step -> policy id of the created rule
        self._tried = set()                 # descriptions of rules this plan tried to create
        self._before_uuids = None           # uuids of the rule API before the plan (None: unknown)
        self._probe_fail = 0
        self._dead = None
        self.writes = []                    # (method, path without the uuid) of every write, for tests

    def __repr__(self):
        return "OpnsenseWriter(%s)" % self._host

    __str__ = __repr__

    def _auth(self) -> dict:
        token = base64.b64encode(("%s:%s" % (self._key, self._secret.reveal())).encode("utf-8")).decode("ascii")
        return {"Authorization": "Basic " + token}

    # ------------------------------------------------------------------ contract basics
    def capabilities(self) -> dict:
        caps = capabilities.validate(WRITE_CAPABILITIES, self.family)
        caps = capabilities.refine(caps, version=self._version or "")
        if self._native is False:
            caps["rollback"] = dict(caps["rollback"], device_native=False)
        return caps

    def endpoints(self) -> list:
        return [self._host]

    # ------------------------------------------------------------------ requests
    def _send(self, method: str, path: str, body=None):
        try:
            status, _, data = self._t.request(method, path, body)
        except Forbidden:
            raise WriteError("opnsense.refused_by_allow_list") from None
        except TransportError as exc:
            raise WriteError(exc.key) from None
        if status == 401:
            self._dead = "401"
            raise WriteError("opnsense.key_refused")
        if status == 403:
            raise WriteError("opnsense.forbidden")            # the key lacks the privilege for this page
        parsed = None
        if data:
            try:
                parsed = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                parsed = None
        return status, parsed

    def _get(self, path: str, optional: bool = False):
        status, data = self._send("GET", path)
        if optional and (status in (404, 405, 500, 501) or data is None):
            return None
        if not 200 <= status < 300 or data is None:
            raise WriteError("opnsense.read_failed", "status %d" % status)
        return data

    def _post(self, path: str, body=None):
        status, data = self._send("POST", path, {} if body is None else body)
        if not 200 <= status < 300:
            raise WriteError("opnsense.write_refused", "status %d" % status)
        if isinstance(data, dict) and str(data.get("result", "")).lower() == "failed":
            why = ",".join(sorted(str(k)[:32] for k in (data.get("validations") or {})))[:80]
            raise WriteError("opnsense.write_refused", why)
        self.writes.append(("POST", re.sub(_UUID, "<uuid>", re.sub(r"/" + _REV + r"\Z", "/<rev>", path))))
        return data

    # ------------------------------------------------------------------ reading
    def _read_raw(self) -> dict:
        raw = {}
        flt = self._get(API + "/get")
        if not isinstance(flt, dict) or not isinstance(flt.get("filter"), dict):
            raise WriteError("opnsense.no_rule_api")
        raw["filter"] = flt["filter"]
        info = self._get("/api/interfaces/overview/interfacesInfo", optional=True)
        raw["interfaces"] = info
        raw["groups"] = self._get("/api/firewall/group/searchItem", optional=True)
        al = self._get("/api/firewall/alias/get", optional=True)
        raw["aliases"] = al.get("alias") if isinstance(al, dict) else None
        if not self._version:
            sysinfo = self._get("/api/diagnostics/system/systemInformation", optional=True)
            fw = None if sysinfo else self._get("/api/core/firmware/status", optional=True)
            for v in (sysinfo or {}).get("versions") or [(fw or {}).get("product_version")]:
                if model.parse_version(v):
                    self._version = str(v)[:48]
                    break
            if self._native is None:
                self._native = model.native_rollback(self._version)
        raw["version"] = self._version
        return raw

    def read(self) -> dict:
        raw = self._read_raw()
        state = model.to_neutral(raw)
        state["controller"] = dict(state.get("controller") or {}, label=self.label, kind="firewall")
        state["side_effects"] = {}
        self._raw, self._state = raw, state
        self._topo = model.Topology(raw)
        self._aliases = model.aliases_of(raw)
        self._refs = {p["id"]: p["vendor_ref"] for p in state["policies"]
                      if model.UUID_RE.match(str(p.get("vendor_ref") or "")) and not p.get("builtin")}
        return state

    def health(self) -> tuple:
        if self._dead:
            return False, "key refused"
        try:
            self._read_raw()
        except WriteError as exc:
            return False, exc.key
        return True, ""

    # ------------------------------------------------------------------ lockout
    def _zone(self, state, zid):
        return next((z for z in state.get("zones") or [] if z.get("id") == zid), None)

    def lockout(self, state: dict, step: dict):
        """Family rules on top of the neutral ones: rules only; on one interface; targets only an interface's
        network (not a WAN interface), the firewall itself or any; no address lists, no groups, one port or one
        range; delete only own rules."""
        kind = step.get("kind")
        if kind not in ("policy.create", "policy.update", "policy.delete"):
            return "fw.block.capability"
        obj = step.get("object")
        old = next((p for p in state.get("policies") or [] if p.get("id") == obj), None) if kind != "policy.create" else {}
        if kind != "policy.create" and old is None:
            return "fw.block.unknown"
        if kind == "policy.delete":
            return None if old.get("own") else "fw.block.not_own"
        if kind == "policy.update" and (old.get("builtin") or old.get("editable") is False):
            return "fw.block.builtin"
        merged = dict(zonefw.POLICY_DEFAULTS, **dict(old or {}, **(step.get("after") or {})))
        if merged.get("proto") not in model.PROTOS_OUT:
            return "fw.block.capability"
        try:
            model.ports_out(merged.get("ports") or "")
        except ValueError:
            return "fw.block.capability"
        src, dst = merged.get("src") or {}, merged.get("dst") or {}
        for side in (src, dst):
            if set(side) - {"zone"}:
                return "fw.block.capability"
        zs = self._zone(state, src.get("zone"))
        if not zs or not zs.get("interface"):
            return "fw.block.capability"
        if dst.get("zone") != model.ANY:
            zd = self._zone(state, dst.get("zone"))
            if not zd or not (zd.get("interface") or zd.get("kind") == model.SELF) or zd.get("system") == "external":
                return "fw.block.capability"
        return None

    def _fw_ip(self):
        from ...connguard import first_address
        return first_address(self._host, self._port)

    def self_address(self):
        """The address this executor reaches the firewall from (see ``connguard.source_address``; nothing is sent)."""
        from ...connguard import source_address
        ip = self._fw_ip()
        return source_address(ip, self._port) if ip else None

    def plan_lockout(self, state: dict, steps: list):
        """The rule order and the way of this executor to the firewall stay right over the whole plan."""
        ip = self._fw_ip()
        me = self.self_address()
        why, _ = zonefw.plan_lockout_vars(state, steps, sources=[me] if me else [],
                                          targets=[(ip, self._port, "zf.path.firewall")] if ip else [])
        return why

    # ------------------------------------------------------------------ neutral fields <-> rule fields
    def _zone_key(self, zid: str) -> str:
        if zid == model.ANY:
            return "any"
        if zid == model.zone_id(model.SELF):
            return model.SELF
        for ident in (self._topo.ifaces if self._topo else {}):
            if model.zone_id(ident) == zid:
                return ident
        raise WriteError("opnsense.zone_unknown")

    def _net_of(self, key: str) -> str:
        if key == "any":
            return "any"
        if key == model.SELF:
            return "(self)"
        return key

    def _fields_out(self, fields: dict, *, own: bool, current: dict = None) -> dict:
        """Neutral rule fields -> rule API fields."""
        body = {}
        for f, v in fields.items():
            if f == "enabled":
                body["enabled"] = "1" if v else "0"
            elif f == "action":
                body["action"] = model.ACTIONS_OUT[v]
            elif f == "proto":
                body["protocol"] = model.PROTOS_OUT[v]
            elif f == "ports":
                try:
                    body["destination_port"] = model.ports_out(v or "")
                except ValueError:
                    raise WriteError("opnsense.ports") from None
            elif f == "name":
                text = ("%s %s" % (model.OWN_MARK, v)).strip() if own else v
                body["description"] = text[:255]
            elif f == "src":
                key = self._zone_key((v or {}).get("zone"))
                if key in ("any", model.SELF):
                    raise WriteError("opnsense.zone_unknown")
                body["interface"] = key
                ifc = (self._topo.ifaces if self._topo else {}).get(key) or {}
                body["source_net"] = key if ifc.get("cidr") else "any"
            elif f == "dst":
                body["destination_net"] = self._net_of(self._zone_key((v or {}).get("zone")))
        return body

    def _rule(self, uuid: str) -> dict:
        got = self._get(API + "/getRule/" + uuid, optional=True)
        r = got.get("rule") if isinstance(got, dict) else None
        return r if isinstance(r, dict) and r else None

    def _neutral(self, r: dict) -> dict:
        if self._topo is None:
            self.read()
        return model.rule_fields(r, self._topo, self._aliases)

    def _uuid(self, oid: str) -> str:
        oid = self._created.get(oid, oid)
        uuid = self._refs.get(oid)
        if not uuid:
            self.read()
            uuid = self._refs.get(oid)
        if not uuid:
            raise WriteError("opnsense.object_unknown")
        return uuid

    # ------------------------------------------------------------------ plan steps
    def apply(self, step: dict):
        kind = step["kind"]
        if self._touched is not None:
            self._touched.add(stepkinds.step_object(step))
        if kind == "policy.create":
            return self._create(step)
        if kind == "policy.update":
            return self._update(step)
        if kind == "policy.delete":
            return self._delete(step["object"], step["before"])
        raise WriteError("opnsense.kind_not_supported")

    def _next_sequence(self) -> int:
        rules = model._rule_map(self._raw.get("filter") or {})
        top = max([model._int(r.get("sequence"), 1) for r in rules.values()] or [0])
        return max(1, min(999999, top + 1))

    def _create(self, step: dict):
        after = step["after"]
        if self._topo is None:
            self.read()
        fields = dict(zonefw.POLICY_DEFAULTS, **after)
        body = self._fields_out({f: fields[f] for f in ("name", "src", "dst", "action", "proto", "ports", "enabled")}, own=True)
        body.update(quick="1", direction="in", ipprotocol="inet", source_not="0", destination_not="0",
                    sequence=str(self._next_sequence()))
        body.setdefault("destination_net", "any")
        self._tried.add(body["description"])
        got = self._post(API + "/addRule", {"rule": body})
        uuid = str((got or {}).get("uuid") or "")
        if not model.UUID_RE.match(uuid):
            raise WriteError("opnsense.not_applied")
        self._staged = True
        self._wrote[uuid] = set(body)
        r = self._rule(uuid)
        want = {f: fields[f] for f in ("name", "action", "proto", "ports", "enabled", "src", "dst")}
        if r is None or any(self._neutral(r).get(f) != v for f, v in want.items()):
            raise WriteError("opnsense.not_applied")
        self.read()
        pid = model.policy_id(uuid, after["src"]["zone"], after["dst"]["zone"])
        self._refs[pid] = uuid
        self._created[step["object"]] = pid
        if self._touched is not None:
            self._touched.add(pid)
        return {"id": pid}

    def _update(self, step: dict):
        uuid = self._uuid(step["object"])
        r = self._rule(uuid)
        if r is None:
            raise WriteError("opnsense.object_unknown")
        now = self._neutral(r)
        after = step["after"]
        if all(now.get(f) == v for f, v in after.items()):
            return None                                       # already as planned (idempotent)
        if any(now.get(f) != v for f, v in step["before"].items()):
            raise WriteError("opnsense.changed")              # changed on the firewall since the plan was made
        own = str(model.rule_parts(r)["description"]).startswith(model.OWN_MARK)
        body = self._fields_out(after, own=own)
        self._wrote.setdefault(uuid, set()).update(body)      # before the first byte: a write cut off half-way still goes back
        self._post(API + "/setRule/" + uuid, {"rule": body})
        self._staged = True
        got = self._rule(uuid)
        if got is None or any(self._neutral(got).get(f) != v for f, v in after.items()):
            raise WriteError("opnsense.not_applied")
        return None

    def _delete(self, oid: str, before, require_own: bool = True):
        uuid = self._refs.get(self._created.get(oid, oid))
        if not uuid:
            self.read()
            uuid = self._refs.get(self._created.get(oid, oid))
        if not uuid:
            return None                                       # already gone (idempotent)
        r = self._rule(uuid)
        if r is None:
            return None
        if require_own and not model.rule_parts(r)["description"].startswith(model.OWN_MARK):
            raise WriteError("opnsense.not_own")
        if before:
            now = self._neutral(r)
            if any(now.get(f) != v for f, v in before.items() if f in now):
                raise WriteError("opnsense.changed")
        self._post(API + "/delRule/" + uuid)
        self._staged = True
        if self._rule(uuid) is not None:
            raise WriteError("opnsense.not_applied")
        return None

    # ------------------------------------------------------------------ backup and the way back
    def snapshot(self, ids) -> dict:
        """The touched rules as stored now, read fresh; then the firewall's savepoint where the release has one."""
        out = {"rules": []}
        self._touched, self._wrote, self._created, self._tried = set(), {}, {}, set()
        self._rev, self._armed, self._staged = None, False, False
        self.read()
        self._before_uuids = set(model._rule_map(self._raw.get("filter") or {}))
        for oid in ids:
            if not isinstance(oid, str) or oid.startswith("new:"):
                continue
            uuid = self._refs.get(oid)
            if not uuid:
                raise WriteError("opnsense.object_unknown")
            r = self._rule(uuid)
            if r is None:
                raise WriteError("opnsense.object_unknown")
            out["rules"].append({"id": oid, "uuid": uuid, "rule": plain_rule(r)})
        if self._native is not False:
            self._take_savepoint()
        out["revision"] = self._rev
        out["device_native"] = bool(self._rev)
        return out

    def _take_savepoint(self) -> None:
        status, data = self._send("POST", API + "/savepoint", {})
        rev = str((data or {}).get("revision") or "") if isinstance(data, dict) else ""
        if 200 <= status < 300 and REV_RE.match(rev) and str(data.get("status", "ok")).lower() == "ok":
            self._rev, self._native = rev, True
            self.writes.append(("POST", API + "/savepoint"))
            return
        if self._native is True:
            raise WriteError("opnsense.savepoint_failed")     # this release has it: without it no change
        self._native = False                                  # a release without the firewall's own way back
        log.info("opnsense: this release has no savepoint; Lanloom's own backup is the way back")

    def _cancel_timer(self) -> None:
        if self._rev and self._armed:
            try:
                self._post(API + "/cancelRollback/" + self._rev)
            except WriteError:
                pass
        self._armed = False

    def restore(self, snapshot: dict) -> None:
        """Put every touched rule back as saved (rules the plan created are deleted, rules it deleted come back),
        then apply once. A running firewall timer is stopped first, so the two ways back never race."""
        snap = snapshot or {}
        reached = self._touched
        self._cancel_timer()
        if self._topo is None:
            try:
                self.read()
            except WriteError:
                pass

        def mine(item):
            return reached is None or item.get("id") in reached

        failed = []
        jobs = [("created", c) for c in snap.get("created") or []]
        jobs += [("rule", i) for i in snap.get("rules") or [] if isinstance(i, dict) and mine(i)]
        for job in jobs:
            try:
                self._restore_one(*job)
            except WriteError:
                failed.append(job)
        for job in failed:
            self._restore_one(*job)
        self._drop_strays()
        self._post(API + "/apply")
        self._staged = False
        self._rev = None

    def _drop_strays(self) -> None:
        """A create whose answer got lost (time-out after the firewall stored it) leaves an own rule nobody knows the
        id of: rules marked as own, not there before the plan and carrying a description this plan tried to create,
        are deleted as well."""
        if not self._tried or self._before_uuids is None:
            return
        self.read()
        for uuid, r in model._rule_map(self._raw.get("filter") or {}).items():
            desc = model.rule_parts(r)["description"]
            if uuid not in self._before_uuids and desc.startswith(model.OWN_MARK) and desc in self._tried:
                self._post(API + "/delRule/" + uuid)

    def _restore_one(self, what: str, item) -> None:
        if what == "created":
            self._delete(item, None)
            return
        uuid, saved = item.get("uuid"), item.get("rule") or {}
        if not isinstance(uuid, str) or not model.UUID_RE.match(uuid):
            raise WriteError("opnsense.object_unknown")
        r = self._rule(uuid)
        if r is None:
            if not str(saved.get("description") or "").startswith(model.OWN_MARK):
                raise WriteError("opnsense.object_unknown")   # only own rules can have been removed by a plan
            body = {k: saved[k] for k in CREATE_KEYS if k in saved}
            body.setdefault("quick", "1")
            body.setdefault("direction", "in")
            body.setdefault("ipprotocol", "inet")
            body.setdefault("source_not", "0")
            body.setdefault("destination_not", "0")
            self._post(API + "/addRule", {"rule": body})
            return
        now = plain_rule(r)
        # only the fields this plan wrote (a rule a step reached but did not write stays as it is now); after a crash
        # nothing is known and every writable field goes back
        keys = (self._wrote.get(uuid) or set()) & UPDATE_KEYS if self._touched is not None else UPDATE_KEYS
        diff = {k: saved[k] for k in keys if k in saved and now.get(k) != saved[k]}
        if diff:
            self._post(API + "/setRule/" + uuid, {"rule": diff})

    # ------------------------------------------------------------------ after the change
    def settle(self, steps: list) -> None:
        """Put the plan live with one apply. With the firewall's own way back: apply with the savepoint revision (the
        firewall's 60 second timer starts), read every changed rule back, and confirm only when all are as planned."""
        if not steps or not self._staged:
            return None
        if self._rev:
            self._post(API + "/apply/" + self._rev)
            self._armed = True
            started = self.clock()
            self.read()                                       # the firewall still answers: the path is open
            missing = self._missing(steps)
            if missing:
                raise WriteError("opnsense.not_applied")      # not confirmed: the restore stops the timer and writes back
            if self.clock() - started > 45:
                raise WriteError("opnsense.confirm_late")     # too close to the firewall's own revert; roll back cleanly
            self._post(API + "/cancelRollback/" + self._rev)
            self._armed = False
        else:
            self._post(API + "/apply")
        self._staged = False
        if self.settle_s:
            self.sleep(self.settle_s)
        return None

    def _missing(self, steps: list) -> list:
        missing = []
        pols = {p["id"]: p for p in (self._state or {}).get("policies") or []}
        for st in steps:
            kind = st["kind"]
            oid = self._created.get(st["object"], st["object"])
            p = pols.get(oid)
            if kind == "policy.delete":
                if p is not None:
                    missing.append(oid)
                continue
            if p is None:
                missing.append(oid)
                continue
            want = dict(zonefw.POLICY_DEFAULTS, **st["after"]) if kind == "policy.create" else st["after"]
            if any(p.get(f) != v for f, v in want.items() if f in ("name", "action", "proto", "ports", "enabled", "src", "dst")):
                missing.append(oid)
        return missing

    def probe(self, steps: list) -> dict:
        """Watch: every touched rule still as planned. Two failed reads of the firewall in a row count as a global
        failure."""
        try:
            self.read()
        except WriteError:
            self._probe_fail += 1
            return {"global_ok": self._probe_fail < 2, "missing": []}
        self._probe_fail = 0
        return {"global_ok": True, "missing": self._missing(steps)}
