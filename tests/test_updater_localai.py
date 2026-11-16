"""Tests of the local AI service management in the update sidecar (fake runtime, no real sleeps)."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lanloom.ai import runtime as R  # noqa: E402
from updater import engine as E  # noqa: E402
from updater import localai as L  # noqa: E402
from updater.runtimeapi import Conflict, NotFound, RuntimeAPIError  # noqa: E402

NOW = 1_800_000_000.0
NONCE = "0123456789abcdef0123456789abcdef"


class FakeRuntime:
    def __init__(self, network_mode="host"):
        self.ctrs: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.pull_error: str | None = None
        self.start_error = False
        self.create_error = False
        self.network_mode = network_mode
        self.ctrs["app"] = {"Id": "app", "Name": "/lanloom", "State": {"Running": True},
                            "Config": {"Labels": {"org.lanloom.role": "app"}},
                            "HostConfig": {"NetworkMode": network_mode}}

    def api_names(self):
        return [c[0] for c in self.calls]

    def containers(self, labels=None, all=True):  # noqa: A002
        every = __builtins__['all'] if isinstance(__builtins__, dict) else __builtins__.all
        out = []
        for c in self.ctrs.values():
            if every(c["Config"]["Labels"].get(k) == v for k, _, v in (x.partition("=") for x in labels or [])):
                out.append({"Id": c["Id"], "Names": [c["Name"]], "State": "running"})
        return out

    def _find(self, ref):
        for c in self.ctrs.values():
            if ref in (c["Id"], c["Name"].lstrip("/")):
                return c
        raise NotFound(404, "no such container")

    def container_inspect(self, ref):
        return json.loads(json.dumps(self._find(ref)))

    def image_pull(self, repo, tag, progress=None, deadline_s=900, read_timeout=120):
        self.calls.append(("pull", repo, tag, deadline_s))
        if self.pull_error:
            raise RuntimeAPIError(500, self.pull_error)
        if progress:
            progress({"status": "Downloading", "progressDetail": {"current": 37, "total": 100}})
        return {}

    def container_create(self, name, body):
        self.calls.append(("create", name, body))
        if self.create_error:
            raise RuntimeAPIError(500, "boom")
        if any(c["Name"] == "/" + name for c in self.ctrs.values()):
            raise Conflict(409, "in use")
        self.ctrs[name] = {"Id": name, "Name": "/" + name, "State": {"Running": False},
                           "Config": {"Labels": body["Labels"]}, "HostConfig": body["HostConfig"]}
        return name

    def container_start(self, ref):
        self.calls.append(("start", ref))
        if self.start_error:
            raise RuntimeAPIError(500, "cannot start")
        self._find(ref)["State"]["Running"] = True

    def container_stop(self, ref, t=20):
        self.calls.append(("stop", ref))
        self._find(ref)["State"]["Running"] = False

    def container_remove(self, ref, force=True):
        self.calls.append(("remove", ref))
        c = self._find(ref)
        del self.ctrs[c["Id"]]


class Env:
    def __init__(self, tmp_path, network_mode="host", ready=True, port_busy=False):
        self.data = tmp_path / "data"
        self.data.mkdir()
        self.rt = FakeRuntime(network_mode)
        self.now = [NOW]
        self.urls: list[str] = []
        self.ready = ready
        self.lines: list[str] = []
        self.ai = L.LocalAI(self.rt, E.Settings(data_dir=self.data), clock=lambda: self.now[0],
                            echo=self.lines.append, http_get=self.http_get, sleep=self.sleep,
                            port_open=lambda p: port_busy,
                            find_target=lambda: E.Updater.find_target(self.ai))

    def http_get(self, url):
        self.urls.append(url)
        return 200 if self.ready else None

    def sleep(self, s):
        self.now[0] += max(s, 1.0)

    def request(self, **over):
        req = {"action": "install", "nonce": NONCE, "requested": NOW}
        req.update(over)
        (self.data / "update").mkdir(exist_ok=True)
        (self.data / "update" / R.REQUEST_FILE).write_text(json.dumps(req))

    def status(self):
        return json.loads((self.data / "update" / R.STATUS_FILE).read_text())


@pytest.fixture()
def env(tmp_path):
    return Env(tmp_path)


def test_install_host_network(env):
    env.request()
    assert env.ai.poll_once() is True
    st = env.status()
    assert st["state"] == "running" and st["message_key"] == "ai.local.sidecar_running"
    assert st["url"] == "http://127.0.0.1:%d" % R.PORT and st["nonce"] == NONCE
    assert env.urls == ["http://127.0.0.1:%d/api/version" % R.PORT]
    assert not (env.data / "update" / R.REQUEST_FILE).exists()
    pull = next(c for c in env.rt.calls if c[0] == "pull")
    assert (pull[1], pull[2], pull[3]) == (R.IMAGE_REPO, R.IMAGE_TAG, 1800)
    body = next(c for c in env.rt.calls if c[0] == "create")[2]
    assert body["Image"] == R.image_ref()
    assert R.container_env(True) == body["Env"] and "127.0.0.1:%d" % R.PORT in body["Env"][0]
    assert body["Labels"] == {"org.lanloom.role": R.ROLE, "org.lanloom.ai.parent": "lanloom"}
    hc = body["HostConfig"]
    assert hc["NetworkMode"] == "host" and hc["RestartPolicy"] == {"Name": "unless-stopped"}
    assert hc["Mounts"] == [{"Type": "volume", "Source": R.VOLUME, "Target": R.MODELS_DIR}]
    assert hc["SecurityOpt"] == ["no-new-privileges:true"] and "NetworkingConfig" not in body
    assert "docker.sock" not in json.dumps(body)


def test_install_other_network(tmp_path):
    e = Env(tmp_path, network_mode="lanloom_default")
    e.request()
    e.ai.poll_once()
    st = e.status()
    assert st["state"] == "running" and st["url"] == "http://%s:%d" % (R.CONTAINER, R.PORT)
    body = next(c for c in e.rt.calls if c[0] == "create")[2]
    assert body["HostConfig"]["NetworkMode"] == "lanloom_default"
    assert body["NetworkingConfig"]["EndpointsConfig"] == {"lanloom_default": {"Aliases": [R.CONTAINER]}}
    assert R.container_env(False) == body["Env"] and "0.0.0.0:%d" % R.PORT in body["Env"][0]
    assert e.urls == []  # the sidecar does not probe a service it may not reach


def test_existing_stopped_container_only_started(env):
    env.rt.ctrs[R.CONTAINER] = {"Id": R.CONTAINER, "Name": "/" + R.CONTAINER, "State": {"Running": False},
                                "Config": {"Labels": {"org.lanloom.role": R.ROLE}}, "HostConfig": {}}
    env.request()
    env.ai.poll_once()
    assert env.status()["state"] == "running"
    assert env.rt.api_names() == ["start"]


def test_existing_running_container_untouched(env):
    env.rt.ctrs[R.CONTAINER] = {"Id": R.CONTAINER, "Name": "/" + R.CONTAINER, "State": {"Running": True},
                                "Config": {"Labels": {"org.lanloom.role": R.ROLE}}, "HostConfig": {}}
    env.request()
    env.ai.poll_once()
    assert env.status()["state"] == "running" and env.rt.calls == []


def test_foreign_container_with_service_name_refused(env):
    env.rt.ctrs[R.CONTAINER] = {"Id": "x", "Name": "/" + R.CONTAINER, "State": {"Running": True},
                                "Config": {"Labels": {}}, "HostConfig": {}}
    env.request()
    env.ai.poll_once()
    assert env.status()["message_key"] == "ai.err.not_ours" and env.rt.calls == []


def test_port_busy(tmp_path):
    e = Env(tmp_path, port_busy=True)
    e.request()
    e.ai.poll_once()
    st = e.status()
    assert st["state"] == "failed" and st["message_key"] == "ai.err.port_busy"
    assert e.rt.calls == []


def test_pull_failure_creates_nothing(env):
    env.rt.pull_error = "denied"
    env.request()
    env.ai.poll_once()
    st = env.status()
    assert st["state"] == "failed" and st["message_key"] == "ai.err.pull_failed"
    assert env.rt.api_names() == ["pull"]


def test_create_failure(env):
    env.rt.create_error = True
    env.request()
    env.ai.poll_once()
    assert env.status()["message_key"] == "ai.err.create_failed"


def test_start_failure_removes_container(env):
    env.rt.start_error = True
    env.request()
    env.ai.poll_once()
    assert env.status()["message_key"] == "ai.err.start_failed"
    assert R.CONTAINER not in env.rt.ctrs and env.rt.api_names()[-1] == "remove"


def test_not_ready_in_time_removes_container(tmp_path):
    e = Env(tmp_path, ready=False)
    e.request()
    e.ai.poll_once()
    assert e.status()["message_key"] == "ai.err.start_failed"
    assert R.CONTAINER not in e.rt.ctrs


def test_no_target(env):
    del env.rt.ctrs["app"]
    env.request()
    env.ai.poll_once()
    assert env.status()["message_key"] == "ai.err.no_target" and env.rt.api_names() == []


def test_remove_only_own_container(env):
    env.rt.ctrs[R.CONTAINER] = {"Id": R.CONTAINER, "Name": "/" + R.CONTAINER, "State": {"Running": True},
                                "Config": {"Labels": {"org.lanloom.role": R.ROLE}}, "HostConfig": {}}
    env.request(action="remove")
    env.ai.poll_once()
    st = env.status()
    assert st["state"] == "removed" and R.CONTAINER not in env.rt.ctrs
    assert env.rt.api_names() == ["stop", "remove"]


def test_remove_foreign_container_refused(env):
    env.rt.ctrs[R.CONTAINER] = {"Id": R.CONTAINER, "Name": "/" + R.CONTAINER, "State": {"Running": True},
                                "Config": {"Labels": {"org.lanloom.role": "app"}}, "HostConfig": {}}
    env.request(action="remove")
    env.ai.poll_once()
    assert env.status()["message_key"] == "ai.err.not_ours" and env.rt.calls == []


def test_remove_without_container(env):
    env.request(action="remove")
    env.ai.poll_once()
    assert env.status()["state"] == "removed" and env.rt.calls == []


@pytest.mark.parametrize("over", [
    {"action": "rm -rf /"}, {"action": None}, {"nonce": "abc"}, {"nonce": "G" * 32}, {"nonce": NONCE + "0"},
    {"requested": NOW - 7200}, {"requested": NOW + 3600}, {"requested": "now"}, {"requested": True},
])
def test_invalid_requests_make_no_api_call(env, over):
    env.request(**over)
    env.ai.poll_once()
    st = env.status()
    assert st["state"] == "failed" and st["message_key"] == "ai.err.invalid_request"
    assert env.rt.calls == []
    assert not (env.data / "update" / R.REQUEST_FILE).exists()


def test_symlink_request_refused(env, tmp_path):
    (env.data / "update").mkdir()
    secret = tmp_path / "other.json"
    secret.write_text(json.dumps({"action": "install", "nonce": NONCE, "requested": NOW}))
    os.symlink(secret, env.data / "update" / R.REQUEST_FILE)
    env.ai.poll_once()
    assert env.status()["message_key"] == "ai.err.invalid_request" and env.rt.calls == []
    assert secret.exists()


def test_oversized_and_non_object_requests(env):
    (env.data / "update").mkdir()
    path = env.data / "update" / R.REQUEST_FILE
    path.write_text(json.dumps({"action": "install", "nonce": NONCE, "requested": NOW, "pad": "x" * 5000}))
    env.ai.poll_once()
    assert env.status()["message_key"] == "ai.err.invalid_request" and env.rt.calls == []
    path.write_text("[1, 2]")
    env.ai.poll_once()
    assert env.status()["message_key"] == "ai.err.invalid_request" and env.rt.calls == []


def test_no_request_is_a_no_op(env):
    assert env.ai.poll_once() is False
    assert not (env.data / "update" / R.STATUS_FILE).exists()


def test_extra_request_fields_never_reach_the_runtime(env):
    env.request(image="evil/image:1", name="x", env=["A=B"], mounts=["/:/host"])
    env.ai.poll_once()
    blob = json.dumps(env.rt.calls)
    assert "evil" not in blob and "/host" not in blob and "A=B" not in blob
    assert env.status()["state"] == "running"


def test_status_log_is_bounded(env):
    for i in range(80):
        env.ai.log("x" * 500 + str(i))
    st = json.loads((env.data / "update" / R.STATUS_FILE).read_text())
    assert len(st["log"]) == 50 and all(len(x) <= 200 for x in st["log"])


def test_source_reads_only_the_three_fields():
    src = (Path(__file__).resolve().parent.parent / "updater" / "localai.py").read_text()
    assert set(re.findall(r"\breq\.get\(\"(\w+)\"\)", src)) == {"action", "nonce", "requested"}
    for bad in (r"\breq\[", r"\breq\.items", r"\breq\.values", r"\*\*req\b", r"\breq\.keys", r"\breq\.pop"):
        assert not re.search(bad, src), bad
    # every other use of the name is a type check or a hand-over to the validator
    others = [m.group(0) for m in re.finditer(r"[^\n]*\breq\b[^\n]*", src)
              if not re.search(r"req\.get\(\"(action|nonce|requested)\"\)", m.group(0))]
    assert all(("isinstance(req" in ln or "validate(req)" in ln or "req = " in ln or "return req" in ln
                or "req is None" in ln or "(self, req: dict)" in ln or "if req" in ln or "def " in ln)
               for ln in others), others


def test_update_request_goes_first(tmp_path):
    rt = FakeRuntime()
    data = tmp_path / "data"
    (data / "update").mkdir(parents=True)
    up = E.Updater(rt, E.Settings(data_dir=data), clock=lambda: NOW, echo=lambda _l: None, sleep=lambda _s: None)
    up.localai.http_get = lambda url: 200
    up.localai.port_open = lambda p: False
    (data / "update" / "request.json").write_text(json.dumps({"version": "bad", "nonce": NONCE, "requested": NOW}))
    (data / "update" / R.REQUEST_FILE).write_text(json.dumps({"action": "install", "nonce": NONCE, "requested": NOW}))
    assert up.poll_once() is True
    assert (data / "update" / "status.json").exists()
    assert (data / "update" / R.REQUEST_FILE).exists() and not (data / "update" / R.STATUS_FILE).exists()
    assert up.poll_once() is True
    assert json.loads((data / "update" / R.STATUS_FILE).read_text())["state"] == "running"
    assert up.check.__self__ is up and up.localai.check() == {"container": True, "running": True}
