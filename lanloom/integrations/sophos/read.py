"""Read half of the Sophos Firewall family: reads the firewall through its local XML API, never writes.

The XML API has no session: every request is one query document ``<Request><Login>...</Login><Get>...</Get>
</Request>`` sent as the form field ``reqxml`` to ``/webconsole/APIController``. The transport's read allow-list
holds exactly one rule for it, and that rule's value check (``is_read_query``) lets a document through only when it
holds the account and a ``<Get>`` of known entities - a ``<Set>`` or ``<Remove>`` can never leave this process.

One request per poll reads every entity (one sign-in every 5 minutes). Should a firmware refuse the combined list
(an entity it does not know), the half asks entity by entity once and from then on leaves the unknown ones out.
After a refused sign-in it pauses for 15 minutes, so a wrong password never locks the account on the firewall.

Event: ``{"type": "config", "source": "family", "family": "sophos", "state": <neutral model v2>, "capabilities": ...}``
- what the firewall tab shows (read only).

Product names are used only to identify compatible products; no affiliation or endorsement.
"""
import asyncio
import re
import time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from . import CAPABILITIES, model
from .. import capabilities
from ..credentials import Secret
from ..transport import Forbidden, Transport, TransportError, read_allowlist
from ...sources._common import SourceError, StatusTracker, emit_event, now, run_with_backoff, short_test

MODULE = "family"
SOURCE = "family"
URL_RE = re.compile(r"\A(https?)://([A-Za-z0-9.-]{1,253}(?::\d{1,5})?|\[[0-9A-Fa-f:.]{2,45}\](?::\d{1,5})?)/?\Z", re.I)
API_PATH = "/webconsole/APIController"
FULL_S = 300.0
PAUSE_S = 900.0                 # after a refused sign-in
MAX_QUERY = 8192
READ_ENTITIES = ("Zone", "Interface", "VLAN", "Alias", "IPHost", "IPHostGroup", "FQDNHost", "Services", "ServiceGroup",
                 "FirewallRule", "FirewallRuleGroup", "NATRule", "DHCPServer", "UnicastRoute", "VPNIPSecConnection",
                 "SSLVPNPolicy")
LOGIN_OK_WORD = "success"         # "Authentication Successful" (the API reference also shows "Authetication Successful")


def is_read_query(doc) -> bool:
    """True only for ``<Request><Login><Username/><Password/></Login><Get><Entity/>...</Get></Request>`` with known
    entities, each once, nothing else (no attributes, no declarations, no other element anywhere)."""
    if not isinstance(doc, str) or len(doc) > MAX_QUERY or "<!" in doc or "<?" in doc:
        return False
    root = ET.fromstring(doc)
    if root.tag != "Request" or set(root.attrib) - {"APIVersion"} or (root.text or "").strip():
        return False
    kids = list(root)
    if [k.tag for k in kids] != ["Login", "Get"] or any((k.tail or "").strip() for k in kids):
        return False
    login, get = kids
    if login.attrib or (login.text or "").strip() or [k.tag for k in login] != ["Username", "Password"]:
        return False
    if any(list(k) or k.attrib or (k.tail or "").strip() for k in login):
        return False
    tags = [e.tag for e in get]
    if get.attrib or (get.text or "").strip() or not tags or len(set(tags)) != len(tags):
        return False
    return all(e.tag in READ_ENTITIES and not e.attrib and not list(e) and not (e.text or "").strip()
               and not (e.tail or "").strip() for e in get)


ALLOW = read_allowlist((), query=(API_PATH, {"reqxml": is_read_query}))


def _secret(value) -> Secret:
    return value if isinstance(value, Secret) else Secret(value)


