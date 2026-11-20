"""Self-renewal of the updater sidecar (updater/engine.py renew_after_update + updater/renew.py) against the fake
container runtime of test_updater_engine.py.

After a healthy app update to a version newer than the sidecar, the sidecar starts a helper from the checked image;
the helper re-creates the sidecar with the same configuration, waits for its heartbeat and goes back to the old one
when none comes. Never two sidecars at once, never none after a failure; own images and switched-off installations
keep the manual way."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from updater import engine as E  # noqa: E402
from updater import sigverify as S  # noqa: E402
from updater.renew import Renewer  # noqa: E402
from updater.runtimeapi import RuntimeAPIError  # noqa: E402
from test_updater_engine import NONCE, REPO, FakeRuntime, fast_settings, make_db, marker  # noqa: E402

OFFICIAL = S.OFFICIAL_REPO
DIGEST = "sha256:" + "d" * 64
SIDE = "lanloom-updater"
GROUP = "998"


class RenewRuntime(FakeRuntime):
    """The fake runtime plus what a renewal needs: restart-policy updates, untagging, a sidecar process that writes
    its heartbeat when started, a helper that runs (in-process) when started and removes itself when done."""

    def __init__(self, data_dir: Path):
        super().__init__(data_dir)
        self.untagged: list[str] = []
        self.update_error = False
        self.create_error_suffix: str | None = None
        self.sidecar_behaviour: dict[str, str] = {}   # version -> "ok" | "silent" | "dies" | "wrong_version"
        self.on_helper_start = None
        self.on_stop = None
        self.events: list[tuple[str, str]] = []       # ("up"|"down", container id) of sidecar processes

    def add_sidecar(self, version: str, repo: str = OFFICIAL, policy: str = "unless-stopped", ref_tag="latest",
                    name: str = SIDE) -> str:
        iid = self.tags[f"{repo}:{version}"]
        self.tags.setdefault(f"{repo}:{ref_tag}", iid)
        body = {"Image": f"{repo}:{ref_tag}", "Cmd": ["python", "-m", "updater"], "User": "0:0",
                "Env": ["LANLOOM_TARGET=lanloom"],
                "Labels": {"com.docker.compose.project": "lanloom", "com.docker.compose.service": "updater",
                           "com.docker.compose.config-hash": "abc123", "com.docker.compose.image": iid},
                "HostConfig": {"NetworkMode": "host", "RestartPolicy": {"Name": policy}, "ReadonlyRootfs": True,
                               "SecurityOpt": ["no-new-privileges:true"]}}
        mounts = [{"Type": "volume", "Name": "lanloom_lanloom-data", "Source": "/var/lib/vol/_data",
                   "Destination": "/data", "RW": True},
                  {"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock",
                   "RW": True}]
        cid = FakeRuntime.container_create(self, name, body, _mounts=mounts)
        self.ctrs[cid]["State"].update(Status="running", Running=True)
        self.calls.clear()
        return cid

    @staticmethod
    def _is_sidecar(c: dict) -> bool:
        return E.Updater.is_sidecar(c)

    def container_create(self, name, body, _mounts=None):
        if self.create_error_suffix and name and name.endswith(self.create_error_suffix):
            raise RuntimeAPIError(500, "cannot create")
        if _mounts is None and not (body.get("HostConfig") or {}).get("Binds"):
            # a clone (build_create_body) keeps the HostConfig; the fake takes the mounts of the original
            for c in self.ctrs.values():
                if (c["HostConfig"] is body.get("HostConfig")) or c["HostConfig"] == body.get("HostConfig"):
                    _mounts = c["Mounts"]
                    break
        cid = super().container_create(name, body, _mounts)
        self.ctrs[cid]["_body"] = body
        return cid

    def container_update(self, ref, body):
        c = self._resolve(ref)
        self.calls.append(("update", c["Name"], json.dumps(body, sort_keys=True)))
        if self.update_error:
            raise RuntimeAPIError(500, "update not supported")
        c["HostConfig"] = dict(c["HostConfig"], **body)

    def image_untag(self, ref):
        self.untagged.append(ref)
        self.tags.pop(ref, None)

    def container_start(self, ref):
        c = self._resolve(ref)
        labels = c["Config"]["Labels"]
        if labels.get(E.ROLE_LABEL) == E.ROLE_RENEW:
            self.calls.append(("start", c["Name"]))
            c["State"].update(Status="running", Running=True)
            if self.on_helper_start:
                self.on_helper_start(c)
                self.ctrs.pop(c["Id"], None)      # AutoRemove
            return
        if self._is_sidecar(c):
            self.calls.append(("start", c["Name"]))
            version = self.version_of(c)
            beh = self.sidecar_behaviour.get(version, "ok")
            if beh == "dies":
                c["State"].update(Status="exited", Running=False, ExitCode=1)
                return
            c["State"].update(Status="running", Running=True)
            self.events.append(("up", c["Id"]))
            if beh in ("ok", "wrong_version"):
                now = time.time()
                hb = {"version": version if beh == "ok" else "0.0.1", "t": now, "started": now,
                      "features": list(E.FEATURES), "previous": None}
                (self.data_dir / "update").mkdir(exist_ok=True)
                (self.data_dir / "update" / "sidecar.json").write_text(json.dumps(hb))
            return
        super().container_start(ref)

    def container_stop(self, ref, t=20):
        c = self._resolve(ref)
        was = c["State"]["Running"]
        super().container_stop(ref, t)
        if was and self._is_sidecar(c):
            self.events.append(("down", c["Id"]))
        if self.on_stop:
            self.on_stop(c)

    def running_sidecars(self) -> list[dict]:
        return [c for c in self.ctrs.values() if self._is_sidecar(c) and c["State"]["Running"]]


def verified(repo, tag):
    return S.Verified(digest=DIGEST, identity=S.IDENTITY, issuer=S.ISSUER, log_index=7, integrated_time=1)


def mountinfo(path: Path, cid: str) -> str:
    path.write_text(f"5183 5172 259:2 /var/lib/docker/containers/{cid}/hostname /etc/hostname rw - ext4 /dev/x rw\n"
                    f"5184 5172 259:2 /var/lib/docker/containers/{cid}/hosts /etc/hosts rw - ext4 /dev/x rw\n")
    return str(path)


class Env:
    def __init__(self, tmp_path: Path, monkeypatch, repo: str = OFFICIAL, side_version: str = "1.0.0",
                 policy: str = "unless-stopped", **settings):
        self.tmp = tmp_path
        self.data = tmp_path / "data"
        self.data.mkdir()
        make_db(self.data / "lanloom.db")
        self.repo = repo
        self.fake = fake = RenewRuntime(self.data)
        fake.add_image("1.0.0", repo=repo)
        iid = fake.add_image("1.1.0", repo=repo)
        if repo == OFFICIAL:      # what a pull by digest leaves behind (the version tag comes from the updater)
            fake.tags[f"{repo}@{DIGEST}"] = iid
            del fake.tags[f"{repo}:1.1.0"]
        self.app = fake.add_app("1.0.0", repo=repo, labels_extra={"com.docker.compose.project": "lanloom"})
        self.side = fake.add_sidecar("1.0.0", repo=repo, policy=policy)
        self.new_iid = iid
        monkeypatch.setattr(E, "sidecar_version", lambda: side_version)
        s = fast_settings(self.data, official_repo=OFFICIAL, renew_timeout=1.0, renew_wait=2.0,
                          mountinfo_path=mountinfo(tmp_path / "mountinfo-side", self.side))
        for k, v in settings.items():
            setattr(s, k, v)
        self.upd = E.Updater(fake, s, health_fn=fake.health, echo=lambda line: None, verify_fn=verified)
        self.upd.helper_identity = lambda: ("10001:10001", [GROUP])
        self.helper_bodies: list[dict] = []
        self.helper_codes: list[int] = []
        self.helper_log: list[str] = []
        fake.on_helper_start = self.run_helper
        fake.on_stop = lambda c: self.upd.stop() if c["Id"] == self.side else None

    def run_helper(self, c: dict) -> None:
        self.helper_bodies.append(c["_body"])
        cmd = c["Config"]["Cmd"]
        hs = fast_settings(self.data, renew_timeout=0.5, mountinfo_path=mountinfo(self.tmp / "mountinfo-helper", c["Id"]))
        r = Renewer(self.fake, hs, echo=self.helper_log.append)
        self.helper_codes.append(r.run(cmd[cmd.index("--renew") + 1], cmd[cmd.index("--renew-version") + 1]))

    def request(self, version="1.1.0", **extra) -> dict:
        self.upd.ensure_update_dir()
        self.upd.request_path.write_text(json.dumps({"version": version, "nonce": NONCE, "requested": time.time(), **extra}))
        assert self.upd.poll_once()
        return json.loads(self.upd.status_path.read_text())

    def renew(self) -> dict | None:
        p = self.data / "update" / "renew.json"
        return json.loads(p.read_text()) if p.exists() else None

    def sidecar(self) -> dict:
        return self.fake.by_name(SIDE)

    def leftovers(self) -> list[str]:
        return [c["Name"] for c in self.fake.ctrs.values()
                if c["Name"].endswith((E.RENEW_OLD_SUFFIX, E.RENEW_NEW_SUFFIX))
                or c["Config"]["Labels"].get(E.ROLE_LABEL) in (E.ROLE_RENEW, E.ROLE_PROBE)]


def never_two_sidecars(events: list[tuple[str, str]]) -> bool:
    up = set()
    for kind, cid in events:
        if kind == "up":
            up.add(cid)
            if len(up) > 1:
                return False
        else:
            up.discard(cid)
    return True


# ---------------------------------------------------------------------- renewal
def test_sidecar_renews_itself_after_a_healthy_update(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    old_side = env.fake.container_inspect(env.side)
    st = env.request()
    assert st["state"] == "done", st["log"]
    # the app update itself is unchanged: 1.1.0 runs, pulled once by the checked digest
    app = env.fake.by_name("lanloom")
    assert env.fake.version_of(app) == "1.1.0" and app["State"]["Running"]
    assert env.fake.pulls == [(OFFICIAL, DIGEST)]                  # no second pull for the sidecar
    # the sidecar runs from the very image the app update checked
    side = env.sidecar()
    assert side["Id"] != env.side and side["State"]["Running"]
    assert side["Image"] == env.new_iid
    assert side["Config"]["Image"] == f"{OFFICIAL}:latest"         # names the tag of the compose file
    assert env.fake.tags[f"{OFFICIAL}:latest"] == env.new_iid
    # same configuration: command, user, environment, network, restart policy, mounts
    assert side["Config"]["Cmd"] == ["python", "-m", "updater"] and side["Config"]["User"] == "0:0"
    assert "LANLOOM_TARGET=lanloom" in side["Config"]["Env"]
    hc = side["HostConfig"]
    assert hc["NetworkMode"] == "host" and hc["RestartPolicy"] == {"Name": "unless-stopped"}
    assert hc["ReadonlyRootfs"] is True and hc["SecurityOpt"] == ["no-new-privileges:true"]
    assert side["Mounts"] == old_side["Mounts"]
    # compose labels stay, so a later "compose up" finds this container and creates nothing twice
    labels = side["Config"]["Labels"]
    for k in ("com.docker.compose.project", "com.docker.compose.service", "com.docker.compose.config-hash"):
        assert labels[k] == old_side["Config"]["Labels"][k]
    assert labels["com.docker.compose.image"] == env.new_iid
    # the old sidecar and every helper are gone, the old image is no longer marked
    assert env.side not in env.fake.ctrs and env.leftovers() == []
    assert f"{OFFICIAL}:{E.SIDECAR_PREVIOUS_TAG}" in env.fake.untagged
    assert f"{OFFICIAL}:{E.SIDECAR_PREVIOUS_TAG}" not in env.fake.tags
    rec = env.renew()
    assert rec["state"] == "done" and rec["from"] == "1.0.0" and rec["to"] == "1.1.0"
    assert env.helper_codes == [0]
    # one sidecar at a time: the old one stopped before the new one started
    assert never_two_sidecars(env.fake.events)
    order = [c for c in env.fake.calls if c[0] in ("stop", "start") and c[1] == "/" + SIDE]
    assert order == [("stop", "/" + SIDE), ("start", "/" + SIDE)]     # the new one starts under the old name
    # the restart policy of the parked sidecar was switched off while it waited
    assert ("update", "/" + SIDE, json.dumps({"RestartPolicy": {"Name": "no"}}, sort_keys=True)) in env.fake.calls
    # the status of the app update stays "done"
    assert json.loads(env.upd.status_path.read_text())["state"] == "done"


def test_helper_runs_unprivileged_from_the_checked_image(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.request()
    (body,) = env.helper_bodies
    assert body["Image"] == env.new_iid                            # the checked image, by id
    assert body["Cmd"][:4] == ["python", "-m", "updater", "--renew"] and body["Cmd"][-1] == "1.1.0"
    assert body["User"] == "10001:10001"                           # the data owner, never root
    hc = body["HostConfig"]
    assert hc["NetworkMode"] == "none" and hc["ReadonlyRootfs"] is True and hc["CapDrop"] == ["ALL"]
    assert hc["SecurityOpt"] == ["no-new-privileges:true"] and hc["GroupAdd"] == [GROUP]
    assert hc["AutoRemove"] is True and hc["RestartPolicy"] == {"Name": "no"}
    assert sorted(hc["Binds"]) == ["/var/run/docker.sock:/var/run/docker.sock", "lanloom_lanloom-data:/data"]
    assert "CapAdd" not in hc and "Privileged" not in hc
    assert body["Labels"] == {E.ROLE_LABEL: E.ROLE_RENEW, E.PARENT_LABEL: SIDE}


@pytest.mark.parametrize("behaviour", ["silent", "dies", "wrong_version"])
def test_no_heartbeat_brings_the_old_sidecar_back(tmp_path, monkeypatch, behaviour):
    env = Env(tmp_path, monkeypatch)
    env.fake.sidecar_behaviour["1.1.0"] = behaviour
    st = env.request()
    assert st["state"] == "done"                                   # the app update is not touched
    assert env.fake.version_of(env.fake.by_name("lanloom")) == "1.1.0"
    side = env.sidecar()
    assert side["Id"] == env.side and side["State"]["Running"]     # the old sidecar runs again under its name
    assert side["HostConfig"]["RestartPolicy"] == {"Name": "unless-stopped"}
    assert env.fake.version_of(side) == "1.0.0"
    assert env.leftovers() == [] and len(env.fake.running_sidecars()) == 1
    rec = env.renew()
    assert rec["state"] == "rolled_back" and rec["reason"] == "update.renew.err.no_heartbeat"
    assert rec["from"] == "1.0.0" and rec["to"] == "1.1.0"
    assert never_two_sidecars(env.fake.events)
    assert f"{OFFICIAL}:{E.SIDECAR_PREVIOUS_TAG}" not in env.fake.tags
    assert env.helper_codes == [1]


def test_create_failure_leaves_the_old_sidecar_untouched(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.fake.create_error_suffix = E.RENEW_NEW_SUFFIX
    assert env.request()["state"] == "done"
    side = env.sidecar()
    assert side["Id"] == env.side and side["State"]["Running"]
    assert not any(c[0] == "stop" and SIDE in c[1] for c in env.fake.calls)
    rec = env.renew()
    assert rec["state"] == "failed" and rec["reason"] == "update.renew.err.create"
    assert env.leftovers() == [] and f"{OFFICIAL}:{E.SIDECAR_PREVIOUS_TAG}" not in env.fake.tags
    # the old sidecar did not stop and carries on with the next request
    assert not env.upd._stop.is_set()


def test_restart_policy_always_needs_the_update_call(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, policy="always")
    env.fake.update_error = True
    assert env.request()["state"] == "done"
    side = env.sidecar()
    assert side["Id"] == env.side and side["State"]["Running"]     # never parked: it could come back as a second one
    rec = env.renew()
    assert rec["state"] == "rolled_back" and rec["reason"] == "update.renew.err.switch"
    assert env.leftovers() == []


def test_unless_stopped_works_without_the_update_call(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.fake.update_error = True
    assert env.request()["state"] == "done"
    assert env.renew()["state"] == "done" and env.fake.version_of(env.sidecar()) == "1.1.0"


def test_helper_that_hangs_is_removed_and_the_sidecar_carries_on(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, renew_wait=0.2)
    env.fake.on_helper_start = None
    real_start = env.fake.container_start

    def start(ref):      # the helper starts and then never finishes
        c = env.fake._resolve(ref)
        if c["Config"]["Labels"].get(E.ROLE_LABEL) == E.ROLE_RENEW:
            c["State"].update(Status="running", Running=True)
            return
        real_start(ref)

    monkeypatch.setattr(env.fake, "container_start", start)
    assert env.request()["state"] == "done"
    assert env.renew()["state"] == "failed" and env.renew()["reason"] == "update.renew.err.timeout"
    assert env.sidecar()["Id"] == env.side and env.leftovers() == []
    assert not env.upd._stop.is_set()


# ---------------------------------------------------------------------- not renewed
def test_own_image_is_not_renewed(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, repo=REPO, allow_local=False)
    st = env.request()
    assert st["state"] == "done", st["log"]
    assert env.sidecar()["Id"] == env.side and env.helper_bodies == []
    rec = env.renew()
    assert rec["state"] == "skipped" and rec["reason"] == "update.renew.skip.unofficial"


@pytest.mark.parametrize("case,reason", [("off", "off"), ("no_self", "self_unknown"), ("socket", "socket"),
                                         ("repo", "repo")])
def test_skips_keep_the_manual_way(tmp_path, monkeypatch, case, reason):
    env = Env(tmp_path, monkeypatch)
    if case == "off":
        env.upd.s.renew = False
    elif case == "no_self":
        (tmp_path / "mountinfo-side").write_text("1 2 0:1 / / rw - overlay overlay rw\n")
    elif case == "socket":
        env.upd.helper_identity = lambda: None
    elif case == "repo":
        env.fake.ctrs[env.side]["Config"]["Image"] = "registry.example/someone/else:latest"
    assert env.request()["state"] == "done"
    assert env.sidecar()["Id"] == env.side and env.helper_bodies == []
    rec = env.renew()
    assert rec == {"state": "skipped", "t": rec["t"], "from": "1.0.0", "to": "1.1.0",
                   "reason": f"update.renew.skip.{reason}"}
    assert f"update.renew.skip.{reason}" in E.RENEW_KEYS


def test_switch_from_the_environment():
    assert E.Settings.from_env({}).renew is True
    assert E.Settings.from_env({"LANLOOM_UPDATER_RENEW": "0"}).renew is False
    assert E.Settings.from_env({"LANLOOM_UPDATER_RENEW": "1"}).renew is True


def test_current_sidecar_and_failed_or_backward_runs_do_nothing(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, side_version="1.1.0")
    assert env.request()["state"] == "done"
    assert env.renew() is None and env.helper_bodies == []


def test_failed_update_never_renews(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.fake.behaviours["1.1.0"] = {"app": "unhealthy"}
    assert env.request()["state"] == "rolled_back"
    assert env.renew() is None and env.helper_bodies == [] and env.sidecar()["Id"] == env.side


def test_going_back_never_renews(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    assert env.request()["state"] == "done"
    # a rollback request to an older version never renews (and a newer sidecar is fine)
    env.upd._stop.clear()
    env.helper_bodies.clear()
    env.upd.handle_request({"version": "1.0.0", "nonce": NONCE, "requested": time.time(), "rollback": True})
    assert env.helper_bodies == []


# ---------------------------------------------------------------------- helper checks
def test_helper_refuses_a_container_that_is_not_its_sidecar(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    hid = env.fake.container_create("x-renew", {"Image": env.new_iid, "Cmd": ["python", "-m", "updater", "--renew", env.app],
                                                "Labels": {E.ROLE_LABEL: E.ROLE_RENEW, E.PARENT_LABEL: SIDE},
                                                "HostConfig": {"NetworkMode": "none"}})
    hs = fast_settings(env.data, mountinfo_path=mountinfo(tmp_path / "mi", hid))
    r = Renewer(env.fake, hs, echo=lambda line: None)
    assert r.run(env.app, "1.1.0") == 1                           # the app container is not a sidecar
    assert env.renew()["reason"] == "update.renew.err.invalid"
    assert r.run("not-an-id", "1.1.0") == 2
    assert r.run(env.side, "1.1") == 2
    assert env.fake.by_name("lanloom")["Id"] == env.app and env.sidecar()["Id"] == env.side
    assert env.fake.ctrs[env.side]["State"]["Running"]


def test_helper_refuses_when_its_image_lacks_the_version(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    hid = env.fake.container_create("x-renew", {"Image": f"{OFFICIAL}:1.0.0",
                                                "Cmd": ["python", "-m", "updater", "--renew", env.side],
                                                "Labels": {E.ROLE_LABEL: E.ROLE_RENEW, E.PARENT_LABEL: SIDE},
                                                "HostConfig": {"NetworkMode": "none"}})
    hs = fast_settings(env.data, mountinfo_path=mountinfo(tmp_path / "mi", hid))
    assert Renewer(env.fake, hs, echo=lambda line: None).run(env.side, "1.1.0") == 1
    assert env.sidecar()["Id"] == env.side and env.fake.ctrs[env.side]["State"]["Running"]


def test_identification_by_mount_table_and_not_by_hostname(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    assert env.upd.find_self()["Id"] == env.side
    assert env.upd.find_self(helper=True) is None                 # the sidecar is no helper
    (tmp_path / "mountinfo-side").write_text(
        f"9 1 0:1 /var/lib/containers/storage/overlay-containers/{env.side}/userdata/hostname /etc/hostname rw\n")
    assert env.upd.find_self()["Id"] == env.side                   # the other runtime's layout
    (tmp_path / "mountinfo-side").write_text(
        f"9 1 0:1 /var/lib/docker/containers/{env.app}/hostname /etc/hostname rw\n")
    assert env.upd.find_self() is None                             # the app container is not a sidecar
    rep = env.upd.check()
    assert rep["self_renew"] == {"enabled": True, "own_container": False, "helper_socket": True}


def test_recover_cleans_up_after_an_interrupted_renewal(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, side_version="1.1.0")
    # a parked old sidecar and a stopped "next" one; the helper is gone
    parked = env.fake.add_sidecar("1.0.0", name=SIDE + E.RENEW_OLD_SUFFIX)
    env.fake.ctrs[parked]["State"].update(Status="exited", Running=False)
    env.fake.tags[f"{OFFICIAL}:{E.SIDECAR_PREVIOUS_TAG}"] = env.fake.tags[f"{OFFICIAL}:1.0.0"]
    env.upd.write_renew("renewing", **{"from": "1.0.0", "to": "1.1.0"})
    env.upd.recover_renewal()
    assert parked not in env.fake.ctrs and env.sidecar()["Id"] == env.side
    assert env.renew()["state"] == "done"                          # this sidecar is the new one
    assert f"{OFFICIAL}:{E.SIDECAR_PREVIOUS_TAG}" not in env.fake.tags


def test_recover_leaves_a_working_helper_alone(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    parked = env.fake.add_sidecar("1.0.0", name=SIDE + E.RENEW_OLD_SUFFIX)
    env.fake.ctrs[parked]["State"].update(Status="exited", Running=False)
    hid = env.fake.container_create("h", {"Image": env.new_iid, "Cmd": ["python", "-m", "updater", "--renew", env.side],
                                          "Labels": {E.ROLE_LABEL: E.ROLE_RENEW, E.PARENT_LABEL: SIDE},
                                          "HostConfig": {"NetworkMode": "none"}})
    env.fake.ctrs[hid]["State"].update(Status="running", Running=True)
    env.upd.write_renew("renewing", **{"from": "1.0.0", "to": "1.1.0"})
    env.upd.recover_renewal()
    assert parked in env.fake.ctrs and hid in env.fake.ctrs and env.renew()["state"] == "renewing"


def test_recover_marks_an_interrupted_renewal_of_the_old_sidecar_as_failed(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)            # this sidecar is still 1.0.0: the renewal to 1.1.0 did not finish
    nxt = env.fake.add_sidecar("1.0.0", name=SIDE + E.RENEW_NEW_SUFFIX)
    env.fake.ctrs[nxt]["State"].update(Status="created", Running=False)
    env.upd.write_renew("renewing", **{"from": "1.0.0", "to": "1.1.0"})
    env.upd.recover_renewal()
    assert nxt not in env.fake.ctrs
    rec = env.renew()
    assert rec["state"] == "failed" and rec["reason"] == "update.renew.err.interrupted"


# ---------------------------------------------------------------------- helper identity (real socket)
def test_helper_identity_from_a_real_socket(tmp_path, monkeypatch):
    data = tmp_path / "d"
    data.mkdir()
    sock_path = Path("/tmp") / f"ll-renew-{os.getpid()}.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(sock_path))
        upd = E.Updater(FakeRuntime(data), fast_settings(data, socket_path=str(sock_path)), echo=lambda line: None)
        uid, gid = os.getuid(), os.getgid()
        os.chmod(sock_path, 0o600)                 # owned by the data owner itself
        assert upd.helper_identity() == (f"{uid}:{gid}", [])
        real_stat = os.stat

        class St:
            def __init__(self, mode, owner, group):
                self.st_mode, self.st_uid, self.st_gid = mode, owner, group

        other = 1 if uid == 0 else 0               # a socket owner that is not the data owner (the test runner may be root)
        monkeypatch.setattr(E.os, "stat", lambda p, *a, **k: St(0o140660, other, 991) if str(p) == str(sock_path) else real_stat(p, *a, **k))
        assert upd.helper_identity() == (f"{uid}:{gid}", ["991"])   # through the socket group
        monkeypatch.setattr(E.os, "stat", lambda p, *a, **k: St(0o140600, other, other) if str(p) == str(sock_path) else real_stat(p, *a, **k))
        assert upd.helper_identity() is None                         # root only: keep the manual way
        monkeypatch.setattr(E.os, "stat", real_stat)
        upd.s.socket_path = str(data / "missing.sock")
        assert upd.helper_identity() is None
    finally:
        srv.close()
        sock_path.unlink(missing_ok=True)


def test_renewal_keys_are_translated():
    root = Path(__file__).resolve().parent.parent / "lanloom" / "i18n"
    for lang in ("en", "de", "fr", "es", "nl", "it"):
        texts = json.loads((root / f"{lang}.json").read_text(encoding="utf-8"))
        for key in E.RENEW_KEYS:
            assert texts.get(key), (lang, key)


def test_heartbeat_names_renew_and_the_data_stays(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.request()
    assert "renew" in E.FEATURES
    assert marker(env.data / "lanloom.db") == "keep"
