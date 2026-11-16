"""Fixtures for the AI tests: a manager wired to a stand-in service factory, a fake core and a movable clock."""
import json
import os
import time
from types import SimpleNamespace

import pytest

from lanloom.ai import manager as manager_mod
from lanloom.ai.manager import AI
from lanloom.crypto import KEY_BYTES, Vault
from lanloom.store import Store

from .fakes import KEY, Fake, FakeCore, device, ndjson, sse

START = 1_700_200_000.0
LOCAL_URL = "http://192.168.1.9:11434"


class Clock:
    def __init__(self, t=START):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class Env:
    def __init__(self, tmp_path, demo=False):
        self.store = Store(":memory:")
        self.vault = Vault(self.store, os.urandom(KEY_BYTES))
        self.core = FakeCore([device()])
        self.fake = Fake()
        self.clock = Clock()
        self.data = tmp_path
        self.ai = AI(self.core, self.store, self.vault, SimpleNamespace(demo=demo, data_dir=tmp_path),
                     opener=self.fake, clock=self.clock)
        self.core.ai = self.ai

    # services -----------------------------------------------------------------
    def local_answers(self, text="Local answer."):
        self.fake.on("/api/chat", ndjson({"message": {"content": text}, "done": True}))

    def public_answers(self, text="Public answer."):
        self.fake.on("/chat/completions", sse({"choices": [{"delta": {"content": text}}]}))

    def enable_local(self, **extra):
        self.ai.update_settings({"local": dict({"kind": "ollama", "url": LOCAL_URL, "model": "llama3.1:8b", "enabled": True}, **extra)})

    def enable_public(self, allow=("explain_device",), **extra):
        self.ai.update_settings({"public": dict({"kind": "openai", "model": "gpt-4o-mini", "enabled": True}, **extra),
                                 "allow": {f: True for f in allow}}, {"public_key": KEY})

    # jobs ---------------------------------------------------------------------
    def wait(self, job_or_id, timeout=10.0):
        jid = job_or_id["id"] if isinstance(job_or_id, dict) else job_or_id
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            v = self.ai.job_view(jid)
            if v["state"] in ("done", "error"):
                return v
            time.sleep(0.01)
        raise AssertionError("job did not finish: %r" % self.ai.job_view(jid))

    def explain(self, ref=None):
        return self.wait(self.ai.explain("device", ref or {"id": "dev1"}, "en"))


@pytest.fixture()
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.ai._pool.shutdown(wait=True)


@pytest.fixture()
def demo_env(tmp_path):
    e = Env(tmp_path, demo=True)
    yield e
    e.ai._pool.shutdown(wait=True)
