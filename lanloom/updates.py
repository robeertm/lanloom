"""Update notice and one-click update.

* The release feed is asked at most every 6 hours (15 minutes after a network error), only while someone has the
  page open, and never when the check is switched off (setting or LANLOOM_UPDATE_CHECK=0). It is the only outbound
  connection of its own; others exist only when switched on (report delivery, cloud AI). The feed lists recent releases; the highest stable version wins, not the newest date,
  so a backport published after a feature release does not hide it.
* With the optional updater sidecar, "Update" writes a request into the shared data volume; the sidecar pulls the
  new image, tests it (self-test without network, then a probe start on a copy of the data), backs up the database,
  swaps the container and rolls back automatically if the new version does not come up healthy.
* Without the sidecar the page shows the two commands for a manual update.
* Channels: "stable" (default) offers the highest stable release; "preview" also offers a pre-release of the release feed when it
  is newer. A pre-release is a plain MAJOR.MINOR.PATCH tag from the same release path as a stable one (same gate, same
  signed image); `:edge` or any other moving tag is never offered. No pre-release -> "no preview available".
* Hold: an administrator keeps the running version; no update notice until the hold is released.
* Rollback: back to the version that ran before the last update (the updater keeps its image as "previous" and names
  it in its heartbeat; older updaters are covered by the backups it made). The request goes through the same checks
  as an update (signature, self-test, probe start, backup, health) and holds the version it goes back to.
* Add-on mode (LANLOOM_ADDON=1): the hub's add-on system updates the add-on. The release feed is never asked, the
  sidecar is never used (update, rollback and the channel answer ``update.err.addon``), and the page shows where updates
  come from instead (``mode: "addon"``).
* Since 1.34.0 the sidecar checks the signature of an official candidate before it pulls; `signature.supported`
  says whether the running sidecar can (older sidecars update without the check until they are renewed).
* Self-renewal: a sidecar whose heartbeat names the feature "renew" renews itself after a healthy update from the
  image it has just checked, and writes the result into renew.json. `sidecar_hint` names the one remark the page shows
  with the renew command: "renew_once" (a sidecar from before self-renewal is behind the app), "renew_failed" (the
  renewal went back to the old sidecar), "renew_manual" (not renewed here: own image, switched off, no own container
  found, socket not usable). The nightly report mentions a failed renewal only.
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
RETRY_AFTER_ERROR = 15 * 60
UPDATER_KEEPS_TAG = "1.17.0"      # first sidecar that keeps the compose tag ("latest") on the running version
FIRST_SIGNED = "1.29.0"           # first official image with a signature; an older one cannot pass the check before a swap
OFFICIAL_REPO = "ghcr.io/robeertm/lanloom"   # same as updater.sigverify.OFFICIAL_REPO (a test keeps them equal)
CHANNELS = ("stable", "preview")
# A run that is not terminal blocks new requests; one older than this is treated as stuck (the sidecar cleans up).
BUSY_FOR = 30 * 60
# Same as updater.engine.TERMINAL (a test keeps them equal; the app does not import the sidecar code).
RUN_TERMINAL = ("idle", "done", "rolled_back", "failed")
# Same as updater.engine.RENEW_STATES (a test keeps them equal).
RENEW_STATES = ("renewing", "done", "rolled_back", "failed", "skipped")
RENEWING_FOR = 10 * 60        # a "renewing" record older than this is stale (the sidecar cleans it up on its next start)
SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_version(text: str) -> Optional[tuple]:
    m = SEMVER.match((text or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def is_older(a: str, b: str) -> bool:
    """True when both are MAJOR.MINOR.PATCH and a is older than b."""
    va, vb = parse_version(str(a or "")), parse_version(str(b or ""))
    return bool(va and vb and va < vb)


def _best(feed, prerelease: bool):
    best = None
    for item in feed:
        if not isinstance(item, dict) or item.get("draft") or bool(item.get("prerelease")) != prerelease:
            continue
        tag = str(item.get("tag_name") or "")
        version = parse_version(tag)       # MAJOR.MINOR.PATCH only: never "edge", "latest" or "-rc" names
        if version and (best is None or version > best[0]):
            best = (version, tag, item)
    return best


def _release(best) -> dict:
    _, tag, data = best
    url = str(data.get("html_url") or "")
    if not url.startswith("https://"):
        url = ""
    return {"version": tag.lstrip("v"), "notes": str(data.get("body") or "")[:20000], "url": url,
            "published": data.get("published_at")}


def pick_release(feed) -> dict:
    """The highest stable release of a release list (no drafts, no pre-releases, only MAJOR.MINOR.PATCH tags)."""
    if not isinstance(feed, list):
        raise ValueError("release feed is not a list")
    best = _best(feed, False)
    if best is None:
        raise ValueError("no stable release")
    return _release(best)


def pick_preview(feed, stable: Optional[str]) -> Optional[dict]:
    """The highest pre-release that is newer than the highest stable release, or None."""
    if not isinstance(feed, list):
        return None
    best = _best(feed, True)
    if best is None or (stable and parse_version(stable) and best[0] <= parse_version(stable)):
        return None
    return _release(best)


class Updates:
    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store
        self.dir = Path(cfg.data_dir) / "update"
        self._lock = asyncio.Lock()

    def addon(self) -> bool:
        return bool(getattr(self.cfg, "addon", False))

    def enabled(self) -> bool:
        return (not self.addon() and bool(self.cfg.update_check)
                and bool(self.store.get_setting("update_check", True)))

    def cached(self) -> dict:
        return self.store.get_setting("update_cache", {}) or {}

    async def check(self, force: bool = False) -> dict:
        if self.addon() or not self.enabled() or self.cfg.demo:
            return self.state()
        async with self._lock:
            cache = self.cached()
            if not force and time.time() - cache.get("checked", 0) < CHECK_EVERY:
                return self.state()
            try:
                info = await asyncio.to_thread(self._fetch)
                cache = {"checked": time.time(), "latest": info.get("version"), "notes": info.get("notes", "")[:20000],
                         "url": info.get("url"), "published": info.get("published"), "error": None,
                         "preview": info.get("preview")}
            except Exception as exc:     # network errors must never break the page
                # back-date the check so the next try comes after RETRY_AFTER_ERROR, not after CHECK_EVERY
                cache = dict(cache, checked=time.time() - CHECK_EVERY + RETRY_AFTER_ERROR, error=type(exc).__name__)
            self.store.set_setting("update_cache", cache)
        return self.state()

    def _fetch(self) -> dict:
        req = urllib.request.Request(self.cfg.release_feed, headers={
            "Accept": "application/json", "User-Agent": "lanloom/%s" % __version__})
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            raw = resp.read(512 * 1024)
        feed = json.loads(raw.decode("utf-8"))
        info = pick_release(feed)
        info["preview"] = pick_preview(feed, info["version"])
        return info

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

    def renewal(self) -> Optional[dict]:
        """The last self-renewal of the sidecar (renew.json), cleaned, or None."""
        try:
            rec = json.loads((self.dir / "renew.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(rec, dict) or rec.get("state") not in RENEW_STATES:
            return None
        reason = rec.get("reason")
        try:
            t = float(rec.get("t") or 0)
        except (TypeError, ValueError):
            t = 0.0
        return {"state": rec["state"], "t": t,
                "from": rec.get("from") if parse_version(str(rec.get("from") or "")) else None,
                "to": rec.get("to") if parse_version(str(rec.get("to") or "")) else None,
                "reason": reason if isinstance(reason, str) and reason.startswith("update.renew.") else None}

    @staticmethod
    def sidecar_hint(side_v, cur, renews: bool, renew: Optional[dict]) -> Optional[str]:
        """The one remark about renewing the sidecar by hand (None: nothing to do)."""
        if not side_v or not cur or side_v >= cur:
            return None
        if not renews:
            return "renew_once"            # from before self-renewal: renewed by hand once, then it renews itself
        if renew and renew.get("to") == ".".join(map(str, cur)):
            if renew["state"] in ("rolled_back", "failed"):
                return "renew_failed"
            if renew["state"] == "skipped":
                return "renew_manual"
        return None

    # ------------------------------------------------------------------ channel, hold, previous version
    def channel(self) -> str:
        ch = self.store.get_setting("update_channel", "stable")
        return ch if ch in CHANNELS else "stable"

    def held(self) -> Optional[str]:
        hold = self.store.get_setting("update_hold", None) or {}
        v = hold.get("version") if isinstance(hold, dict) else None
        return v if isinstance(v, str) and parse_version(v) else None

    def set_hold(self, version: Optional[str]) -> None:
        self.store.set_setting("update_hold", {"version": version, "t": time.time()} if version else None)

    def settings(self, data: dict) -> dict:
        """Channel and hold (administrators). Returns the new state."""
        if self.addon():
            raise ValueError("update.err.addon")
        if "channel" in data:
            if data["channel"] not in CHANNELS:
                raise ValueError("update.err.channel")
            self.store.set_setting("update_channel", data["channel"])
        if "hold" in data:
            self.set_hold(__version__ if data["hold"] is True else None)
        return self.state()

    def previous(self) -> Optional[dict]:
        """The version that ran before the current one and can be gone back to, or None.

        The updater names the image it keeps as "previous" in its heartbeat (since 1.34.0); before that the backup it
        made for the update to the running version says where it came from."""
        cur = parse_version(__version__)
        if self.cfg.demo:
            return self._demo_previous()
        side = self.sidecar() or {}
        named = side.get("previous")
        found = None
        try:
            entries = sorted((p for p in (Path(self.cfg.data_dir) / "backups").iterdir() if p.is_dir() and not p.is_symlink()),
                             key=lambda p: p.name, reverse=True)
        except OSError:
            entries = []
        for d in entries[:20]:
            try:
                info = json.loads((d / "backup.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            frm, to = str(info.get("from") or ""), str(info.get("to") or "")
            if to == __version__ and parse_version(frm) and cur and parse_version(frm) < cur:
                found = {"version": frm, "when": info.get("t")}
                break
        if isinstance(named, str) and parse_version(named) and cur and parse_version(named) < cur:
            if found and found["version"] == named:
                return found
            return {"version": named, "when": None}
        return found

    def _demo_previous(self) -> Optional[dict]:
        from . import whatsnew
        older = [e["version"] for e in whatsnew.entries() if is_older(e["version"], __version__)]
        return {"version": older[0], "when": time.time() - 9 * 86400} if older else None

    @staticmethod
    def _demo_renewal(prev: Optional[dict]) -> Optional[dict]:
        """The sample instance shows an updater that renewed itself with the last update."""
        if not prev:
            return None
        return {"state": "done", "t": prev.get("when") or time.time() - 9 * 86400, "from": prev["version"],
                "to": __version__, "reason": None}

    def state(self) -> dict:
        if self.addon():
            # the add-on system shows and installs new versions; nothing here asks the feed or touches the sidecar
            return {"current": __version__, "mode": "addon", "enabled": False, "latest": None, "available": False,
                    "notes": "", "url": None, "checked": None, "error": None, "sidecar": False, "status": {"state": "idle"},
                    "sidecar_version": None, "sidecar_old": False, "channel": "stable", "stable": None, "preview": None,
                    "preview_none": False, "held": None, "held_newer": None, "previous": None, "rollback_ok": False,
                    "rollback_unsigned": False, "signature": {"supported": False, "enforced": False},
                    "sidecar_renews": False, "renew": None, "sidecar_hint": None,
                    "image": self.cfg.image_repo, "demo": self.cfg.demo}
        cache = self.cached()
        side = self.sidecar()
        demo = bool(self.cfg.demo)
        if demo and side is None:   # the sample instance shows every part of the page: an updater that checks signatures
            side = {"version": __version__, "features": ["signature", "previous", "renew"]}
        side_v = parse_version(str((side or {}).get("version") or ""))
        cur = parse_version(__version__)
        stable = cache.get("latest")
        preview = cache.get("preview") if isinstance(cache.get("preview"), dict) else None
        if preview and not (parse_version(str(preview.get("version") or "")) and
                            (not parse_version(str(stable or "")) or parse_version(preview["version"]) > parse_version(stable))):
            preview = None
        channel = self.channel()
        offer = {"version": stable, "notes": cache.get("notes"), "url": cache.get("url")}
        if channel == "preview" and preview:
            offer = preview
        latest = offer.get("version")
        newer = bool(latest and parse_version(latest) and cur and parse_version(latest) > cur)
        held = self.held()
        prev = self.previous()
        # the updater checks the signature of an official rollback target too; images before FIRST_SIGNED have none
        unsigned_prev = bool(prev and self.cfg.image_repo == OFFICIAL_REPO and is_older(prev["version"], FIRST_SIGNED))
        features = (side or {}).get("features") if isinstance((side or {}).get("features"), list) else []
        renews = "renew" in features
        renew = self._demo_renewal(prev) if demo else self.renewal()
        if renew and renew["state"] == "renewing" and time.time() - renew["t"] > RENEWING_FOR:
            renew = None
        return {"current": __version__, "enabled": self.enabled(), "latest": latest, "available": newer and not held,
                "notes": offer.get("notes") if newer else "", "url": offer.get("url"), "checked": cache.get("checked"),
                "error": cache.get("error"), "sidecar": side is not None, "status": {"state": "idle"} if demo else self.status(),
                "sidecar_version": (side or {}).get("version"),
                # an updater sidecar from before UPDATER_KEEPS_TAG left "latest" on an old image after a second update;
                # one "compose pull" + "up" renews it, after that it keeps the tag itself
                "sidecar_old": bool(side_v and side_v < parse_version(UPDATER_KEEPS_TAG)),
                "channel": channel, "stable": stable, "preview": preview, "preview_none": channel == "preview" and not preview,
                "held": held, "held_newer": latest if (held and newer) else None,
                "previous": prev, "rollback_ok": bool(prev and side is not None and not unsigned_prev),
                "rollback_unsigned": unsigned_prev,
                "signature": {"supported": "signature" in features, "enforced": self.cfg.image_repo == OFFICIAL_REPO},
                "sidecar_renews": bool(side is not None and renews), "renew": renew,
                "sidecar_hint": self.sidecar_hint(side_v, cur, renews, renew) if side is not None else None,
                "image": self.cfg.image_repo, "demo": self.cfg.demo, "mode": "container"}

    def busy(self) -> bool:
        """True while a request waits for the sidecar or a run is still going."""
        if (self.dir / "request.json").exists():
            return True
        st = self.status()
        try:
            age = time.time() - float(st.get("t") or 0)
        except (TypeError, ValueError):
            age = BUSY_FOR
        return st.get("state") not in RUN_TERMINAL and age < BUSY_FOR

    def rollback(self, version: str) -> dict:
        """Back to the previous version: same checks as an update; the version it goes back to is held."""
        if self.addon():
            raise ValueError("update.err.addon")
        prev = self.previous()
        if not prev:
            raise ValueError("update.err.no_previous")
        if str(version or "").lstrip("v") != prev["version"]:
            raise ValueError("update.err.rollback_mismatch")
        if self.cfg.image_repo == OFFICIAL_REPO and is_older(prev["version"], FIRST_SIGNED):
            raise ValueError("update.err.rollback_unsigned")
        payload = self.request(prev["version"], rollback=True)
        self.set_hold(prev["version"])
        return payload

    def request(self, version: str, rollback: bool = False) -> dict:
        if self.addon():
            raise ValueError("update.err.addon")
        if not parse_version(version):
            raise ValueError("update.err.invalid_version")
        if not rollback and self.held():
            raise ValueError("update.err.held")
        if not self.sidecar():
            raise ValueError("update.err.no_sidecar")
        if self.busy():
            raise ValueError("update.err.busy")
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {"version": version.lstrip("v"), "nonce": secrets.token_hex(16), "requested": time.time()}
        if rollback:
            payload["rollback"] = True
        # a unique temp file per request: two requests at once must not share (and steal) one file
        tmp = self.dir / (".request.%s.tmp" % secrets.token_hex(6))
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            target = self.dir / "request.json"
            try:
                os.link(tmp, target)     # atomic "create if absent": only one waiting request
            except FileExistsError:
                raise ValueError("update.err.busy") from None
            except OSError:              # file systems without hard links
                os.replace(tmp, target)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return payload
