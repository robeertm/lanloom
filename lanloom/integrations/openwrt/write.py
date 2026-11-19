"""Write half of the OpenWrt family (stage B: firewall rules). Loaded only by the executor service, never by the app.

What it can change - nothing else, whatever a plan says:

* ``policy.create``   add a firewall rule from one zone to another zone, to the router itself or to any zone, as a
                      named section ``lanloom_<12 hex>`` whose name starts with ``[lanloom]`` (appended after the
                      existing rules)
* ``policy.update``   change name, action, protocol, destination port, source/destination zone, or switch a rule on
                      and off (only simple rules: one source zone, no addresses, marks, schedules or limits; a rule
                      that is not Lanloom's own keeps its name, because UCI refers to an unnamed section by its name
                      option)
* ``policy.delete``   remove a rule Lanloom created (only those)

Never: interfaces, zones, forwardings, redirects, wireless, address service, users, system settings. Those are no
plan step of this family.

The write half signs in with a login of its own (separate from the read-only one of the app) whose access list allows
``uci`` changes of the firewall configuration only (see docs/integrations/openwrt.md). Options file (mode 0600, see
``credentials.load_write_options``)::

    {"url": "https://192.168.1.1", "write_username": "lanloom-write", "write_password": "...",
     "cert_sha256": "<SHA-256 of the router's certificate>"}        # or a certificate the system trusts

Safety below the adapter: every request goes through ``transport.Transport`` with a field-exact allow-list of
JSON-RPC calls: ``uci add`` (config ``firewall``, type ``rule``, a ``lanloom_`` section name, a complete rule of the
fields below whose name carries the mark), ``uci set`` (some of those fields on one section), ``uci delete`` (a whole
section only when it is named ``lanloom_...``; otherwise only the options ``dest``, ``dest_port``, ``enabled``,
``proto`` and ``target``, which equals writing their default), ``uci
changes``/``revert`` of the firewall configuration, ``uci apply`` (with ``rollback`` and a timeout of 90 to 600
seconds, or without), ``uci confirm`` and ``uci rollback``; reading ``uci get`` of the firewall and network
configuration, ``system board`` and ``network.interface dump``. Every value is checked (one zone name, ``*`` as a
destination, a port or one range, ACCEPT/REJECT/DROP, ...).

The way back has two layers:

1. Lanloom's own: before the first change every touched rule is read and kept; changes are staged in the session
   first (nothing is live), so trouble before the apply only discards them (``uci revert``); trouble after it writes
   the kept rules back (rules the plan created are deleted) and applies.
2. The router's own: ``settle`` puts the plan live with ``uci apply`` + ``rollback`` + a 90 second timeout. The router
   keeps a copy of the configuration from before and puts it back by itself unless ``uci confirm`` comes in time -
   which the half sends only after it has read every changed rule back as planned and the router still answers (rpcd
   takes the confirmation from any session allowed to send it, so the write login belongs to the executor alone). If the executor dies or loses its way to the router in between (a rule that cut it off), the router
   reverts on its own. When the half itself finds trouble before confirming, it sends ``uci rollback`` (immediate).

Lockout rules on top of the neutral ones (``firewall/zonefw.py``: no blocking rule on the gateway zone, the path from
the executor to the router stays open): a rule's source is one real zone; its destination is a real zone, the router
itself or any; one port or one range. Product names are used only to identify compatible products; no affiliation or
endorsement.
"""
import json
import logging
import re
import secrets as _secrets
import time

from . import WRITE_CAPABILITIES, model
from .. import capabilities
from ..credentials import Secret
from ..transport import AllowList, Forbidden, Rule, Transport, TransportError
from ...firewall import steps as stepkinds, zonefw
from ...firewall.adapter import ControllerAdapter

log = logging.getLogger("lanloom.executor")

URL_RE = re.compile(r"\A(https?)://([A-Za-z0-9.-]{1,253}|\[[0-9A-Fa-f:.]{2,45}\])(?::(\d{1,5}))?/?\Z", re.I)
USER_RE = re.compile(r"\A[A-Za-z0-9_.@-]{1,64}\Z")
OPTIONS = ("url", "write_username", "write_password", "allow_self_signed", "cert_sha256", "settle_s", "timeout",
           "apply_timeout")
