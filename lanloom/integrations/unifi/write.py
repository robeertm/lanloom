"""Write half of the UniFi family (stage B: clients). Loaded only by the executor service, never by the web app.

What it can change - nothing else, whatever a plan says:

* ``move``                move a client to another network: network override plus a fixed address in that network
* ``client.set.network``  the same, as part of a client step that also renames or readdresses the client
* ``client.set.fixed_ip`` give a client a fixed address in its network, change it, or remove it ("")
* ``client.set.name``     the client's name

Client settings exist only in the classic API (the official integration API has no client writes), so the write half
signs in with a local account of its own - a site administrator without system rights, separate from the read-only
account of the app. Its options file (mode 0600, see ``credentials.load_write_options``)::

    {"url": "https://192.168.1.1", "site": "default", "write_username": "...", "write_password": "...",
     "cert_sha256": "<SHA-256 of the console's certificate>"}           # or a certificate the system trusts

Safety below the adapter: every request goes through ``transport.Transport`` with a field-exact allow-list (GET on
the areas read below, sign-in/out, and ``PUT rest/user/<id>`` with only the six client fields, each value checked).
Before anything is written the half checks what the controller itself does not: that the network exists and is a
local network, that the address lies inside it, is not its gateway and is not used by another client. After every
write it reads the client back and stops (the executor rolls back) when the values did not take.

Sessions: one sign-in per need, one new sign-in after HTTP 401 and never after 403, a planned new sign-in before the
controller's own session limit, and at most four sign-ins an hour across processes with a 15-minute pause after a
refused session (``session.FileLedger``). Product names are used only to identify compatible products; no
affiliation or endorsement.
"""
import ipaddress
import json
import logging
import re
import time
from pathlib import Path

from . import WRITE_CAPABILITIES, model, session
from .. import capabilities
from ..credentials import Secret, vet_write_account
from ..transport import Forbidden, Rule, Transport, TransportError, write_allowlist
from ...firewall import steps as stepkinds
from ...firewall.adapter import ControllerAdapter

log = logging.getLogger("lanloom.executor")

URL_RE = re.compile(r"\A(https?)://([A-Za-z0-9.-]{1,253}|\[[0-9A-Fa-f:.]{2,45}\])(?::(\d{1,5}))?/?\Z", re.I)
SITE_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
OID = r"[0-9a-f]{24}"
_S = r"[A-Za-z0-9_-]{1,64}"
_P = r"(?:/proxy/network)?"
_OID_RE = re.compile(r"\A%s\Z" % OID)
MSG_RE = re.compile(r"\A[A-Za-z0-9_.]{1,60}\Z")
SETTLE_WIRELESS_S = 90           # a wireless client reconnects after its network changed
SETTLE_ADDRESS_S = 5
LOCAL_PURPOSES = ("corporate", "guest", "vlan-only", "")
OPTIONS = ("url", "site", "write_username", "write_password", "allow_self_signed", "cert_sha256", "settle_s", "timeout",
           "state_dir")


# ---------------------------------------------------------------------------- the allow-list (below the adapter)
def _name_ok(v):
    return isinstance(v, str) and len(v) <= 128 and not any(ord(c) < 32 or ord(c) == 127 for c in v)


def _ip_or_empty(v):
    if v == "":
        return True
    ipaddress.ip_address(v if isinstance(v, str) else "")
    return True


def _oid_or_empty(v):
    return isinstance(v, str) and (v == "" or bool(_OID_RE.match(v)))


def _bool(v):
    return isinstance(v, bool)


USER_VALUES = {"name": _name_ok, "use_fixedip": _bool, "fixed_ip": _ip_or_empty, "network_id": _oid_or_empty,
               "virtual_network_override_enabled": _bool, "virtual_network_override_id": _oid_or_empty}
READ_PATHS = (
    _P + r"/api/s/%s/(?:self|stat/(?:sta|device|sysinfo)|rest/(?:networkconf|user))" % _S,
    _P + r"/api/s/%s/rest/user/%s" % (_S, OID),
    _P + r"/v2/api/site/%s/(?:firewall/zone|firewall-policies)" % _S,
)
ALLOW = write_allowlist(
    [Rule("GET", p) for p in READ_PATHS]
    + [Rule("POST", r"/api/(?:auth/)?login", fields=("username", "password"), login=True),
       Rule("POST", r"/api/(?:auth/)?logout", fields=()),
       Rule("PUT", _P + r"/api/s/%s/rest/user/%s" % (_S, OID), fields=tuple(USER_VALUES), values=USER_VALUES, nonempty=True)])
