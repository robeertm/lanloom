"""Update notice and one-click update.

* The release feed is asked at most every 6 hours, only while someone has the page open, and never when the
  check is switched off (setting or LANLOOM_UPDATE_CHECK=0). It is the only outbound connection Lanloom makes.
* With the optional updater sidecar, "Update" writes a request into the shared data volume; the sidecar pulls the
  new image, tests it (self-test without network, then a probe start on a copy of the data), backs up the database,
  swaps the container and rolls back automatically if the new version does not come up healthy.
* Without the sidecar the page shows the two commands for a manual update.
"""
import asyncio
import json
import os
import re
import secrets
import ssl
import time
import urllib.request
from pathlib import Path
from typing import Optional

from . import __version__

CHECK_EVERY = 6 * 3600
SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_version(text: str) -> Optional[tuple]:
    m = SEMVER.match((text or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


class Updates:
    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store
        self.dir = Path(cfg.data_dir) / "update"
        self._lock = asyncio.Lock()

    def enabled(self) -> bool:
        return bool(self.cfg.update_check) and bool(self.store.get_setting("update_check", True))

    def cached(self) -> dict:
        return self.store.get_setting("update_cache", {}) or {}

    async def check(self, force: bool = False) -> dict:
        if not self.enabled() or self.cfg.demo:
            return self.state()
        async with self._lock:
            cache = self.cached()
            if not force and time.time() - cache.get("checked", 0) < CHECK_EVERY:
                return self.state()
            try:
                info = await asyncio.to_thread(self._fetch)
                cache = {"checked": time.time(), "latest": info.get("version"), "notes": info.get("notes", "")[:20000],
                         "url": info.get("url"), "published": info.get("published"), "error": None}
            except Exception as exc:     # network errors must never break the page
                cache = dict(cache, checked=time.time(), error=type(exc).__name__)
            self.store.set_setting("update_cache", cache)
        return self.state()

    def _fetch(self) -> dict:
        req = urllib.request.Request(self.cfg.release_feed, headers={
            "Accept": "application/json", "User-Agent": "lanloom/%s" % __version__})
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            raw = resp.read(512 * 1024)
        data = json.loads(raw.decode("utf-8"))
        tag = str(data.get("tag_name") or "")
        if not parse_version(tag) or data.get("draft") or data.get("prerelease"):
            raise ValueError("no stable release")
        url = str(data.get("html_url") or "")
        if not url.startswith("https://"):
            url = ""
        return {"version": tag.lstrip("v"), "notes": str(data.get("body") or ""), "url": url,
                "published": data.get("published_at")}

    def sidecar(self) -> Optional[dict]:
        try:
            info = json.loads((self.dir / "sidecar.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if time.time() - float(info.get("t", 0)) > 120:
            return None
        return info

    def status(self) -> dict:
        try:
            return json.loads((self.dir / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"state": "idle"}

    def state(self) -> dict:
        cache = self.cached()
        latest = cache.get("latest")
        newer = bool(latest and parse_version(latest) and parse_version(__version__)
                     and parse_version(latest) > parse_version(__version__))
        return {"current": __version__, "enabled": self.enabled(), "latest": latest, "available": newer,
                "notes": cache.get("notes") if newer else "", "url": cache.get("url"), "checked": cache.get("checked"),
                "error": cache.get("error"), "sidecar": self.sidecar() is not None, "status": self.status(),
                "image": self.cfg.image_repo, "demo": self.cfg.demo}

    def request(self, version: str) -> dict:
        if not parse_version(version):
            raise ValueError("update.err.invalid_version")
        if not self.sidecar():
            raise ValueError("update.err.no_sidecar")
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {"version": version.lstrip("v"), "nonce": secrets.token_hex(16), "requested": time.time()}
        tmp = self.dir / ".request.tmp"
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, self.dir / "request.json")
        return payload
