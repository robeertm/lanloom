"""Unit tests of the update engine against an in-process fake container runtime."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from updater import engine as E  # noqa: E402
from updater.runtimeapi import Conflict, NotFound, RuntimeAPIError  # noqa: E402

REPO = "registry.example/acme/lanloom"
NONCE = "a1b2c3d4e5f60718"


# --------------------------------------------------------------------- fake
class FakeRuntime:
    """Enough of the container runtime API for the engine.

    behaviours[version] keys:
      selftest: exit code of the selftest container (default 0)
      probe:    True if the probe answers health (default True)
      app:      "ok" | "unhealthy" | "dies" | "start_fails" (default "ok")
      poison:   True -> the app writes into the database when it starts
    """

    def __init__(self, data_dir: Path, api=(1, 47), network_mode="lanloom_default", mount="bind"):
        self.data_dir = data_dir
        self.api_version = api
        self.images: dict[str, dict] = {}
        self.tags: dict[str, str] = {}
        self.ctrs: dict[str, dict] = {}
        self.behaviours: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.pulls: list[tuple[str, str]] = []
        self.pull_error: str | None = None
        self.log_lines: dict[str, list[str]] = {}
        self._n = 0
        self.network_mode = network_mode
        self.mount = mount

    # helpers -------------------------------------------------------------
    def add_image(self, version: str, repo: str = REPO, label: str | None = "same") -> str:
        iid = f"sha256:{version.replace('.', '')}{'0' * 20}"
        labels = {"org.opencontainers.image.version": version if label == "same" else label,
                  "org.example.base": "x"}
        if label is None:
            labels.pop("org.opencontainers.image.version")
        self.images[iid] = {"Id": iid, "Config": {
            "Env": ["PATH=/usr/bin", f"IMAGE_BUILD={version}"], "Labels": labels,
            "Cmd": ["python", "-m", "lanloom"], "User": "10001", "WorkingDir": "/app"}}
        self.tags[f"{repo}:{version}"] = iid
        return iid

    def add_app(self, version: str, name="lanloom", ref_tag="latest", env_extra=(), repo: str = REPO):
        iid = self.tags[f"{repo}:{version}"]
        self.tags[f"{repo}:{ref_tag}"] = iid
        img = self.images[iid]["Config"]
        mounts = [{"Type": "bind", "Source": str(self.data_dir), "Destination": "/data"}]
        if self.mount == "volume":
            mounts = [{"Type": "volume", "Name": "lanloom_data", "Source": "/var/lib/vol/_data",
                       "Destination": "/data"}]
        body = {
            "Image": f"{repo}:{ref_tag}", "User": img["User"], "Cmd": img["Cmd"], "WorkingDir": "/app",
            "Env": img["Env"] + ["LANLOOM_PORT=7380", "TZ=UTC", *env_extra],
            "Labels": dict(img["Labels"], **{"org.lanloom.role": "app", "com.example.project": "lanloom"}),
            "Hostname": "",
            "HostConfig": {"NetworkMode": self.network_mode, "RestartPolicy": {"Name": "unless-stopped"},
                           "ReadonlyRootfs": True},
            "NetworkingConfig": {"EndpointsConfig": {self.network_mode: {"Aliases": ["app", name]}}}
            if self.network_mode not in ("host", "none") else None,
        }
        cid = self.container_create(name, body, _mounts=mounts)
        self.container_start(cid)
        self.calls.clear()
        return cid

    def by_name(self, name: str) -> dict | None:
        for c in self.ctrs.values():
            if c["Name"] == "/" + name:
                return c
        return None

    def _resolve(self, ref: str) -> dict:
        if ref in self.ctrs:
            return self.ctrs[ref]
        c = self.by_name(ref)
        if c is None:
            raise NotFound(404, f"no such container: {ref}")
        return c

    def version_of(self, c: dict) -> str:
        return self.images[c["Image"]]["Config"]["Labels"].get("org.opencontainers.image.version") or \
            next(t.split(":")[-1] for t, i in self.tags.items() if i == c["Image"] and E.parse_semver(t.split(":")[-1]))

    # API surface ---------------------------------------------------------
    def negotiate(self):
        return self.api_version

    def api_at_least(self, major, minor):
        return self.api_version >= (major, minor)

    def containers(self, labels=None, all=True):  # noqa: A002
        out = []
        for c in self.ctrs.values():
            ok = True
            for lab in labels or []:
                k, _, v = lab.partition("=")
                if c["Config"]["Labels"].get(k) != v:
                    ok = False
            if ok:
                out.append({"Id": c["Id"], "Names": [c["Name"]], "State": c["State"]["Status"]})
        return out

    def container_inspect(self, ref):
        return json.loads(json.dumps(self._resolve(ref)))

    def container_create(self, name, body, _mounts=None):
        self.calls.append(("create", name))
        if name and self.by_name(name):
            raise Conflict(409, "name in use")
        image = body["Image"]
        iid = self.tags.get(image) or (image if image in self.images else None)
        if not iid:
            raise NotFound(404, "no such image")
        img = self.images[iid]["Config"]
        self._n += 1
        cid = f"{self._n:02d}" + "c" * 62
        env = list(img["Env"])
        for e in body.get("Env") or []:
            k = e.partition("=")[0]
            env = [x for x in env if x.partition("=")[0] != k] + [e]
        labels = dict(img["Labels"], **(body.get("Labels") or {}))
        hc = body.get("HostConfig") or {}
        mounts = _mounts
        if mounts is None:
            mounts = []
            for b in hc.get("Binds") or []:
                src, dst = b.rsplit(":", 1)
                mounts.append({"Type": "bind", "Source": src, "Destination": dst})
            for m in hc.get("Mounts") or []:
                mounts.append({"Type": m["Type"], "Name": m["Source"], "Destination": m["Target"],
                               "Subpath": (m.get("VolumeOptions") or {}).get("Subpath")})
            if not mounts and body.get("_clone_mounts_from"):
                mounts = body["_clone_mounts_from"]
        nets = {}
        mode = hc.get("NetworkMode") or "default"
        for n, ep in ((body.get("NetworkingConfig") or {}).get("EndpointsConfig") or {}).items():
            nets[n] = dict(ep, IPAddress="")
        if not nets and mode not in ("host", "none"):
            nets[mode] = {"IPAddress": ""}
        self.ctrs[cid] = {
            "Id": cid, "Name": "/" + (name or cid[:12]), "Image": iid,
            "Config": {"Image": image, "Env": env, "Labels": labels, "User": body.get("User") or img["User"],
                       "Cmd": body.get("Cmd") or img["Cmd"], "Hostname": body.get("Hostname") or cid[:12]},
            "HostConfig": hc, "Mounts": mounts, "RestartCount": 0,
            "NetworkSettings": {"Networks": nets},
            "State": {"Status": "created", "Running": False, "ExitCode": 0, "Restarting": False},
            "_body": body,
        }
        return cid

    def container_start(self, ref):
        c = self._resolve(ref)
        self.calls.append(("start", c["Name"]))
        version = self.version_of(c)
        beh = self.behaviours.get(version, {})
        labels = c["Config"]["Labels"]
        if "--selftest" in (c["Config"]["Cmd"] or []):
            c["State"].update(Status="exited", Running=False, ExitCode=beh.get("selftest", 0))
            secret = [e for e in c["Config"]["Env"] if e.startswith("API_TOKEN=")]
            self.log_lines[c["Id"]] = ["selftest running", "token is " + "hunter2-secret-value"]
            if beh.get("selftest", 0) == 0:
                self.log_lines[c["Id"]].append(f"selftest: ok {version}")
            return
        if labels.get("org.lanloom.role") == "app" and beh.get("app") == "start_fails":
            raise RuntimeAPIError(500, "cannot start")
        c["State"].update(Status="running", Running=True, ExitCode=0)
        for i, (n, ep) in enumerate(c["NetworkSettings"]["Networks"].items()):
            ep["IPAddress"] = f"10.0.0.{int(c['Id'][:2])}"
        if labels.get("org.lanloom.role") == "app":
            if beh.get("poison"):
                db = sqlite3.connect(self.data_dir / "lanloom.db")
                db.execute("CREATE TABLE IF NOT EXISTS poison(x)")
                db.execute("INSERT INTO poison VALUES (1)")
                db.commit()
                db.close()
            if beh.get("app") == "dies":
                c["RestartCount"] = 1
                c["State"].update(Restarting=True)

    def container_stop(self, ref, t=20):
        c = self._resolve(ref)
        self.calls.append(("stop", c["Name"]))
        c["State"].update(Status="exited", Running=False)

    def container_remove(self, ref, force=True):
        c = self._resolve(ref)
        self.calls.append(("remove", c["Name"]))
        del self.ctrs[c["Id"]]

    def container_rename(self, ref, name):
        c = self._resolve(ref)
        if self.by_name(name):
            raise Conflict(409, "name in use")
        self.calls.append(("rename", c["Name"], name))
        c["Name"] = "/" + name

    def container_logs(self, ref, tail=20):
        return self.log_lines.get(self._resolve(ref)["Id"], [])[-tail:]

    def network_connect(self, network, container, endpoint):
        self.calls.append(("connect", network))

    def image_inspect(self, ref):
        iid = self.tags.get(ref) or (ref if ref in self.images else None)
        return json.loads(json.dumps(self.images[iid])) if iid else None

    def image_tag(self, ref, repo, tag):
        iid = self.tags.get(ref) or (ref if ref in self.images else None)
        if not iid:
            raise NotFound(404, "no such image")
        self.tags[f"{repo}:{tag}"] = iid

    def image_pull(self, repo, tag, progress=None, deadline_s=900, read_timeout=120):
        self.pulls.append((repo, tag))
        if self.pull_error:
            raise RuntimeAPIError(500, self.pull_error)
        if progress:
            progress({"status": "Downloading"})
        return {"status": "done"}

    # health --------------------------------------------------------------
    def health(self, url: str):
        parts = urllib.parse.urlsplit(url)
        for c in self.ctrs.values():
            if not c["State"]["Running"]:
                continue
            env = E.env_list_to_dict(c["Config"]["Env"])
            mode = c["HostConfig"].get("NetworkMode")
            if mode == "host":
                hit = parts.hostname == "127.0.0.1" and str(parts.port) == env.get("LANLOOM_PORT", "7380")
            else:
                ips = [ep.get("IPAddress") for ep in c["NetworkSettings"]["Networks"].values()]
                hit = parts.hostname in ips and str(parts.port) == env.get("LANLOOM_PORT", "7380")
            if not hit:
                continue
            version = self.version_of(c)
            beh = self.behaviours.get(version, {})
            role = c["Config"]["Labels"].get("org.lanloom.role")
            if role == "updater-probe" and not beh.get("probe", True):
                return None
            if role == "app" and beh.get("app", "ok") != "ok":
                return None
            return {"ok": True, "version": version}
        return None


# ------------------------------------------------------------------ fixtures
def make_db(path: Path, marker="keep") -> None:
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT)")
    db.execute("INSERT INTO kv VALUES ('marker', ?)", (marker,))
    db.commit()
    db.close()


def tables(path: Path) -> set[str]:
    db = sqlite3.connect(path)
    try:
        return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        db.close()


def marker(path: Path) -> str | None:
    db = sqlite3.connect(path)
    try:
        row = db.execute("SELECT v FROM kv WHERE k='marker'").fetchone()
        return row[0] if row else None
    finally:
        db.close()


def fast_settings(data: Path, **kw) -> E.Settings:
    s = E.Settings(data_dir=data, poll_s=0.01, heartbeat_s=0.05, selftest_timeout=1.0, probe_timeout=1.0,
                   health_timeout=1.0, rollback_health_timeout=1.0, settle_s=0.0, check_interval=0.01,
                   stop_timeout=1)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


@pytest.fixture
def env(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    make_db(data / "lanloom.db")
    (data / "config.json").write_text('{"a":1}')
    (data / "sub").mkdir()
    (data / "sub" / "x.txt").write_text("x")
    fake = FakeRuntime(data)
    fake.add_image("1.0.0")
    fake.add_image("1.1.0")
    old = fake.add_app("1.0.0")
    upd = E.Updater(fake, fast_settings(data), health_fn=fake.health, echo=lambda line: None)
    return fake, upd, data, old


def request(upd: E.Updater, version="1.1.0", **extra) -> dict:
    upd.ensure_update_dir()
    req = {"version": version, "nonce": NONCE, "requested": time.time(), **extra}
    upd.request_path.write_text(json.dumps(req))
    assert upd.poll_once()
    return json.loads(upd.status_path.read_text())


def probe_leftovers(fake: FakeRuntime, data: Path) -> list:
    left = [c for c in fake.ctrs.values() if c["Config"]["Labels"].get("org.lanloom.role") == "updater-probe"]
    left += list((data / "update").glob("probe-*"))
    return left


# ---------------------------------------------------------------------- tests
def test_happy_path(env):
    fake, upd, data, old = env
    st = request(upd)
    assert st["state"] == "done", st["log"]
    assert st["message_key"] == "update.msg.done"
    assert st["version"] == "1.1.0" and st["from"] == "1.0.0" and st["nonce"] == NONCE
    assert not upd.request_path.exists()
    new = fake.by_name("lanloom")
    assert new and new["Id"] != old and new["State"]["Running"]
    assert new["Config"]["Image"] == f"{REPO}:1.1.0"
    assert old not in fake.ctrs and fake.by_name("lanloom-previous") is None
    # image defaults of the new version apply, user settings are kept
    labels = new["Config"]["Labels"]
    assert labels["org.opencontainers.image.version"] == "1.1.0"
    assert labels["org.lanloom.role"] == "app" and labels["com.example.project"] == "lanloom"
    env_ = new["Config"]["Env"]
    assert "IMAGE_BUILD=1.1.0" in env_ and "IMAGE_BUILD=1.0.0" not in env_ and "TZ=UTC" in env_
    assert new["HostConfig"]["RestartPolicy"] == {"Name": "unless-stopped"}
    assert new["NetworkSettings"]["Networks"]["lanloom_default"]["Aliases"] == ["app", "lanloom"]
    # backup: previous tag, database copy, config copy
    assert fake.tags[f"{REPO}:previous"] == fake.tags[f"{REPO}:1.0.0"]
    assert fake.tags[f"{REPO}:latest"] == fake.tags[f"{REPO}:1.1.0"]  # moving tag follows
    backups = sorted((data / "backups").iterdir())
    assert len(backups) == 1 and backups[0].name.endswith("-1.0.0")
    assert marker(backups[0] / "lanloom.db") == "keep"
    assert (backups[0] / "config.json").exists() and (backups[0] / "sub" / "x.txt").exists()
    assert not (backups[0] / "update").exists() and not (backups[0] / "backups").exists()
    assert json.loads((backups[0] / "backup.json").read_text())["from"] == "1.0.0"
    assert fake.pulls == [(REPO, "1.1.0")]
    assert probe_leftovers(fake, data) == []
    assert marker(data / "lanloom.db") == "keep"
    # the old container was renamed and stopped before the new one was created
    order = [c[0] for c in fake.calls if c[0] in ("rename", "stop", "create", "start")]
    i = order.index("rename")
    assert order[i:i + 2] == ["rename", "stop"]


def test_selftest_fails_changes_nothing(env):
    fake, upd, data, old = env
    fake.behaviours["1.1.0"] = {"selftest": 3}
    st = request(upd)
    assert st["state"] == "failed" and st["message_key"] == "update.err.selftest_failed"
    c = fake.by_name("lanloom")
    assert c["Id"] == old and c["State"]["Running"]
    assert not (data / "backups").exists()
    assert f"{REPO}:previous" not in fake.tags
    assert probe_leftovers(fake, data) == []
    assert not any(k[0] in ("rename", "stop") for k in fake.calls)


def test_probe_fails_changes_nothing(env):
    fake, upd, data, old = env
    fake.behaviours["1.1.0"] = {"probe": False}
    st = request(upd)
    assert st["state"] == "failed" and st["message_key"] == "update.err.probe_failed"
    assert fake.by_name("lanloom")["Id"] == old
    assert probe_leftovers(fake, data) == []
    assert not (data / "backups").exists()
    # the probe got a copy of the data, mounted from a subdirectory of the data volume
    probe_creates = [c for c in fake.calls if c[0] == "create" and "-probe-" in c[1]]
    assert len(probe_creates) == 1


def test_probe_body(env, monkeypatch):
    fake, upd, data, old = env
    seen = {}
    real = fake.container_create

    def spy(name, body, _mounts=None):
        if name and "-probe-" in name:
            seen["body"] = body
            seen["files"] = sorted(p.relative_to(data).as_posix() for p in (data / "update").rglob("*"))
        return real(name, body, _mounts)

    monkeypatch.setattr(fake, "container_create", spy)
    assert request(upd)["state"] == "done"
    body = seen["body"]
    envd = E.env_list_to_dict(body["Env"])
    assert envd["LANLOOM_UPDATE_CHECK"] == "0" and envd["LANLOOM_PROBE"] == "1"
    assert envd["LANLOOM_HOST"] == "0.0.0.0" and envd["LANLOOM_PORT"] == "17380"
    assert envd["TZ"] == "UTC" and "PATH" not in envd
    hc = body["HostConfig"]
    assert hc["NetworkMode"] == "lanloom_default" and hc["ReadonlyRootfs"] is True
    assert hc["Binds"] == [f"{data}/update/probe-{NONCE}:/data"]
    assert body["Labels"]["org.lanloom.role"] == "updater-probe"
    assert f"update/probe-{NONCE}/lanloom.db" in seen["files"]
    assert f"update/probe-{NONCE}/config.json" in seen["files"]
    assert not any("backups" in f for f in seen["files"])


def test_probe_uses_volume_subpath(tmp_path):
    data = tmp_path / "d"
    data.mkdir()
    make_db(data / "lanloom.db")
    fake = FakeRuntime(data, api=(1, 47), mount="volume")
    fake.add_image("1.0.0")
    fake.add_image("1.1.0")
    fake.add_app("1.0.0")
    upd = E.Updater(fake, fast_settings(data), health_fn=fake.health, echo=lambda line: None)
    spec = upd.probe_mount_spec(fake.by_name("lanloom"), "update/probe-x")
    assert spec == {"Mounts": [{"Type": "volume", "Source": "lanloom_data", "Target": "/data",
                                "VolumeOptions": {"Subpath": "update/probe-x"}}]}
    fake.api_version = (1, 41)
    assert upd.probe_mount_spec(fake.by_name("lanloom"), "update/probe-x") == \
        {"Binds": ["/var/lib/vol/_data/update/probe-x:/data"]}


def test_host_network_mode(tmp_path):
    data = tmp_path / "d"
    data.mkdir()
    make_db(data / "lanloom.db")
    fake = FakeRuntime(data, network_mode="host")
    fake.add_image("1.0.0")
    fake.add_image("1.1.0")
    fake.add_app("1.0.0")
    upd = E.Updater(fake, fast_settings(data), health_fn=fake.health, echo=lambda line: None)
    app = fake.by_name("lanloom")
    assert upd.health_url_for(app, upd.app_port(app)) == "http://127.0.0.1:7380/api/health"
    st = request(upd)
    assert st["state"] == "done", st["log"]
    new = fake.by_name("lanloom")
    assert "NetworkingConfig" not in new["_body"] and "Hostname" not in new["_body"]


def test_health_fails_after_swap_rolls_back_and_restores_db(env):
    fake, upd, data, old = env
    fake.behaviours["1.1.0"] = {"app": "unhealthy", "poison": True}
    st = request(upd)
    assert st["state"] == "rolled_back", st["log"]
    assert st["message_key"] == "update.err.health_failed"
    c = fake.by_name("lanloom")
    assert c["Id"] == old and c["State"]["Running"]
    assert fake.by_name("lanloom-previous") is None
    assert len(fake.ctrs) == 1
    assert "poison" not in tables(data / "lanloom.db")
    assert marker(data / "lanloom.db") == "keep"
    assert any("database restored" in line for line in st["log"])


def test_dies_after_start_rolls_back(env):
    fake, upd, data, old = env
    fake.behaviours["1.1.0"] = {"app": "dies"}
    st = request(upd)
    assert st["state"] == "rolled_back" and st["message_key"] == "update.err.health_failed"
    assert any("restarted" in line for line in st["log"])
    assert any("database unchanged" in line for line in st["log"])
    assert fake.by_name("lanloom")["Id"] == old


def test_start_failure_rolls_back(env):
    fake, upd, data, old = env
    fake.behaviours["1.1.0"] = {"app": "start_fails"}
    st = request(upd)
    assert st["state"] == "rolled_back" and st["message_key"] == "update.err.start_failed"
    assert fake.by_name("lanloom")["Id"] == old and fake.by_name("lanloom")["State"]["Running"]


def test_rollback_failure_reports_failed(env, monkeypatch):
    fake, upd, data, old = env
    fake.behaviours["1.1.0"] = {"app": "unhealthy"}
    fake.behaviours["1.0.0"] = {"app": "unhealthy"}  # the old one is broken too now
    st = request(upd)
    assert st["state"] == "failed" and st["message_key"] == "update.err.rollback_failed"
    assert fake.by_name("lanloom")["Id"] == old


@pytest.mark.parametrize("version", ["1.2", "v1.2.0", "1.2.0\n", "1.2.0-rc1", "01.2.3.4", "", 120, None,
                                     "1.2.3 ", "1234567.0.0"])
def test_invalid_versions_refused(env, version):
    fake, upd, data, old = env
    st = request(upd, version=version)
    assert st["state"] == "failed" and st["message_key"] == "update.err.invalid_version"
    assert fake.calls == [] and fake.pulls == []


@pytest.mark.parametrize("patch,key", [
    ({"nonce": "XYZ"}, "update.err.invalid_request"),
    ({"nonce": None}, "update.err.invalid_request"),
    ({"requested": time.time() - 7200}, "update.err.stale_request"),
    ({"requested": "now"}, "update.err.invalid_request"),
    ({"rollback": "yes"}, "update.err.invalid_request"),
    ({"repository": "registry.example/other/thing"}, "update.err.repository_not_allowed"),
])
def test_request_validation(env, patch, key):
    fake, upd, data, old = env
    upd.ensure_update_dir()
    req = {"version": "1.1.0", "nonce": NONCE, "requested": time.time()}
    req.update(patch)
    upd.request_path.write_text(json.dumps(req))
    upd.poll_once()
    st = json.loads(upd.status_path.read_text())
    assert st["state"] == "failed" and st["message_key"] == key
    assert fake.pulls == [] and fake.by_name("lanloom")["Id"] == old


def test_garbage_and_symlink_requests(env, tmp_path):
    fake, upd, data, old = env
    upd.ensure_update_dir()
    upd.request_path.write_text("{not json")
    upd.poll_once()
    assert json.loads(upd.status_path.read_text())["message_key"] == "update.err.invalid_request"
    assert not upd.request_path.exists()
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"version": "1.1.0", "nonce": NONCE, "requested": time.time()}))
    os.symlink(secret, upd.request_path)
    upd.poll_once()
    st = json.loads(upd.status_path.read_text())
    assert st["state"] == "failed" and st["message_key"] == "update.err.invalid_request"
    assert not upd.request_path.is_symlink() and secret.exists()
    upd.request_path.write_text("x" * 10000)
    upd.poll_once()
    assert json.loads(upd.status_path.read_text())["message_key"] == "update.err.invalid_request"
    assert fake.pulls == []


def test_same_repository_only(tmp_path):
    data = tmp_path / "d"
    data.mkdir()
    fake = FakeRuntime(data)
    fake.add_image("1.0.0", repo="lanloom-local")
    fake.add_image("1.1.0", repo="lanloom-local")
    fake.add_app("1.0.0", repo="lanloom-local")
    upd = E.Updater(fake, fast_settings(data), health_fn=fake.health, echo=lambda line: None)
    st = request(upd)
    assert st["state"] == "failed" and st["message_key"] == "update.err.repository_not_allowed"
    # test mode: local repository accepted, never pulled
    upd.s.allow_local = True
    st = request(upd)
    assert st["state"] == "done", st["log"]
    assert fake.pulls == []
    assert fake.by_name("lanloom")["Config"]["Image"] == "lanloom-local:1.1.0"


def test_local_mode_missing_image_fails(tmp_path):
    data = tmp_path / "d"
    data.mkdir()
    fake = FakeRuntime(data)
    fake.add_image("1.0.0", repo="lanloom-local")
    fake.add_app("1.0.0", repo="lanloom-local")
    upd = E.Updater(fake, fast_settings(data, allow_local=True), health_fn=fake.health, echo=lambda line: None)
    st = request(upd, version="1.2.0")
    assert st["state"] == "failed" and st["message_key"] == "update.err.pull_failed"
    assert fake.pulls == []


def test_downgrade_refused_and_rollback_flag(env):
    fake, upd, data, old = env
    fake.add_image("0.9.0")
    st = request(upd, version="0.9.0")
    # a refusal changed nothing, so it is not a failure
    assert st["state"] == "idle" and st["message_key"] == "update.err.downgrade_refused"
    assert fake.pulls == []
    st = request(upd, version="1.0.0")
    assert st["state"] == "idle" and st["message_key"] == "update.err.already_current"
    st = request(upd, version="0.9.0", rollback=True)
    assert st["state"] == "done", st["log"]
    assert fake.version_of(fake.by_name("lanloom")) == "0.9.0"


def test_pull_failure_and_label_mismatch(env):
    fake, upd, data, old = env
    fake.pull_error = "manifest unknown"
    st = request(upd)
    assert st["state"] == "failed" and st["message_key"] == "update.err.pull_failed"
    assert any("manifest unknown" in line for line in st["log"])
    fake.pull_error = None
    fake.add_image("1.2.0", label="9.9.9")
    st = request(upd, version="1.2.0")
    assert st["message_key"] == "update.err.version_label_mismatch"
    assert fake.by_name("lanloom")["Id"] == old


def test_backup_retention_keeps_newest_five(env):
    fake, upd, data, old = env
    b = data / "backups"
    b.mkdir()
    names = [f"2026010{i}T000000Z-0.{i}.0" for i in range(1, 8)]
    for n in names:
        (b / n).mkdir()
    (b / "manual-copy").mkdir()
    (b / "notes.txt").write_text("keep me")
    st = request(upd)
    assert st["state"] == "done"
    left = sorted(p.name for p in b.iterdir() if E.BACKUP_DIR_RE.match(p.name))
    assert len(left) == 5
    assert left[-1].endswith("-1.0.0")  # the new one (current year sorts after 2026-01)
    assert left[:4] == names[-4:]
    assert (b / "manual-copy").exists() and (b / "notes.txt").exists()


def test_prune_ignores_symlinked_dir(env, tmp_path):
    fake, upd, data, old = env
    outside = tmp_path / "outside"
    outside.mkdir()
    for i in range(8):
        (outside / f"2026010{i}T000000Z-0.{i}.0").mkdir()
    os.symlink(outside, data / "backups")
    assert upd.prune_backups() == []
    assert len(list(outside.iterdir())) == 8


def test_atomic_status_writes(tmp_path, monkeypatch):
    target = tmp_path / "status.json"
    E.atomic_write_json(target, {"state": "idle"})
    assert json.loads(target.read_text()) == {"state": "idle"}
    stop = threading.Event()
    errors = []

    def writer():
        i = 0
        while not stop.is_set():
            E.atomic_write_json(target, {"state": "pulling", "log": ["x" * 200] * 50, "i": i})
            i += 1

    def reader():
        for _ in range(400):
            try:
                json.loads(target.read_text())
            except ValueError as exc:
                errors.append(exc)

    t = threading.Thread(target=writer)
    t.start()
    reader()
    stop.set()
    t.join()
    assert errors == []
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]

    # a failing rename keeps the old file and leaves no temp file behind
    def boom(a, b):
        raise OSError("disk full")

    monkeypatch.setattr(E.os, "replace", boom)
    with pytest.raises(OSError):
        E.atomic_write_json(target, {"state": "done"})
    assert json.loads(target.read_text())["state"] == "pulling"
    assert [p.name for p in tmp_path.iterdir()] == ["status.json"]


def test_status_log_is_bounded_and_redacted(env):
    fake, upd, data, old = env
    c = fake.ctrs[old]
    c["Config"]["Env"].append("API_TOKEN=hunter2-secret-value")
    c["Config"]["Env"].append("LANLOOM_PORT=7380")
    st = request(upd)
    text = json.dumps(st)
    assert "hunter2-secret-value" not in text
    assert any("token is ***" in line for line in st["log"])
    for i in range(80):
        upd.log("line %d %s" % (i, "y" * 500))
    st = json.loads(upd.status_path.read_text())
    assert len(st["log"]) == 50 and all(len(line) <= 200 for line in st["log"])


def test_heartbeat_and_owner(env):
    fake, upd, data, old = env
    upd.start_heartbeat()
    deadline = time.time() + 2
    while time.time() < deadline and not upd.heartbeat_path.exists():
        time.sleep(0.01)
    upd.stop()
    hb = json.loads(upd.heartbeat_path.read_text())
    assert set(hb) == {"version", "t"} and abs(hb["t"] - time.time()) < 5


def test_build_create_body_filters(env):
    fake, upd, data, old = env
    c = fake.container_inspect(old)
    c["NetworkSettings"]["Networks"]["lanloom_default"]["Aliases"] = ["app", old[:12]]
    c["NetworkSettings"]["Networks"]["lanloom_default"]["MacAddress"] = "02:00:00:00:00:01"
    c["NetworkSettings"]["Networks"]["extra"] = {"Aliases": ["x"], "IPAddress": "10.1.0.2"}
    c["Config"]["Hostname"] = old[:12]
    body, extra = upd.build_create_body(c, f"{REPO}:1.1.0")
    eps = body["NetworkingConfig"]["EndpointsConfig"]
    assert eps["lanloom_default"] == {"Aliases": ["app"]} and eps["extra"] == {"Aliases": ["x"]}
    assert extra == [] and "Hostname" not in body and "User" not in body and "Cmd" not in body
    fake.api_version = (1, 41)
    body, extra = upd.build_create_body(c, f"{REPO}:1.1.0")
    assert list(body["NetworkingConfig"]["EndpointsConfig"]) == ["lanloom_default"]
    assert extra == [("extra", {"Aliases": ["x"]})]
    c["Config"]["Hostname"] = "myhost"
    c["Config"]["User"] = "2000"
    body, _ = upd.build_create_body(c, f"{REPO}:1.1.0")
    assert body["Hostname"] == "myhost" and body["User"] == "2000"


def test_recover_interrupted_swap(env):
    fake, upd, data, old = env
    fake.container_rename(old, "lanloom-previous")
    fake.container_stop(old)
    upd.ensure_update_dir()
    E.atomic_write_json(upd.status_path, {"state": "swap", "version": "1.1.0", "from": "1.0.0",
                                          "message_key": None, "log": [], "nonce": NONCE, "t": 1})
    (upd.update_dir / f"probe-{NONCE}").mkdir()
    upd.recover()
    st = json.loads(upd.status_path.read_text())
    assert st["state"] == "failed" and st["message_key"] == "update.err.interrupted"
    c = fake.by_name("lanloom")
    assert c["Id"] == old and c["State"]["Running"]
    assert list(upd.update_dir.glob("probe-*")) == []


def test_recover_keeps_terminal_status(env):
    fake, upd, data, old = env
    upd.ensure_update_dir()
    E.atomic_write_json(upd.status_path, {"state": "done", "version": "1.1.0", "from": "1.0.0",
                                          "message_key": "update.msg.done", "log": ["a"], "nonce": NONCE, "t": 1})
    upd.recover()
    st = json.loads(upd.status_path.read_text())
    assert st["state"] == "done" and st["log"] == ["a"]


def test_target_discovery(env):
    fake, upd, data, old = env
    assert upd.find_target()["Id"] == old
    fake.add_app("1.0.0", name="second")
    fake.container_stop("second")
    assert upd.find_target()["Id"] == old  # the running one wins
    fake.container_start("second")
    with pytest.raises(E.UpdateError) as exc:
        upd.find_target()
    assert exc.value.key == "update.err.target_ambiguous"
    upd.s.target_name = "second"
    assert upd.find_target()["Name"] == "/second"
    upd.s.target_name = "missing"
    with pytest.raises(E.UpdateError) as exc:
        upd.find_target()
    assert exc.value.key == "update.err.target_not_found"


def test_once_check(env):
    fake, upd, data, old = env
    report = upd.check()
    assert report["ok"] is True and report["target"]["name"] == "lanloom"
    assert report["health_version"] == "1.0.0" and report["repository_allowed"] is True
    assert "Env" not in json.dumps(report)


def test_helpers():
    assert E.split_ref("reg.example:5000/a/b:1.2.3@sha256:abc") == ("reg.example:5000/a/b", "1.2.3")
    assert E.split_ref("reg.example:5000/a/b") == ("reg.example:5000/a/b", None)
    assert E.is_qualified_repo("registry.example/acme/lanloom")
    assert not E.is_qualified_repo("lanloom") and not E.is_qualified_repo("acme/lanloom")
    assert not E.is_qualified_repo("localhost:5000/lanloom")
    assert E.parse_semver("10.20.30") == (10, 20, 30) and E.parse_semver("1.2.3\n") is None
    assert [E.is_secret_name(n) for n in ("API_KEY", "db_password", "X_TOKEN", "MY_SECRET", "LANLOOM_PORT")] == \
        [True, True, True, True, False]
    assert set(E.MESSAGE_KEYS) >= {"update.msg.done", "update.err.health_failed"}


def test_refuses_to_target_itself(env, monkeypatch):
    fake, upd, data, old = env
    monkeypatch.setattr(E.socket, "gethostname", lambda: old[:12])
    st = request(upd)
    assert st["state"] == "failed" and st["message_key"] == "update.err.target_not_found"
    assert fake.pulls == []


def test_second_request_after_success_keeps_done(env):
    fake, upd, data, old = env
    st = request(upd)
    assert st["state"] == "done"
    # double click / second tab: the same request arrives once more after the swap
    st = request(upd)
    assert st["state"] == "done" and st["message_key"] == "update.msg.done", st
    assert st["version"] == "1.1.0" and st["from"] == "1.0.0"
    assert fake.pulls == [(REPO, "1.1.0")]


def test_refusal_after_a_failed_run_is_not_reported_as_failure(env):
    fake, upd, data, old = env
    fake.behaviours["1.1.0"] = {"selftest": 3}
    assert request(upd)["state"] == "failed"
    st = request(upd, version="1.0.0")
    assert st["state"] == "idle" and st["message_key"] == "update.err.already_current"


def test_selftest_needs_the_ok_line_with_the_target_version(env):
    fake, upd, data, old = env
    real_start = fake.container_start

    def start(ref):
        real_start(ref)
        c = fake._resolve(ref)
        if "--selftest" in (c["Config"]["Cmd"] or []):
            fake.log_lines[c["Id"]] = lines

    fake.container_start = start
    for lines in (["something else entirely"], ["selftest: ok 1.0.0"], ["selftest: ok 1.1.0 extra"],
                  ["selftest: FAIL x", "xselftest: ok 1.1.0"]):
        st = request(upd)
        assert st["state"] == "failed" and st["message_key"] == "update.err.selftest_failed", (lines, st)
        assert fake.by_name("lanloom")["Id"] == old
    lines = ["selftest: ok 1.1.0"]
    assert request(upd)["state"] == "done"