class SophosReader:
    family = "sophos"
    name = SOURCE

    def __init__(self, url=None, username=None, password=None, allow_self_signed=False, interval=FULL_S, timeout=15.0):
        m = URL_RE.match(str(url or "").strip())
        if not m:
            raise SourceError("source.family.config", "address missing or invalid")
        self._base = "%s://%s" % (m.group(1).lower(), m.group(2))
        self._host = m.group(2)
        self._user = str(username or "").strip()
        self._password = _secret(password)
        if not self._user or not self._password:
            raise SourceError("source.family.config", "user name and password are needed")
        if len(self._user) > 128 or any(ord(c) < 32 for c in self._user):
            raise SourceError("source.family.config", "user name invalid")
        self._t = Transport(self._base, ALLOW, allow_self_signed=bool(allow_self_signed), timeout=timeout)
        self.interval = max(30.0, float(interval))
        self._unknown = set()            # entities this firmware refused in a combined request
        self._paused_until = 0.0
        self._caps = capabilities.validate(CAPABILITIES, self.family)
        self.last_entity_status = {}

    def __repr__(self):
        return "SophosReader(%s)" % self._host

    __str__ = __repr__

    # ------------------------------------------------------------------ one request
    def _query(self, entities) -> str:
        get = "".join("<%s></%s>" % (e, e) for e in entities)
        return "<Request><Login><Username>%s</Username><Password>%s</Password></Login><Get>%s</Get></Request>" % (
            escape(self._user), escape(self._password.reveal()), get)

    def _ask(self, entities) -> tuple:
        if time.monotonic() < self._paused_until:
            raise SourceError("source.family.paused", "sign-in paused")
        try:
            status, _, data = self._t.request("POST", API_PATH, {"reqxml": self._query(entities)},
                                              headers={"Accept": "application/xml, text/xml"})
        except Forbidden:
            raise SourceError("source.family.refused", "request outside the allow-list") from None
        except TransportError as exc:
            raise SourceError("source.family.unreachable" if exc.key.endswith("unreachable") else "source.family.protocol") from None
        if status in (401, 403):
            raise SourceError("source.family.forbidden", "web console refused the request")
        if not 200 <= status < 300:
            raise SourceError("source.family.protocol", "unexpected status %d" % status)
        try:
            meta, ents = model.parse_reply(data)
        except model.ReplyError:
            raise SourceError("source.family.protocol", "reply is not an API document") from None
        st = (meta.get("status") or "").lower()
        if meta.get("status_code") == "534" or "requester ip" in st or "not allowed from" in st:
            raise SourceError("source.family.api_ip", "API access is not allowed from this address")
        if "enable the api" in st or "api configuration" in st or meta.get("status_code") == "532":
            raise SourceError("source.family.api_off", "API is switched off on the firewall")
        login = (meta.get("login") or "").lower()
        if meta.get("login") is not None and (LOGIN_OK_WORD not in login or "fail" in login):
            self._paused_until = time.monotonic() + PAUSE_S
            raise SourceError("source.family.auth_failed", "sign-in refused")
        return meta, ents

    # ------------------------------------------------------------------ polls
    def collect(self) -> dict:
        """Blocking: every entity this poll could get, as ``{"api_version", "entities"}``. Call from a thread."""
        wanted = [e for e in READ_ENTITIES if e not in self._unknown]
        meta, ents = self._ask(wanted)
        if meta.get("status") and meta.get("login") is not None and not any(ents.values()):
            ents, statuses = {}, {}                       # the combined list was refused: ask one by one, once
            for e in wanted:
                try:
                    m1, got = self._ask([e])
                except SourceError as exc:
                    if exc.key in ("source.family.auth_failed", "source.family.paused", "source.family.unreachable"):
                        raise
                    m1, got = {"status": "refused"}, {}
                if m1.get("status") and not got:
                    self._unknown.add(e)
                ents.update(got)
                statuses.update(m1.get("entity_status") or {})
            meta = dict(meta, entity_status=statuses)
        self.last_entity_status = dict(meta.get("entity_status") or {})
        if not any(ents.values()) and self.last_entity_status and len(self.last_entity_status) >= len(wanted):
            raise SourceError("source.family.forbidden", "account may not read the configuration")
        return {"api_version": meta.get("api_version") or "", "entities": ents}

    def capabilities(self) -> dict:
        return self._caps

    def _refine(self, raw: dict, state: dict) -> dict:
        ents = raw.get("entities") or {}
        missing = set()
        if not ents.get("Zone"):
            missing.add("zones")
        if not ents.get("FirewallRule"):
            missing.add("policies")
        if not ents.get("NATRule"):
            missing.add("port_forwards")
        if not ents.get("DHCPServer"):
            missing.add("dhcp_reservations")
        if not (ents.get("VPNIPSecConnection") or ents.get("SSLVPNPolicy")):
            missing.add("vpn")
        caps = capabilities.refine(capabilities.validate(CAPABILITIES, self.family), version=state["controller"]["version"] or "",
                                   read_missing=missing)
        self._caps = caps
        return caps

    def config_event(self, raw: dict) -> dict:
        state = model.to_neutral(raw)
        return {"type": "config", "source": SOURCE, "family": self.family, "t": now(), "state": state,
                "capabilities": self._refine(raw, state)}

    async def poll(self, light: bool = False) -> list:
        raw = await asyncio.to_thread(self.collect)
        return [self.config_event(raw)]

    # ------------------------------------------------------------------ source contract
    async def run(self, emit):
        async def one_session(emit_, status: StatusTracker):
            while True:
                for ev in await self.poll():
                    await emit_event(emit_, ev)
                await status.set(emit_, True, "source.family.ok")
                await asyncio.sleep(self.interval)

        await run_with_backoff(SOURCE, MODULE, emit, one_session)

    async def test(self):
        async def probe():
            raw = await asyncio.to_thread(self.collect)
            st = model.to_neutral(raw)
            return {"version": st["controller"]["version"] or None, "zones": len(st["zones"]), "networks": len(st["networks"]),
                    "policies": len({p["vendor_ref"] for p in st["policies"]}), "port_forwards": len(st["port_forwards"])}

        return await short_test(MODULE, probe(), self._t.timeout * 3 + 10)

    async def account_scope(self) -> str:
        """The XML API does not tell the rights of the signed-in account, so this reports ``unknown``; the guide asks
        for an administrator profile that only reads (the transport cannot send a change either way)."""
        return "unknown"
