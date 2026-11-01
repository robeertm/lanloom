"""Shared source contract: backoff, status events, never raising, hygiene of the source files."""
import ast
import asyncio
import inspect
import ipaddress
import os
import re

import pytest

from lanloom import sources
from lanloom.sources import _common as c

SOURCES_DIR = os.path.dirname(sources.__file__)
FILES = sorted(f for f in os.listdir(SOURCES_DIR) if f.endswith(".py"))


def test_backoff_sequence_and_reset():
    b = c.Backoff()
    assert [b.next() for _ in range(9)] == [1, 2, 4, 8, 16, 32, 60, 60, 60]
    b.reset()
    assert b.next() == 1


def test_run_with_backoff_never_raises_and_reports_changes_only():
    events = []
    calls = []

    async def session(emit, status):
        calls.append(1)
        if len(calls) == 3:
            await status.set(emit, True, "source.x.ok")
            raise ConnectionRefusedError()
        raise RuntimeError("secret-looking text that must not appear")

    async def go():
        task = asyncio.create_task(c.run_with_backoff("x", "x", events.append, session,
                                                      backoff=c.Backoff(0.01, 0.02)))
        await asyncio.sleep(0.2)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(go())
    keys = [(e["ok"], e["key"]) for e in events]
    assert keys[:3] == [(False, "source.x.error"), (True, "source.x.ok"), (False, "source.x.refused")]
    assert keys[3] == (False, "source.x.error") and len(calls) > 4
    assert all(set(e) == {"type", "source", "ok", "key", "t"} for e in events)
    assert "secret" not in repr(events)


def test_emit_event_survives_consumer_errors():
    async def bad(ev):
        raise ValueError("boom")

    asyncio.run(c.emit_event(bad, {"type": "x"}))
    asyncio.run(c.emit_event(lambda ev: 1 / 0, {"type": "x"}))


def test_error_keys():
    import ssl
    assert c.error_key("m", asyncio.TimeoutError()) == "source.m.timeout"
    assert c.error_key("m", ConnectionRefusedError()) == "source.m.refused"
    assert c.error_key("m", ssl.SSLError()) == "source.m.tls"
    assert c.error_key("m", OSError()) == "source.m.unreachable"
    assert c.error_key("m", c.SourceError("source.m.custom", "t")) == "source.m.custom"
    assert c.error_key("m", KeyError()) == "source.m.error"


def test_clip():
    assert c.clip(None) is None
    assert c.clip("a\x00b\nc\td") == "abc\td"
    assert c.clip("x" * 300) == "x" * 255
    assert c.clip(12) == "12"


def test_reason_keys_cover_emitted_literals():
    used = set()
    for name in FILES:
        text = open(os.path.join(SOURCES_DIR, name), encoding="utf-8").read()
        used |= set(re.findall(r'"(source\.[a-z]+\.[a-z_]+)"', text))
    assert used <= set(sources.REASON_KEYS), used - set(sources.REASON_KEYS)


def test_every_source_has_the_contract():
    for cls in sources.SOURCES.values():
        assert inspect.iscoroutinefunction(cls.run) and inspect.iscoroutinefunction(cls.test)


@pytest.mark.parametrize("name", FILES)
def test_source_file_hygiene(name):
    path = os.path.join(SOURCES_DIR, name)
    text = open(path, encoding="utf-8").read()
    assert text.isascii(), "English-only, ASCII source"
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name.split(".")[0] for a in node.names] + ([node.module.split(".")[0]] if getattr(node, "module", None) else [])
            assert not set(mods) & {"subprocess", "ctypes", "requests", "pickle", "shelve", "multiprocessing"}, mods
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in ("eval", "exec", "print"), node.func.id
    # only documentation / private example addresses may appear as literals
    for ip in re.findall(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])", text):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue                    # OID fragments such as 1.3.6.1
        assert addr.is_private or addr.is_loopback or addr.is_unspecified or addr.is_reserved, ip