LOGIN = {"console": "/api/auth/login", "classic": "/api/login"}
LOGOUT = {"console": "/api/auth/logout", "classic": "/api/logout"}
PREFIX = {"console": "/proxy/network", "classic": ""}
AREAS = {"sysinfo": "api/s/{site}/stat/sysinfo", "networkconf": "api/s/{site}/rest/networkconf",
         "user": "api/s/{site}/rest/user", "sta": "api/s/{site}/stat/sta", "device": "api/s/{site}/stat/device",
         "zone": "v2/api/site/{site}/firewall/zone", "policies": "v2/api/site/{site}/firewall-policies"}


class WriteError(RuntimeError):
    """A write that cannot or did not happen. ``key`` is a short reason (never a secret, never an address)."""

    def __init__(self, key: str, detail: str = ""):
        super().__init__(key)
        self.key, self.detail = key, detail


def _msg(body) -> str:
    meta = body.get("meta") if isinstance(body, dict) else None
    text = str((meta or {}).get("msg") or "")
    return text if MSG_RE.match(text) else ""


class UnifiWriter(ControllerAdapter):
    label = "UniFi Network (local API)"
    kind = "controller"
    family = "unifi"

    def __init__(self, url=None, site="default", write_username=None, write_password=None, allow_self_signed=False,
                 cert_sha256=None, settle_s=None, timeout=15.0, state_dir=None, *, clock=time.time, sleep=time.sleep, **unknown):
        if unknown:
            raise ValueError("unknown option(s): %s (known: %s)" % (", ".join(sorted(unknown)), ", ".join(OPTIONS)))
        m = URL_RE.match(str(url or "").strip())
        if not m:
            raise ValueError("url: https://<address of the console or controller>")
        self._host = m.group(2).strip("[]")
        base = "%s://%s%s" % (m.group(1).lower(), m.group(2), ":" + m.group(3) if m.group(3) else "")
        site = str(site or "default").strip()
        if not SITE_RE.match(site):
            raise ValueError("site")
        self._site = site
        self._user = str(write_username or "").strip()
        self._password = write_password if isinstance(write_password, Secret) else Secret(write_password)
        if not self._user or not self._password or len(self._user) > 128 or any(ord(c) < 32 for c in self._user):
            raise ValueError("write_username and write_password are needed")
        if allow_self_signed and not cert_sha256:
            log.warning("unifi: allow_self_signed without cert_sha256 - the connection is not bound to the console's "
                        "certificate; set cert_sha256 (see docs/integrations/unifi.md)")
        self._t = Transport(base, ALLOW, allow_self_signed=bool(allow_self_signed), timeout=float(timeout),
                            pin_sha256=cert_sha256 or None)
        self._session = session.Session(session._key(self._host, self._user, self._password.reveal()))
        ledger = Path(state_dir) / "unifi-signins.json" if state_dir else None
        self._ledger = session.FileLedger(ledger, per_hour=WRITE_CAPABILITIES["limits"]["logins_per_hour"], clock=clock)
        self._relogin_free = False
        self._dead = None
        self._account_ok = None
        self.settle_s = SETTLE_WIRELESS_S if settle_s is None else max(0, int(settle_s))
        self.clock, self.sleep = clock, sleep
        self._raw = {}
        self._state = None
        self._refs = {}
        self._nets = {}
        self._probe_fail = 0
        self.writes = []                       # (user id, fields) of every PUT, for the protocol of tests

    def __repr__(self):
        return "UnifiWriter(%s, site=%r)" % (self._host, self._site)

    __str__ = __repr__

    # ------------------------------------------------------------------ contract basics
    def capabilities(self) -> dict:
        return capabilities.validate(WRITE_CAPABILITIES, self.family)

    def endpoints(self) -> list:
        return [self._host]

    # ------------------------------------------------------------------ session
    def _send(self, method: str, path: str, body=None):
        try:
            status, heads, data = self._t.request(method, path, body, headers=self._session.headers())
        except Forbidden:
            raise WriteError("unifi.refused_by_allow_list") from None
        except TransportError as exc:
            raise WriteError(exc.key) from None
        self._session.absorb(heads)
        parsed = None
        if data:
            try:
                parsed = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                parsed = None
        return status, parsed

    def _sign_in(self, allow_relogin: bool = True) -> None:
        if self._dead:
            raise WriteError("unifi.session_refused")
        try:
            self._ledger.book()
        except session.LoginPaused:
            raise WriteError("unifi.sign_in_paused") from None
        s = self._session
        style_known = s.style
        s.forget()
        order = [style_known] if style_known else ["console", "classic"]
        creds = {"username": self._user, "password": self._password.reveal()}
        refused = False
        for i, style in enumerate(order):
            status, body = self._send("POST", LOGIN[style], creds)
            more = i + 1 < len(order)
            if more and status in (401, 404, 405):
                refused = refused or status == 401
                continue
            if status in (400, 401, 403) or (refused and status in (404, 405)):
                self._ledger.refused()
                raise WriteError("unifi.sign_in_refused")
            if not 200 <= status < 300 or not s.headers().get("Cookie"):
                raise WriteError("unifi.sign_in_failed")
            s.started(style)
            self._relogin_free = allow_relogin
            return
        raise WriteError("unifi.sign_in_failed")

    def _abort(self, why: str):
        self._dead = why
        self._session.forget()
        try:
            self._ledger.refused()
        except OSError:
            pass
        raise WriteError("unifi.session_refused")

    def _call(self, method: str, rel: str, body=None):
        """One request with at most one new sign-in after 401; a second 401 ends this session for good (no loop)."""
        if self._dead:
            raise WriteError("unifi.session_refused")
        if not self._session.signed_in:
            self._sign_in()
        path = "%s/%s" % (PREFIX[self._session.style], rel.format(site=self._site))
        status, data = self._send(method, path, body)
        if status == 401:
            if not self._relogin_free:
                self._abort("401")
            self._relogin_free = False
            self._sign_in(allow_relogin=False)
            path = "%s/%s" % (PREFIX[self._session.style], rel.format(site=self._site))
            status, data = self._send(method, path, body)
            if status == 401:
                self._abort("401 after a fresh sign-in")
        if status == 403:
            raise WriteError("unifi.forbidden")             # the account lacks a right; signing in again cannot help
        return status, data

    def _get(self, area: str, optional: bool = True):
        status, body = self._call("GET", AREAS[area])
        if status in (400, 404, 405) and optional:
            return None
        meta = body.get("meta") if isinstance(body, dict) else None
        if not 200 <= status < 300 or (isinstance(meta, dict) and meta.get("rc") not in (None, "ok")):
            if "LoginRequired" in _msg(body):
                self._session.forget()
            raise WriteError("unifi.read_failed", _msg(body))
        return body

    def _user_record(self, uid: str) -> dict:
        status, body = self._call("GET", "api/s/{site}/rest/user/" + uid)
        data = body.get("data") if isinstance(body, dict) else None
        if status != 200 or not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise WriteError("unifi.client_gone", _msg(body))
        return {f: data[0].get(f) for f in model.USER_FIELDS if f in data[0]}

    def sign_out(self) -> None:
        s = self._session
        if not s.signed_in:
            return
        try:
            self._send("POST", LOGOUT[s.style], {})
        except WriteError:
            pass
        finally:
            s.forget()

    # ------------------------------------------------------------------ account check
    def account(self) -> tuple:
        """(ok, warnings) for the write account: refused when it cannot write; owner-level roles give a warning."""
        status, body = self._call("GET", "api/s/{site}/self")
        data = body.get("data") if isinstance(body, dict) else None
        me = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
        role = "superadmin" if me.get("is_super") else ("owner" if me.get("is_owner") else str(me.get("site_role") or ""))
        ok, warnings = vet_write_account({"principal": self._user, "role": role})
        if role == "readonly":
            return False, ["read_only"]
        return ok, warnings

    # ------------------------------------------------------------------ reading
    def _read_raw(self) -> dict:
        raw = {}
        for area in AREAS:
            body = self._get(area, optional=area not in ("networkconf", "user"))
            if body is not None:
                raw[area] = body
        return raw

    def read(self) -> dict:
        raw = self._read_raw()
        state = model.to_neutral(raw)
        state["controller"] = dict(state.get("controller") or {}, label=self.label, kind="controller")
        state["side_effects"] = {"wireless_move": "reprovision_all"}
        self._raw, self._state = raw, state
        self._nets = model.network_refs(raw)
        self._refs = model.client_refs(raw)
        return state

    def health(self) -> tuple:
        if self._dead:
            return False, "session refused"
        if self.clock() < self._ledger.paused_until():
            return False, "sign-in paused"
        try:
            self._get("sysinfo")
            if self._account_ok is None:
                ok, warnings = self.account()
                self._account_ok = ok
                if "owner_role" in warnings:
                    log.warning("unifi: the write account has an owner-level role; a site administrator without system "
                                "rights is enough (see docs/integrations/unifi.md)")
            if not self._account_ok:
                return False, "account cannot write"
        except WriteError as exc:
            return False, exc.key
        return True, ""

    # ------------------------------------------------------------------ plan steps -> client settings
    def _net(self, nid: str) -> dict:
        ref = self._nets.get(nid)
        if not ref:
            raise WriteError("unifi.network_unknown")
        if ref["purpose"] not in LOCAL_PURPOSES or not ref["cidr"]:
            raise WriteError("unifi.network_not_local")
        return ref

    def _check_address(self, ip: str, net: dict, uid: str) -> None:
        addr = ipaddress.ip_address(ip)
        cidr = ipaddress.ip_network(net["cidr"], strict=False)
        if addr not in cidr or addr in (cidr.network_address, cidr.broadcast_address) or ip == net.get("gateway_ip"):
            raise WriteError("unifi.address_outside")
        for other in self._refs.values():
            rec = other["record"]
            if other["user"] != uid and rec.get("use_fixedip") and rec.get("fixed_ip") == ip:
                raise WriteError("unifi.address_taken")
        for dev in (self._state or {}).get("devices") or []:
            ref = self._refs.get(dev["id"]) or {}
            if ref.get("user") != uid and dev.get("ip") == ip and dev.get("online", True):
                raise WriteError("unifi.address_taken")

    def body_for(self, step: dict) -> tuple:
        """(classic user id, the client fields this step writes). Raises WriteError when it must not be written."""
        kind = step["kind"]
        if kind not in ("move", "client.set"):
            raise WriteError("unifi.kind_not_supported")
        oid = stepkinds.step_object(step)
        ref = self._refs.get(oid)
        if not ref:
            raise WriteError("unifi.client_unknown")
        after = step["after"]
        dev = next((d for d in (self._state or {}).get("devices") or [] if d["id"] == oid), {})
        body = {}
        if kind == "move" or "network" in after:
            if ref.get("wired"):
                raise WriteError("unifi.wired_move")
            net = self._net(after["network"])
            body.update(virtual_network_override_enabled=True, virtual_network_override_id=net["id"])
            ip = after["ip"] if kind == "move" else after.get("fixed_ip", "")
            if ip:
                self._check_address(ip, net, ref["user"])
                body.update(use_fixedip=True, fixed_ip=ip, network_id=net["id"])
        if kind == "client.set":
            if "name" in after:
                body["name"] = after["name"]
            if "fixed_ip" in after and "network" not in after:
                if after["fixed_ip"]:
                    net = self._net(dev.get("network") or "")
                    self._check_address(after["fixed_ip"], net, ref["user"])
                    body.update(use_fixedip=True, fixed_ip=after["fixed_ip"], network_id=net["id"])
                else:
                    body["use_fixedip"] = False
        if not body:
            raise WriteError("unifi.nothing_to_write")
        return ref["user"], body

    @staticmethod
    def _matches(record: dict, body: dict) -> bool:
        return all(record.get(k, "" if isinstance(v, str) else (False if isinstance(v, bool) else None)) == v for k, v in body.items())

    def _put(self, uid: str, body: dict) -> None:
        status, reply = self._call("PUT", "api/s/{site}/rest/user/" + uid, body)
        meta = reply.get("meta") if isinstance(reply, dict) else None
        if not 200 <= status < 300 or (isinstance(meta, dict) and meta.get("rc") not in (None, "ok")):
            raise WriteError("unifi.write_refused", _msg(reply))
        self.writes.append((uid, sorted(body)))
        if not self._matches(self._user_record(uid), body):
            raise WriteError("unifi.not_applied")

    def snapshot(self, ids) -> dict:
        """The client settings as stored now, read fresh (the way back writes exactly these)."""
        raw = dict(self._raw)
        raw["user"] = self._get("user", optional=False)
        self._refs = model.client_refs(raw)
        out = []
        for oid in ids:
            ref = self._refs.get(oid)
            if not ref:
                raise WriteError("unifi.client_unknown")
            out.append({"id": oid, "user": ref["user"], "record": self._user_record(ref["user"])})
        return {"devices": out}

    def apply(self, step: dict):
        uid, body = self.body_for(step)
        if self._matches(self._user_record(uid), body):
            return None                                     # already as planned (idempotent)
        self._put(uid, body)
        return None

    def _restore_body(self, saved: dict, now: dict) -> dict:
        body = {}
        if saved.get("name", "") != now.get("name", ""):
            body["name"] = saved.get("name") or ""
        if saved.get("use_fixedip"):
            for k in ("use_fixedip", "fixed_ip", "network_id"):
                if saved.get(k) not in (None, "") and saved.get(k) != now.get(k):
                    body[k] = saved[k]
            if body.keys() & {"fixed_ip", "network_id"}:
                body["use_fixedip"] = True
        elif now.get("use_fixedip"):
            body["use_fixedip"] = False
        if saved.get("virtual_network_override_enabled"):
            if not now.get("virtual_network_override_enabled") or now.get("virtual_network_override_id") != saved.get("virtual_network_override_id"):
                body["virtual_network_override_enabled"] = True
                if _oid_or_empty(saved.get("virtual_network_override_id")) and saved.get("virtual_network_override_id"):
                    body["virtual_network_override_id"] = saved["virtual_network_override_id"]
        elif now.get("virtual_network_override_enabled"):
            body["virtual_network_override_enabled"] = False
        return body

    def restore(self, snapshot: dict) -> None:
        """Put every touched client back as saved. Clients that give up a fixed address go first, so two clients that
        swapped addresses never collide; a failure is retried once after the others."""
        items = [d for d in (snapshot or {}).get("devices") or [] if isinstance(d, dict) and _OID_RE.match(str(d.get("user") or ""))]
        todo = []
        for d in items:
            now = self._user_record(d["user"])
            body = self._restore_body(d.get("record") or {}, now)
            if body:
                todo.append((d["user"], body, bool(now.get("use_fixedip")) and not body.get("use_fixedip", True)))
        todo.sort(key=lambda x: not x[2])
        failed = []
        for uid, body, _ in todo:
            try:
                self._put(uid, body)
            except WriteError:
                failed.append((uid, body))
        for uid, body in failed:
            self._put(uid, body)

    # ------------------------------------------------------------------ after the change
    def settle(self, steps: list) -> None:
        wait = 0
        for st in steps:
            ref = self._refs.get(stepkinds.step_object(st)) or {}
            if st["kind"] == "move" or "network" in st["after"]:
                if ref.get("online") and not ref.get("wired"):
                    wait = max(wait, self.settle_s)
            elif "fixed_ip" in st["after"]:
                wait = max(wait, SETTLE_ADDRESS_S)
        if wait:
            self.sleep(wait)

    def probe(self, steps: list) -> dict:
        """Watch: every client still carries the settings of the plan; a moved client that is online sits in its new
        network. A wireless client that is offline right now is not counted (it rejoins when it wakes up). Two failed
        reads in a row of the controller itself count as a global failure."""
        try:
            sta = self._get("sta") or {}
        except WriteError:
            self._probe_fail += 1
            return {"global_ok": self._probe_fail < 2, "missing": []}
        self._probe_fail = 0
        online = {}
        for c in model.items({"sta": sta}, "sta"):
            mac = model.norm_mac(c.get("mac"))
            if mac:
                online[model.oid("c", mac)] = c
        missing = []
        for st in steps:
            oid = stepkinds.step_object(st)
            try:
                uid, body = self.body_for(st)
                if not self._matches(self._user_record(uid), body):
                    missing.append(oid)
                    continue
            except WriteError:
                missing.append(oid)
                continue
            if "virtual_network_override_id" in body and oid in online:
                now_net = online[oid].get("network_id")
                if now_net and now_net != body["virtual_network_override_id"]:
                    missing.append(oid)
        return {"global_ok": True, "missing": missing}

    def lockout(self, state: dict, step: dict):
        """Family rules on top of the neutral ones: a wired client changes its network through its switch port (port
        settings come with a later stage), and only local networks take clients."""
        if step.get("kind") not in ("move", "client.set"):
            return "fw.block.capability"
        ref = self._refs.get(stepkinds.step_object(step)) or {}
        after = step.get("after") or {}
        if step["kind"] == "move" or "network" in after:
            if ref.get("wired"):
                return "fw.block.wired_move"
            net = self._nets.get(after.get("network"))
            if net and net["purpose"] not in LOCAL_PURPOSES:
                return "fw.block.network_kind"
        return None
