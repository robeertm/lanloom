"""Nightly report section "AI"."""
from types import SimpleNamespace

from lanloom.ai import report
from lanloom.nightly import contrib

from .conftest import START
from .fakes import KEY, ndjson


def ctx_of(env):
    return SimpleNamespace(core=SimpleNamespace(ai=env.ai), since=START - 3600, until=START + 3600)


def test_section_is_registered():
    assert any(c.id == "ai" for c in contrib.REGISTRY)


def test_only_available_when_ai_is_switched_on(env):
    ctx = ctx_of(env)
    assert report._available(ctx) is False
    assert report._available(SimpleNamespace(core=SimpleNamespace())) is False
    env.enable_local()
    assert report._available(ctx) is True
    env.ai.update_settings({"local": {"enabled": False}})
    assert report._available(ctx) is False
    env.enable_public()
    assert report._available(ctx) is True


def test_quiet_night(env):
    env.enable_local()
    sec = report._build(ctx_of(env))
    assert sec["id"] == "ai" and sec["items"] == [] and sec["summary"]["key"] == "ai.report.quiet"


def test_counts_local_and_public(env):
    env.local_answers()
    env.public_answers()
    env.enable_local()
    env.enable_public()
    env.explain()
    env.explain()
    env.ai.update_settings({"order": "public_first"})
    env.explain()
    sec = report._build(ctx_of(env))
    assert sec["summary"]["key"] == "ai.report.summary"
    usage = next(i for i in sec["items"] if i["id"] == "ai-usage")
    assert usage["vars"] == {"local": 2, "public": 1} and usage["point"] is False and usage["level"] == "ok"
    assert sec["level"] == "ok"
    assert KEY not in str(sec)


def test_limit_is_a_point(env):
    env.public_answers()
    env.enable_public()
    env.ai.update_settings({"daily_limit": 1})
    env.explain()
    assert env.explain()["error"] == "ai.err.daily_limit"
    sec = report._build(ctx_of(env))
    lim = next(i for i in sec["items"] if i["id"] == "ai-limit")
    assert lim["point"] is True and lim["level"] == "warn" and lim["vars"] == {"n": 1, "limit": 1}
    assert sec["level"] == "warn"


def test_failures_only_is_a_point_some_failures_a_note(env):
    from lanloom.ai.providers import AIError
    env.enable_local()
    env.fake.on("/api/chat", AIError("ai.err.unreachable"))
    env.explain()
    sec = report._build(ctx_of(env))
    bad = next(i for i in sec["items"] if i["id"] == "ai-failed")
    assert bad["point"] is True and bad["level"] == "warn" and sec["summary"]["key"] == "ai.report.summary"
    env.fake.routes.clear()
    env.local_answers()
    env.explain()
    sec = report._build(ctx_of(env))
    assert {i["id"] for i in sec["items"]} == {"ai-failed-some", "ai-usage"}
    assert not any(i["point"] for i in sec["items"])


def test_usage_outside_the_window_is_not_counted(env):
    env.local_answers()
    env.enable_local()
    env.explain()
    far = SimpleNamespace(core=SimpleNamespace(ai=env.ai), since=START + 10 * 86400, until=START + 10 * 86400 + 3600)
    assert report._build(far)["items"] == []
