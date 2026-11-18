"""Shared part of the read halves of the DNS filter families.

A DNS filter read half asks the filter's local web API for **totals only** - queries and blocked queries per hour,
the top lists, the upstream resolvers and the block lists - never for single queries: no family's allow-list holds
a path of a query log (a static test checks it). Everything goes through the transport and its GET-only allow-list;
the only POST a family may add is the sign-in with exactly its credential fields.

Event: ``{"type": "dns", "source": "family", "family": <id>, "t": ..., "state": <neutral reading>}`` (see
``lanloom/dnsfilter.py``). After a refused sign-in the half pauses for 15 minutes, so a wrong password never locks the
account on the filter.

Product names are used only to identify compatible products; no affiliation or endorsement.
"""
import asyncio
import re
import time

from .. import dnsfilter
from . import capabilities
from .credentials import Secret
from .transport import Forbidden, Transport, TransportError
from ..sources._common import SourceError, emit_event, now, run_with_backoff, short_test, StatusTracker

MODULE = "family"
SOURCE = "family"
URL_RE = re.compile(r"\A(https?)://([A-Za-z0-9.-]{1,253}(?::\d{1,5})?|\[[0-9A-Fa-f:.]{2,45}\](?::\d{1,5})?)/?\Z", re.I)
POLL_S = 120.0
SLOW_S = 900.0                    # block lists and version change rarely: asked every 15 minutes only
PAUSE_S = 900.0                   # after a refused sign-in


def secret_of(value) -> Secret:
    return value if isinstance(value, Secret) else Secret(value)


class Unauthorized(Exception):
    """The filter answered 401/403 to a request (internal; the half signs in again or reports the key)."""


class DnsReader:
    family = ""
    ALLOW = None                  # read allow-list of the family (set by the subclass)
    CAPS = None                   # capabilities of the family (set by the subclass)

    def __init__(self, url=None, allow_self_signed=False, interval=POLL_S, timeout=10.0):
        m = URL_RE.match(str(url or "").strip())
        if not m:
            raise SourceError("source.family.config", "address missing or invalid")
        self._base = "%s://%s" % (m.group(1).lower(), m.group(2))
        self._host = m.group(2)
        self._t = Transport(self._base, self.ALLOW, auth_headers=self._auth_headers, allow_self_signed=bool(allow_self_signed),
                            timeout=timeout)
        self.interval = max(30.0, float(interval))
        self._paused_until = 0.0
        self._slow = None
        self._slow_at = 0.0
        self._caps = capabilities.validate(self.CAPS, self.family)

    def __repr__(self):
        return "%s(%s)" % (type(self).__name__, self._host)

    __str__ = __repr__

    # ------------------------------------------------------------------ to be provided by a family
    def _auth_headers(self) -> dict:
        return {}

    def _sign_in(self) -> None:
        """Sign in again (families with a session). Raises SourceError."""

    def collect(self) -> dict:
        """Blocking: the answers of every endpoint of this poll, as raw JSON values. Call from a thread."""
        raise NotImplementedError

    def reading(self, raw: dict) -> dict:
        raise NotImplementedError

    # ------------------------------------------------------------------ one request
    def _pause_check(self) -> None:
        if time.monotonic() < self._paused_until:
            raise SourceError("source.family.paused", "sign-in paused")

    def _pause(self) -> None:
        self._paused_until = time.monotonic() + PAUSE_S

    def get(self, path: str, *, optional: bool = False):
        """GET one endpoint and return its JSON. ``optional``: a 404 (older version) is None instead of an error."""
        self._pause_check()
        try:
            status, body = self._t.json("GET", path)
        except Forbidden:
            raise SourceError("source.family.refused", "request outside the allow-list") from None
        except TransportError as exc:
            raise SourceError("source.family.unreachable" if exc.key.endswith("unreachable") else "source.family.protocol") from None
        if status in (401, 403):
            raise Unauthorized()
        if status == 404 and optional:
            return None
        if not 200 <= status < 300:
            raise SourceError("source.family.protocol", "unexpected status %d" % status)
        return body

    def fetch(self, path: str, *, optional: bool = False):
        """``get`` with one fresh sign-in when the session ran out."""
        try:
            return self.get(path, optional=optional)
        except Unauthorized:
            self._sign_in()
            try:
                return self.get(path, optional=optional)
            except Unauthorized:
                self._pause()
                raise SourceError("source.family.auth_failed", "sign-in refused") from None

    def slow(self, loader):
        """The rarely changing part of a poll, reloaded after ``SLOW_S``; a failure keeps the old part."""
        t = time.monotonic()
        if self._slow is None or t - self._slow_at >= SLOW_S:
            try:
                self._slow, self._slow_at = loader(), t
            except SourceError:
                if self._slow is None:
                    raise
        return self._slow

    # ------------------------------------------------------------------ polls
    def capabilities(self) -> dict:
        return self._caps

    def dns_event(self, raw: dict) -> dict:
        state = self.reading(raw)
        state["t"] = now()
        return {"type": "dns", "source": SOURCE, "family": self.family, "t": state["t"], "state": dnsfilter.clean(state)}

    async def poll(self, light: bool = False) -> list:
        raw = await asyncio.to_thread(self.collect)
        return [self.dns_event(raw)]

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
            st = self.reading(raw)
            tot = st["totals"]
            return {"version": st.get("version") or None, "queries": tot.get("queries"), "blocked": tot.get("blocked"),
                    "clients": tot.get("clients")}

        return await short_test(MODULE, probe(), self._t.timeout * 6 + 10)

    async def account_scope(self) -> str:
        """The filters' APIs do not tell the rights of the account; the transport cannot send a change either way."""
        return "unknown"