PATH = "/ubus"
SETTLE_S = 3
SESSION_S = 900
LOGINS_PER_HOUR = 4
CONFIRM_BY = 0.6                 # confirm within this share of the router's timeout, or roll back cleanly

# ---------------------------------------------------------------------------- the rule options the half may write
_TEXT_BAD = re.compile(r"[\x00-\x1f\x7f]")


def _zone(v) -> bool:
    return isinstance(v, str) and v != model.ANY and bool(model.ZONE_RE.match(v))


def _port(v) -> bool:
    if not isinstance(v, str):
        return False
    try:
        return model.ports_out(v) == v and v != ""
    except ValueError:
        return False


def _name(v) -> bool:
    return isinstance(v, str) and 0 < len(v) <= 120 and not _TEXT_BAD.search(v)


FIELD_CHECKS = {
    "name": _name,
    "src": _zone,
    "dest": lambda v: v == model.ANY or _zone(v),
    "proto": lambda v: v in model.PROTOS_OUT.values(),
    "dest_port": _port,
    "target": lambda v: v in model.TARGETS,
    "enabled": lambda v: v in ("0", "1"),
}
CREATE_NEEDS = frozenset({"name", "src", "proto", "target", "enabled"})
DELETABLE_OPTIONS = ("dest", "dest_port", "enabled", "proto", "target")   # no option = the router itself, every port, on, tcp+udp, DROP


def _values(create: bool):
    def check(vals) -> bool:
        if not isinstance(vals, dict) or not vals or not set(vals) <= set(FIELD_CHECKS):
            return False
        if create and (not CREATE_NEEDS <= set(vals) or not str(vals.get("name")).startswith(model.OWN_MARK)):
            return False
        return all(FIELD_CHECKS[k](v) is True for k, v in vals.items())
    return check


_FW = lambda v: v == "firewall"                                                   # noqa: E731
_SECTION = lambda v: isinstance(v, str) and bool(model.SECTION_RE.match(v))       # noqa: E731
_OWN_SECTION = lambda v: isinstance(v, str) and bool(model.OWN_SECTION_RE.match(v))   # noqa: E731
WRITE_CALLS = {
    ("session", "login"): model.LOGIN_ARGS,
    ("system", "board"): {},
    ("network.interface", "dump"): {},
    ("uci", "get"): {"config": lambda v: v in ("firewall", "network")},
    ("uci", "add"): {"config": _FW, "type": lambda v: v == "rule", "name": _OWN_SECTION, "values": _values(True)},
    ("uci", "set"): {"config": _FW, "section": _SECTION, "values": _values(False)},
    ("uci", "delete"): [{"config": _FW, "section": _OWN_SECTION},
                        {"config": _FW, "section": _SECTION, "option": lambda v: v in DELETABLE_OPTIONS}],
    ("uci", "changes"): {"config": _FW},
    ("uci", "revert"): {"config": _FW},
    ("uci", "apply"): [{"rollback": lambda v: v is True,
                        "timeout": lambda v: isinstance(v, int) and not isinstance(v, bool) and 90 <= v <= 600},
                       {"rollback": lambda v: v is False}],
    ("uci", "confirm"): {},
    ("uci", "rollback"): {},
}
_CHECKS = dict(model.ENVELOPE, params=model.call_check(WRITE_CALLS))
ALLOW = AllowList([Rule("POST", re.escape(PATH), fields=tuple(_CHECKS), values=_CHECKS, exact=True)], write=True)


class WriteError(RuntimeError):
    """A write that cannot or did not happen. ``key`` is a short reason (never a secret, never an address)."""

    def __init__(self, key: str, detail: str = ""):
        super().__init__(key)
        self.key, self.detail = key, detail


def plain_rule(s: dict) -> dict:
    """The writable options of a rule section as they are stored (missing options stay missing)."""
    out = {}
    for k in FIELD_CHECKS:
        v = s.get(k)
        if v is None or v == "" or v == []:
            continue
        out[k] = " ".join(model.items(v)) if isinstance(v, list) else str(v)
    return out


