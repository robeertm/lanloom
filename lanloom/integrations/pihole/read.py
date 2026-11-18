"""Read half of the Pi-hole family: reads the filter's totals through its local API, never writes.

Requests (all GET except the sign-in): summary, history (10-minute slots), top blocked names, top clients (all and
blocked), upstream resolvers, state of the blocking, block lists and version (the last two every 15 minutes only).
The sign-in is ``POST /api/auth`` with the password; the session id travels in a header and is reused until the filter
ends it. No password configured: the filter may answer without one (an open API); a 401 then reports a missing password.

Product names are used only to identify compatible products; no affiliation or endorsement.
"""
from . import CAPABILITIES, model
from ..dnsbase import DnsReader, secret_of
from ..transport import Forbidden, TransportError, read_allowlist
from ...sources._common import SourceError

SIGN_IN = "/api/auth"
GET_PATHS = (
    r"/api/stats/summary", r"/api/history", r"/api/dns/blocking", r"/api/lists", r"/api/info/version",
    r"/api/stats/top_domains\?blocked=true&count=\d{1,3}",
    r"/api/stats/top_clients\?(?:blocked=true&)?count=\d{1,3}",
    r"/api/stats/upstreams",
)
ALLOW = read_allowlist(GET_PATHS, login=(SIGN_IN, ("password",)))


class PiholeReader(DnsReader):
    family = "pihole"
    ALLOW = ALLOW
    CAPS = CAPABILITIES

    def __init__(self, url=None, password=None, allow_self_signed=False, **kw):
        self._password = secret_of(password)
        self._sid = None
        super().__init__(url, allow_self_signed=allow_self_signed, **kw)
        if len(self._password.reveal()) > 256:
            raise SourceError("source.family.config", "password invalid")

    def _auth_headers(self) -> dict:
        return {"X-FTL-SID": self._sid} if self._sid else {}

    def _sign_in(self) -> None:
        if not self._password:
            raise SourceError("source.family.auth_failed", "the API needs a password")
        self._pause_check()
        self._sid = None
        try:
            status, body = self._t.json("POST", SIGN_IN, {"password": self._password.reveal()})
        except Forbidden:
            raise SourceError("source.family.refused") from None
        except TransportError as exc:
            raise SourceError("source.family.unreachable" if exc.key.endswith("unreachable") else "source.family.protocol") from None
        sess = (body or {}).get("session") if isinstance(body, dict) else None
        if status == 429:
            self._pause()
            raise SourceError("source.family.paused", "too many sign-ins")
        if status in (401, 403) or not isinstance(sess, dict) or not sess.get("valid"):
            self._pause()
            raise SourceError("source.family.auth_failed", "sign-in refused")
        sid = sess.get("sid")
        self._sid = sid if isinstance(sid, str) and 0 < len(sid) <= 256 and sid.isprintable() else None

    def collect(self) -> dict:
        if self._password and self._sid is None:
            self._sign_in()
        f = self.fetch
        raw = {"summary": f("/api/stats/summary"), "history": f("/api/history"),
               "top_blocked": f("/api/stats/top_domains?blocked=true&count=10"),
               "clients": f("/api/stats/top_clients?count=50"), "clients_blocked": f("/api/stats/top_clients?blocked=true&count=50"),
               "upstreams": f("/api/stats/upstreams", optional=True), "blocking": f("/api/dns/blocking", optional=True)}
        if not isinstance(raw["summary"], dict) or "queries" not in raw["summary"]:
            raise SourceError("source.family.protocol", "not a Pi-hole version 6 answer")
        raw["lists"], raw["version"] = self.slow(lambda: (f("/api/lists", optional=True), f("/api/info/version", optional=True)))
        return raw

    def reading(self, raw: dict) -> dict:
        return model.to_neutral(raw)
