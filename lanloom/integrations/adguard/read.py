"""Read half of the AdGuard Home family: reads the filter's totals through its local API, never writes.

Three GET requests per poll (status, statistics) and per 15 minutes (filter lists), HTTP basic authentication with the
account entered in the dialog, no session, no POST. The query log (``/control/querylog``) is not on the allow-list.

Product names are used only to identify compatible products; no affiliation or endorsement.
"""
import base64
import time

from . import CAPABILITIES, model
from ..dnsbase import DnsReader, secret_of
from ..transport import read_allowlist
from ...sources._common import SourceError

GET_PATHS = (r"/control/status", r"/control/stats", r"/control/filtering/status")
ALLOW = read_allowlist(GET_PATHS)


class AdguardReader(DnsReader):
    family = "adguard"
    ALLOW = ALLOW
    CAPS = CAPABILITIES

    def __init__(self, url=None, username=None, password=None, allow_self_signed=False, **kw):
        self._user = str(username or "").strip()
        self._password = secret_of(password)
        if not self._user or not self._password:
            raise SourceError("source.family.config", "user name and password are needed")
        if len(self._user) > 128 or any(ord(c) < 32 for c in self._user) or ":" in self._user or len(self._password.reveal()) > 256:
            raise SourceError("source.family.config", "user name or password invalid")
        super().__init__(url, allow_self_signed=allow_self_signed, **kw)

    def _auth_headers(self) -> dict:
        token = base64.b64encode(("%s:%s" % (self._user, self._password.reveal())).encode("utf-8")).decode("ascii")
        return {"Authorization": "Basic " + token}

    def collect(self) -> dict:
        f = self.fetch
        raw = {"status": f("/control/status"), "stats": f("/control/stats")}
        if not isinstance(raw["status"], dict) or not isinstance(raw["stats"], dict):
            raise SourceError("source.family.protocol", "not an AdGuard Home answer")
        raw["filtering"] = self.slow(lambda: f("/control/filtering/status", optional=True))
        return raw

    def reading(self, raw: dict) -> dict:
        return model.to_neutral(raw, time.time())