class OpenwrtWriter(ControllerAdapter):
    label = "OpenWrt (local API)"
    kind = "controller"
    family = "openwrt"

    def __init__(self, url=None, write_username=None, write_password=None, allow_self_signed=False, cert_sha256=None,
                 settle_s=None, timeout=15.0, apply_timeout=None, state_dir=None, *, clock=time.time, sleep=time.sleep,
                 **unknown):
        if unknown:
            raise ValueError("unknown option(s): %s (known: %s)" % (", ".join(sorted(unknown)), ", ".join(OPTIONS)))
        m = URL_RE.match(str(url or "").strip())
        if not m:
            raise ValueError("url: https://<address of the router>")
        port = int(m.group(3)) if m.group(3) else (443 if m.group(1).lower() == "https" else 80)
        if not 0 < port < 65536:
            raise ValueError("url: port")
        self._host, self._port = m.group(2).strip("[]"), port
        base = "%s://%s:%d" % (m.group(1).lower(), m.group(2), port)
        user = str(write_username.reveal() if isinstance(write_username, Secret) else write_username or "").strip()
        if not USER_RE.match(user):
            raise ValueError("write_username missing or invalid")
        self._user = user
        self._password = write_password if isinstance(write_password, Secret) else Secret(write_password)
        if not self._password or not 0 < len(self._password.reveal()) <= 256:
            raise ValueError("write_password missing or invalid")
        if m.group(1).lower() == "http":
            log.warning("openwrt: plain http - the password travels unencrypted on the network; use https")
        if allow_self_signed and not cert_sha256:
            log.warning("openwrt: allow_self_signed without cert_sha256 - the connection is not bound to the router's "
                        "certificate; set cert_sha256 (see docs/integrations/openwrt.md)")
        self._t = Transport(base, ALLOW, allow_self_signed=bool(allow_self_signed), timeout=float(timeout),
                            pin_sha256=cert_sha256 or None)
        self.settle_s = SETTLE_S if settle_s is None else max(0, int(settle_s))
        self.apply_timeout = model.APPLY_TIMEOUT if apply_timeout is None else min(600, max(90, int(apply_timeout)))
        self.clock, self.sleep = clock, sleep
        self._sid = None                    # session id (a bearer secret: never logged, never in repr)
        self._logins = []
        self._last_ok = float("-inf")
        self._n = 0
        self._raw = {}
        self._state = None
        self._topo = None
        self._refs = {}                     # policy id -> stable rule reference (rules only)
        self._sections = {}                 # stable reference -> section name in this session's view
        self._rules = {}                    # stable reference -> section dict
        self._version = ""
        self._native = None                 # True/False after the version probe or the first apply
        self._armed = False                 # apply with rollback sent and not confirmed yet: the router's timer runs
        self._staged = False                # a change sits in the session and is not live yet
        self._touched = None                # policy ids a step of the running plan reached (None: unknown)
        self._wrote = {}                    # reference -> option names this plan wrote
        self._created = {}                  # new: id of a step -> policy id of the created rule
        self._made = set()                  # section names this plan created
        self._probe_fail = 0
        self._dead = None
        self.writes = []                    # (object, method) of every change, for tests

    def __repr__(self):
        return "OpenwrtWriter(%s)" % self._host

    __str__ = __repr__

    # ------------------------------------------------------------------ contract basics
    def capabilities(self) -> dict:
        caps = capabilities.validate(WRITE_CAPABILITIES, self.family)
        caps = capabilities.refine(caps, version=self._version or "")
        if self._native is False:
            caps["rollback"] = dict(caps["rollback"], device_native=False)
        return caps

    def endpoints(self) -> list:
        return [self._host]

    # ------------------------------------------------------------------ calls
    def _post(self, sid, obj, method, args):
        self._n = self._n % 999_999_999 + 1
        try:
            status, _, data = self._t.request("POST", PATH, model.call_body(sid, obj, method, args, self._n))
        except Forbidden:
            raise WriteError("openwrt.refused_by_allow_list") from None
        except TransportError as exc:
            raise WriteError(exc.key) from None
        if status in (401, 403):
            return 6, None
        if not 200 <= status < 300:
            raise WriteError("openwrt.protocol", "status %d" % status)
        try:
            body = json.loads(data.decode("utf-8")) if data else None
        except (ValueError, UnicodeDecodeError):
            raise WriteError("openwrt.protocol") from None
        return model.reply(body)

    def _login(self) -> None:
        t = self.clock()
        self._logins = [x for x in self._logins if t - x < 3600]
        if len(self._logins) >= LOGINS_PER_HOUR:
            raise WriteError("openwrt.login_limit")
        self._logins.append(t)
        code, data = self._post(model.ZERO_SID, "session", "login",
                                {"username": self._user, "password": self._password.reveal(), "timeout": SESSION_S})
        sid = data.get("ubus_rpc_session") if isinstance(data, dict) else None
        if code != 0 or not isinstance(sid, str) or not model.SID_RE.match(sid):
            self._dead = "login"
            raise WriteError("openwrt.login_refused")
        self._sid = sid
        self._last_ok = t

    def _call(self, obj, method, args=None, *, retry=True):
        if self._sid is None:
            self._login()
        code, data = self._post(self._sid, obj, method, dict(args or {}))
        if code == -32002 and retry and not (self._staged or self._armed) and self.clock() - self._last_ok > SESSION_S - 60:
            # the session expired: sign in once more - never while changes sit in it or the router's timer runs
            # (staged changes and the confirmation belong to that one session); within the session's lifetime
            # "access denied" means the access list leaves the call out
            self._sid = None
            self._login()
            code, data = self._post(self._sid, obj, method, dict(args or {}))
        if code == -32002:
            raise WriteError("openwrt.session_lost" if self._staged or self._armed else "openwrt.forbidden")
        self._last_ok = self.clock()
        return code, data

    def _change(self, method, args):
        code, data = self._call("uci", method, args)
        if code != 0:
            raise WriteError("openwrt.write_refused", "uci %s: %d" % (method, code))
        self.writes.append(("uci", method))
        return data

    # ------------------------------------------------------------------ reading
    def _uci(self, config, optional=False):
        code, data = self._call("uci", "get", {"config": config})
        vals = data.get("values") if isinstance(data, dict) else None
        if code != 0 or not isinstance(vals, dict):
            if optional:
                return None
            raise WriteError("openwrt.read_failed", "uci get %s: %d" % (config, code))
        return model.scrub(vals)

    def _read_raw(self) -> dict:
        raw = {"firewall": self._uci("firewall"), "network": self._uci("network", optional=True)}
        code, dump = self._call("network.interface", "dump")
        raw["ifaces"] = dump if code == 0 else None
        if not self._version:
            code, board = self._call("system", "board")
            rel = (board or {}).get("release") if code == 0 and isinstance(board, dict) else None
            self._version = str((rel or {}).get("version") or "")[:48]
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
        self._sections, self._rules = {}, {}
        for name, ref, s in model.rule_refs(raw):
            self._sections[ref] = name
            self._rules[ref] = s
        self._refs = {p["id"]: p["vendor_ref"] for p in state["policies"] if not p.get("builtin")}
        return state

    def health(self) -> tuple:
        if self._dead:
            return False, "login refused"
        try:
            self._read_raw()
        except WriteError as exc:
            return False, exc.key
        return True, ""

    # ------------------------------------------------------------------ lockout
    def _zone_obj(self, state, zid):
        return next((z for z in state.get("zones") or [] if z.get("id") == zid), None)

    def lockout(self, state: dict, step: dict):
        """Family rules on top of the neutral ones: rules only; the source is one real zone; the destination is a real
        zone, the router itself or any; one port or one range; a foreign rule keeps its name; delete only own rules."""
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
        after = step.get("after") or {}
        if kind == "policy.update" and "name" in after and not old.get("own") and str(old.get("vendor_ref", "")).startswith("a:"):
            return "fw.block.capability"            # the name is how an unnamed section is found again
        merged = dict(zonefw.POLICY_DEFAULTS, **dict(old or {}, **after))
        if merged.get("proto") not in model.PROTOS_OUT or merged.get("action") not in model.TARGETS_OUT:
            return "fw.block.capability"
        try:
            model.ports_out(merged.get("ports") or "")
        except ValueError:
            return "fw.block.capability"
        if merged.get("ports") and merged.get("proto") not in ("tcp", "udp", "tcp_udp"):
            return "fw.block.capability"
        src, dst = merged.get("src") or {}, merged.get("dst") or {}
        for side in (src, dst):
            if set(side) - {"zone"}:
                return "fw.block.capability"
        zs = self._zone_obj(state, src.get("zone"))
        if not zs or not zs.get("interface"):
            return "fw.block.capability"
        if dst.get("zone") != model.ANY:
            zd = self._zone_obj(state, dst.get("zone"))
            if not zd or not (zd.get("interface") or zd.get("kind") == model.SELF):
                return "fw.block.capability"
        return None

    def _router_ip(self):
        from ...connguard import first_address
        return first_address(self._host, self._port)

    def self_address(self):
        """The address this executor reaches the router from (see ``connguard.source_address``; nothing is sent)."""
        from ...connguard import source_address
        ip = self._router_ip()
        return source_address(ip, self._port) if ip else None

    def plan_lockout(self, state: dict, steps: list):
        """The way of this executor to the router stays open over the whole plan."""
        ip = self._router_ip()
        me = self.self_address()
        why, _ = zonefw.plan_lockout_vars(state, steps, sources=[me] if me else [],
                                          targets=[(ip, self._port, "zf.path.router")] if ip else [])
        return why

    # ------------------------------------------------------------------ neutral fields <-> rule options
    def _zone_name(self, zid: str) -> str:
        if zid == model.ANY:
            return model.ANY
        if zid == model.zone_id(model.SELF):
            return model.SELF
        for name in (self._topo.zones if self._topo else {}):
            if model.zone_id(name) == zid:
                return name
        raise WriteError("openwrt.zone_unknown")

    def _options_out(self, fields: dict, *, own: bool) -> tuple:
        """Neutral rule fields -> (options to set, options to delete)."""
        vals, drop = {}, []
        for f, v in fields.items():
            if f == "enabled":
                vals["enabled"] = "1" if v else "0"
            elif f == "action":
                vals["target"] = model.TARGETS_OUT[v]
            elif f == "proto":
                vals["proto"] = model.PROTOS_OUT[v]
            elif f == "ports":
                try:
                    p = model.ports_out(v or "")
                except ValueError:
                    raise WriteError("openwrt.ports") from None
                if p:
                    vals["dest_port"] = p
                else:
                    drop.append("dest_port")
            elif f == "name":
                vals["name"] = (("%s %s" % (model.OWN_MARK, v)).strip() if own else str(v))[:120]
            elif f == "src":
                name = self._zone_name((v or {}).get("zone"))
                if name in (model.ANY, model.SELF):
                    raise WriteError("openwrt.zone_unknown")
                vals["src"] = name
            elif f == "dst":
                name = self._zone_name((v or {}).get("zone"))
                if name == model.SELF:
                    drop.append("dest")
                else:
                    vals["dest"] = name
        return vals, drop

    def _neutral(self, s: dict) -> dict:
        if self._topo is None:
            self.read()
        return model.rule_fields(s, self._topo)

    def _ref(self, oid: str) -> str:
        oid = self._created.get(oid, oid)
        ref = self._refs.get(oid)
        if not ref:
            self.read()
            ref = self._refs.get(oid)
        if not ref:
            raise WriteError("openwrt.object_unknown")
        return ref

    def _section_of(self, ref: str, fresh: bool = True):
        if fresh:
            self.read()
        return self._sections.get(ref), self._rules.get(ref)

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
        raise WriteError("openwrt.kind_not_supported")

    def _create(self, step: dict):
        after = step["after"]
        if self._topo is None:
            self.read()
        fields = dict(zonefw.POLICY_DEFAULTS, **after)
        vals, _ = self._options_out({f: fields[f] for f in ("name", "src", "dst", "action", "proto", "ports", "enabled")},
                                    own=True)
        section = "lanloom_" + _secrets.token_hex(6)
        self._made.add(section)
        self._staged = True                                   # before the first byte: a cut-off answer still goes back
        got = self._change("add", {"config": "firewall", "type": "rule", "name": section, "values": vals})
        if isinstance(got, dict) and got.get("section") not in (None, section):
            raise WriteError("openwrt.not_applied")
        ref = "s:" + section
        name, s = self._section_of(ref)
        want = {f: fields[f] for f in ("name", "action", "proto", "ports", "enabled", "src", "dst")}
        if s is None or any(self._neutral(s).get(f) != v for f, v in want.items()):
            raise WriteError("openwrt.not_applied")
        pid = model.policy_id(ref)
        self._refs[pid] = ref
        self._created[step["object"]] = pid
        self._wrote[ref] = set(vals)
        if self._touched is not None:
            self._touched.add(pid)
        return {"id": pid}

    def _update(self, step: dict):
        ref = self._ref(step["object"])
        section, s = self._section_of(ref)
        if s is None:
            raise WriteError("openwrt.object_unknown")
        now = self._neutral(s)
        after = step["after"]
        if all(now.get(f) == v for f, v in after.items()):
            return None                                       # already as planned (idempotent)
        if any(now.get(f) != v for f, v in step["before"].items()):
            raise WriteError("openwrt.changed")               # changed on the router since the plan was made
        own = ref.startswith("s:") and bool(model.OWN_SECTION_RE.match(ref[2:]))
        if "name" in after and not own and ref.startswith("a:"):
            raise WriteError("openwrt.rename_refused")
        vals, drop = self._options_out(after, own=own)
        self._wrote.setdefault(ref, set()).update(vals, drop)  # before the first byte: a cut-off write still goes back
        self._staged = True
        if vals:
            self._change("set", {"config": "firewall", "section": section, "values": vals})
        for opt in drop:
            if s.get(opt) not in (None, "", []):
                self._change("delete", {"config": "firewall", "section": section, "option": opt})
        _, got = self._section_of(ref)
        if got is None or any(self._neutral(got).get(f) != v for f, v in after.items()):
            raise WriteError("openwrt.not_applied")
        return None

    def _delete(self, oid: str, before, require_own: bool = True):
        try:
            ref = self._ref(oid)
        except WriteError:
            return None                                       # already gone (idempotent)
        section, s = self._section_of(ref)
        if s is None:
            return None
        if require_own and not (model.OWN_SECTION_RE.match(section or "") and model.rule_parts(s)["name"].startswith(model.OWN_MARK)):
            raise WriteError("openwrt.not_own")
        if before:
            now = self._neutral(s)
            if any(now.get(f) != v for f, v in before.items() if f in now):
                raise WriteError("openwrt.changed")
        self._staged = True
        self._change("delete", {"config": "firewall", "section": section})
        if self._section_of(ref)[1] is not None:
            raise WriteError("openwrt.not_applied")
        return None

    # ------------------------------------------------------------------ backup and the way back
    def snapshot(self, ids) -> dict:
        """The touched rules as stored now, read fresh (the router's own copy is taken by ``uci apply``)."""
        out = {"rules": []}
        self._touched, self._wrote, self._created, self._made = set(), {}, {}, set()
        self._armed, self._staged = False, False
        self.read()
        code, ch = self._call("uci", "changes", {"config": "firewall"})
        if code == 0 and isinstance(ch, dict) and ch.get("changes"):
            self._change("revert", {"config": "firewall"})    # left-overs of an earlier session never go live with us
            self.read()
        for oid in ids:
            if not isinstance(oid, str) or oid.startswith("new:"):
                continue
            ref = self._refs.get(oid)
            if not ref or ref not in self._rules:
                raise WriteError("openwrt.object_unknown")
            out["rules"].append({"id": oid, "ref": ref, "rule": plain_rule(self._rules[ref])})
        out["device_native"] = self._native is not False
        return out

    def restore(self, snapshot: dict) -> None:
        """Put every touched rule back as saved. Changes still in the session are discarded (they never went live);
        a running router timer is ended with ``uci rollback`` (the router puts its copy back at once); anything that
        is live and differs from the backup is written back and applied (rules the plan created are deleted, rules it
        deleted come back)."""
        snap = snapshot or {}
        if self._armed:
            try:
                self._change("rollback", {})
            except WriteError:
                pass                                          # the router's own timer still runs out and reverts
            self._armed = False
            self._staged = False
        if self._staged:
            try:
                self._change("revert", {"config": "firewall"})
            except WriteError:
                pass
            self._staged = False
        try:
            self.read()
        except WriteError:
            if self._sid is not None:
                raise
            self._login()
            self.read()
        reached = self._touched

        def mine(item):
            return reached is None or item.get("id") in reached

        changed = False
        for c in snap.get("created") or []:
            changed |= self._drop_created(c)
        for section in sorted(self._made):
            if ("s:" + section) in self._rules:
                self._change("delete", {"config": "firewall", "section": section})
                changed = True
        for item in snap.get("rules") or []:
            if isinstance(item, dict) and mine(item):
                changed |= self._restore_one(item)
        if changed:
            self._change("apply", {"rollback": False})
        self._staged = False

    def _drop_created(self, oid) -> bool:
        ref = self._refs.get(self._created.get(oid, oid))
        if not ref or ref not in self._rules or not ref.startswith("s:") or not model.OWN_SECTION_RE.match(ref[2:]):
            return False
        self._change("delete", {"config": "firewall", "section": ref[2:]})
        self.read()
        return True

    def _restore_one(self, item) -> bool:
        ref, saved = item.get("ref"), item.get("rule") or {}
        if not isinstance(ref, str) or not ref[:2] in ("s:", "a:"):
            raise WriteError("openwrt.object_unknown")
        section, s = self._sections.get(ref), self._rules.get(ref)
        if s is None:
            if not (ref.startswith("s:") and model.OWN_SECTION_RE.match(ref[2:])
                    and str(saved.get("name") or "").startswith(model.OWN_MARK)):
                raise WriteError("openwrt.object_unknown")    # only own rules can have been removed by a plan
            vals = {k: v for k, v in saved.items() if k in FIELD_CHECKS}
            self._change("add", {"config": "firewall", "type": "rule", "name": ref[2:], "values": vals})
            self.read()
            return True
        now = plain_rule(s)
        # only the options this plan wrote (a rule a step reached but did not write stays as it is now); after a crash
        # nothing is known and every writable option goes back
        keys = (self._wrote.get(ref) or set()) if self._touched is not None else set(FIELD_CHECKS)
        if "name" in keys and ref.startswith("a:"):
            keys = keys - {"name"}
        vals = {k: saved[k] for k in keys if k in saved and now.get(k) != saved[k]}
        drop = [k for k in keys if k in DELETABLE_OPTIONS and k not in saved and k in now]
        if vals:
            self._change("set", {"config": "firewall", "section": section, "values": vals})
        for k in drop:
            self._change("delete", {"config": "firewall", "section": section, "option": k})
        return bool(vals or drop)

    # ------------------------------------------------------------------ after the change
    def settle(self, steps: list) -> None:
        """Put the plan live: ``uci apply`` with the router's own way back (rollback + timeout), read every changed rule
        back, and confirm only when all are as planned and the router still answers - early enough before its timer."""
        if not steps or not self._staged:
            return None
        if self._native is False:
            self._change("apply", {"rollback": False})
            self._staged = False
        else:
            code, _ = self._call("uci", "apply", {"rollback": True, "timeout": self.apply_timeout})
            if code != 0:
                raise WriteError("openwrt.apply_refused", "code %d" % code)   # e.g. another apply still waits
            self.writes.append(("uci", "apply"))
            self._native = True
            self._armed = True
            self._staged = False
            started = self.clock()
            if self.settle_s:
                self.sleep(self.settle_s)                     # the firewall reloads
            self.read()                                       # the router still answers: the path is open
            if self._missing(steps):
                raise WriteError("openwrt.not_applied")       # not confirmed: restore rolls back at once
            if self.clock() - started > self.apply_timeout * CONFIRM_BY:
                raise WriteError("openwrt.confirm_late")      # too close to the router's own revert; roll back cleanly
            self._change("confirm", {})
            self._armed = False
            return None
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
        """Watch: every touched rule still as planned. Two failed reads of the router in a row count as a global
        failure."""
        try:
            self.read()
        except WriteError:
            self._probe_fail += 1
            return {"global_ok": self._probe_fail < 2, "missing": []}
        self._probe_fail = 0
        return {"global_ok": True, "missing": self._missing(steps)}
