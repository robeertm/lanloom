"""Renewal helper: replaces the updater sidecar with one on the image it has just checked.

Started by the sidecar after a healthy app update (`python -m updater --renew <sidecar id> --renew-version X.Y.Z`),
from that very image, without network, read-only, without root and without capabilities. It reaches the container
runtime through the socket group and writes its result into <data>/update/renew.json as the owner of the data.

Order (only one sidecar runs at any time, and a failure always ends with the old sidecar running again):

  1. check: the old container is the sidecar that started this helper, the helper's own image carries the version
  2. mark the old image (tag "updater-previous") so nothing prunes it while the old container is parked
  3. create the new sidecar under "<name>-next" with the old configuration (name, mounts, socket, network, restart
     policy, user, labels incl. the compose labels; the compose image label names the new image)
  4. park the old one: restart policy off, stop, rename to "<name>-renewing"; give the new one the name and start it
  5. wait for the heartbeat of the new sidecar (version X.Y.Z, started after the switch), then remove the parked one
  6. no heartbeat: remove the new one, give the old one its name and restart policy back and start it
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .engine import (COMPOSE_IMAGE_LABEL, HEXID_RE, PARENT_LABEL, RENEW_NEW_SUFFIX, RENEW_OLD_SUFFIX,
                     SIDECAR_PREVIOUS_TAG, VERSION_LABEL, Settings, Updater, parse_semver, read_json_file, split_ref,
                     UpdateError)
from .runtimeapi import NotFound, RuntimeAPI, RuntimeAPIError

CLOCK_SKEW_S = 5.0     # the heartbeat must come from a process started after the switch (same host clock)


class RenewFailed(Exception):
    def __init__(self, why: str, detail: str = ""):
        super().__init__(detail or why)
        self.why = why
        self.detail = detail


class Renewer:
    def __init__(self, api: RuntimeAPI, settings: Settings, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, echo: Callable[[str], None] | None = None):
        self.api = api
        self.s = settings
        self.clock = clock
        self.sleep = sleep
        self.echo = echo if echo is not None else (lambda line: print(line, flush=True))
        self.u = Updater(api, settings, clock=clock, sleep=sleep, echo=self.echo)
        self.base: dict[str, Any] = {}

    # ------------------------------------------------------------------ helpers
    def say(self, line: str) -> None:
        self.echo(f"renew: {line}")

    def write(self, state: str, **fields: Any) -> None:
        self.u.write_renew(state, **dict(self.base, **fields))

    def image_version(self, ref: str) -> str | None:
        img = self.api.image_inspect(ref) or {}
        label = ((img.get("Config") or {}).get("Labels") or {}).get(VERSION_LABEL)
        return label if parse_semver(label) else None

    def heartbeat(self) -> dict:
        try:
            hb = read_json_file(self.u.heartbeat_path, 64 * 1024)
        except UpdateError:
            return {}
        return hb if isinstance(hb, dict) else {}

    def image_ref(self, repo: str, tag: str | None, new_id: str, version: str) -> str:
        """The reference the new sidecar names: the old moving tag when it already points at the new image (as
        the compose file names it), else the version tag of the new image, else its id."""
        if tag and not parse_semver(tag) and (self.api.image_inspect(f"{repo}:{tag}") or {}).get("Id") == new_id:
            return f"{repo}:{tag}"
        if (self.api.image_inspect(f"{repo}:{version}") or {}).get("Id") == new_id:
            return f"{repo}:{version}"
        return new_id

    def wait_sidecar(self, cid: str, version: str | None, since: float) -> tuple[bool, str]:
        """True when the container runs (no restarts) and its heartbeat names `version` from a process started after
        `since` (older sidecars send no start time: then a heartbeat written after `since` counts)."""
        deadline = self.clock() + self.s.renew_timeout
        seen = None
        reason = "no heartbeat"
        while self.clock() < deadline:
            try:
                info = self.api.container_inspect(cid)
            except RuntimeAPIError as exc:
                return False, f"inspect failed: {exc.message}"
            st = info.get("State") or {}
            if int(info.get("RestartCount") or 0) > 0 or st.get("Restarting"):
                return False, "container restarted"
            if st.get("Status") in ("exited", "dead"):
                return False, f"container exited with code {st.get('ExitCode')}"
            hb = self.heartbeat()
            try:
                fresh = float(hb.get("started") if hb.get("started") is not None else hb.get("t") or 0) >= since - CLOCK_SKEW_S
            except (TypeError, ValueError):
                fresh = False
            if st.get("Running") and fresh and (version is None or hb.get("version") == version):
                if seen is None:
                    seen = self.clock()
                if self.clock() - seen >= self.s.settle_s:
                    return True, "heartbeat"
            else:
                seen = None
                if hb.get("version") and hb.get("version") != version and fresh:
                    reason = f"heartbeat names {hb.get('version')}"
            self.sleep(self.s.check_interval)
        return False, reason

    def quiet(self, fn: Callable[..., Any], *args: Any) -> bool:
        try:
            fn(*args)
            return True
        except RuntimeAPIError:
            return False

    # ---------------------------------------------------------------------- run
    def run(self, old_id: str, version: str) -> int:
        """Renew the sidecar `old_id` to the image of this helper. Exit code 0 = renewed (or nothing to do)."""
        self.base = {"to": version}
        if not HEXID_RE.match(old_id or "") or not parse_semver(version):
            self.write("failed", reason="update.renew.err.invalid")
            return 2
        try:
            self.api.negotiate()
            return self._run(old_id, version)
        except RenewFailed as exc:
            self.say(f"not renewed: {exc.why} {exc.detail}".strip())
            self.write("failed", reason=f"update.renew.err.{exc.why}")
            return 1
        except RuntimeAPIError as exc:
            self.say(f"not renewed: runtime {exc.message}")
            self.write("failed", reason="update.renew.err.runtime")
            return 1

    def _run(self, old_id: str, version: str) -> int:
        me = self.u.find_self(helper=True)
        if me is None:
            raise RenewFailed("invalid", "own container not found")
        new_id = me.get("Image") or ""
        if self.image_version(new_id) != version:
            raise RenewFailed("invalid", "the helper image does not carry the version")
        try:
            old = self.api.container_inspect(old_id)
        except NotFound:
            raise RenewFailed("invalid", "sidecar not found") from None
        name = (old.get("Name") or "").lstrip("/")
        parent = ((me.get("Config") or {}).get("Labels") or {}).get(PARENT_LABEL)
        if not self.u.is_sidecar(old) or not name or parent != name or not (old.get("Id") or "").startswith(old_id):
            raise RenewFailed("invalid", "not the sidecar that started this helper")
        repo, tag = split_ref((old.get("Config") or {}).get("Image") or "")
        old_version = self.image_version(old.get("Image") or "")
        self.base = {"from": old_version, "to": version}
        if old_version and parse_semver(old_version) >= parse_semver(version):    # type: ignore[operator]
            self.say(f"{name} already runs {old_version}")
            self.write("done")
            return 0
        self.write("renewing")
        self.say(f"renewing {name} {old_version or 'unknown'} -> {version}")

        # 2. keep the old image marked until the new sidecar has reported
        if not self.quiet(self.api.image_tag, old.get("Image") or "", repo, SIDECAR_PREVIOUS_TAG):
            raise RenewFailed("create", "cannot mark the old image")

        # 3. the new sidecar, same configuration on the new image
        body, extra = self.u.build_create_body(old, self.image_ref(repo, tag, new_id, version))
        labels = body.get("Labels") or {}
        if COMPOSE_IMAGE_LABEL in labels:
            labels[COMPOSE_IMAGE_LABEL] = new_id       # compose compares it on "up": it must name what runs now
        new_name = name + RENEW_NEW_SUFFIX
        try:
            stale = self.api.container_inspect(new_name)
            if not (stale.get("State") or {}).get("Running"):
                self.quiet(self.api.container_remove, stale["Id"], True)
        except NotFound:
            pass
        new_cid = None
        try:
            new_cid = self.api.container_create(new_name, body)
            for net_name, ep in extra:
                self.api.network_connect(net_name, new_cid, ep)
        except RuntimeAPIError as exc:
            if new_cid:
                self.quiet(self.api.container_remove, new_cid, True)
            self.quiet(self.api.image_untag, f"{repo}:{SIDECAR_PREVIOUS_TAG}")
            raise RenewFailed("create", exc.message) from None

        # 4. park the old sidecar, switch, start the new one
        policy = dict(((old.get("HostConfig") or {}).get("RestartPolicy") or {}))
        state = {"policy_changed": False, "stopped": False, "renamed": False}
        try:
            if policy.get("Name") not in (None, "", "no"):
                try:
                    self.api.container_update(old_id, {"RestartPolicy": {"Name": "no"}})
                    state["policy_changed"] = True
                except RuntimeAPIError:
                    # "always" would bring the parked sidecar back on the next start of the runtime: two sidecars
                    if policy.get("Name") == "always":
                        raise
            self.api.container_stop(old_id, self.s.stop_timeout)
            state["stopped"] = True
            self.api.container_rename(old_id, name + RENEW_OLD_SUFFIX)
            state["renamed"] = True
            self.api.container_rename(new_cid, name)
            since = self.clock()
            self.api.container_start(new_cid)
        except RuntimeAPIError as exc:
            return self.rollback(old, new_cid, name, repo, policy, state, "switch", exc.message)

        # 5. the new sidecar must report with the new version
        ok, why = self.wait_sidecar(new_cid, version, since)
        if not ok:
            try:
                for line in self.api.container_logs(new_cid, tail=6):
                    self.say(f"new: {str(line).strip()[:200]}")
            except RuntimeAPIError:
                pass
            return self.rollback(old, new_cid, name, repo, policy, state, "no_heartbeat", why)
        self.quiet(self.api.container_remove, old_id, True)
        self.quiet(self.api.image_untag, f"{repo}:{SIDECAR_PREVIOUS_TAG}")
        self.write("done")
        self.say(f"{name} runs {version}")
        return 0

    # ----------------------------------------------------------------- rollback
    def rollback(self, old: dict, new_cid: str | None, name: str, repo: str, policy: dict, state: dict,
                 why: str, detail: str) -> int:
        """Back to the old sidecar: the new container goes, the old one gets its name and restart policy back."""
        self.say(f"{detail}; going back to the previous updater")
        old_id = old["Id"]
        problems = []
        if new_cid:
            self.quiet(self.api.container_stop, new_cid, 10)
            if not self.quiet(self.api.container_remove, new_cid, True):
                problems.append("remove new")
        if state.get("renamed"):
            if not self.quiet(self.api.container_rename, old_id, name):
                problems.append("rename")
        if state.get("policy_changed") and not self.quiet(self.api.container_update, old_id, {"RestartPolicy": policy}):
            problems.append("restart policy")
        since = self.clock()
        try:
            running = bool((self.api.container_inspect(old_id).get("State") or {}).get("Running"))
        except RuntimeAPIError:
            running = False
        if not running and not self.quiet(self.api.container_start, old_id):
            problems.append("start")
        if state.get("stopped") or not running:
            ok, reason = self.wait_sidecar(old_id, self.base.get("from"), since)
        else:
            ok, reason = True, "never stopped"      # the old sidecar kept running all along
        self.quiet(self.api.image_untag, f"{repo}:{SIDECAR_PREVIOUS_TAG}")
        if ok and not problems:
            self.write("rolled_back", reason=f"update.renew.err.{why}")
            self.say("the previous updater runs again")
        else:
            self.write("failed", reason="update.renew.err.restore")
            self.say(f"previous updater not back: {', '.join(problems) or reason}")
        return 1
