"""External reports (lanloom/ingest.py, POST /api/v1/ingest) and the nightly report section "API" (lanloom/api_report.py)."""
import pytest

from lanloom import api_report, apitokens, ingest
from lanloom.nightly.contrib import Ctx
from lanloom.store import Store


def test_clean_keeps_text_only_and_checks_limits():
    rep = ingest.clean({"source": "  backup  job ", "title": "<b>T</b>", "level": "warn", "summary": "x" * 900,
                        "items": ["plain", {"text": "two\nlines", "level": "danger"}, {"text": ""}]})
    assert rep["source"] == "backup job" and rep["title"] == "<b>T</b>"            # stored as text, shown as text
    assert len(rep["summary"]) == ingest.LIMITS["summary"]
    assert rep["items"] == [{"text": "plain", "level": "info"}, {"text": "two lines", "level": "danger"}]
    for bad, key in (([], "api.err.ingest_body"), ({"title": "t"}, "api.err.ingest_fields"), ({"source": "s", "title": "t", "level": "red"}, "api.err.ingest_level"),
                     ({"source": "s", "title": "t", "items": [1]}, "api.err.ingest_items"),
                     ({"source": "s", "title": "t", "items": ["x"] * (ingest.MAX_ITEMS + 1)}, "api.err.ingest_items")):
        with pytest.raises(ValueError) as e:
            ingest.clean(bad)
        assert str(e.value) == key


def test_daily_limit_and_retention():
    store = Store(":memory:")
    clock = [1_800_000_000.0]
    ing = ingest.Ingest(store, clock=lambda: clock[0])
    for _ in range(ingest.MAX_PER_DAY):
        ing.add({"source": "s", "title": "t"}, 10)
    with pytest.raises(OverflowError):
        ing.add({"source": "s", "title": "t"}, 10)
    clock[0] += 86401
    ing.add({"source": "s", "title": "t"}, 10)
    clock[0] += 40 * 86400
    ing.prune()
    assert ing.list() == []


class _Core:
    pass


def test_nightly_section_api():
    store = Store(":memory:")
    now = 1_800_000_000.0
    core = _Core()
    uid = store.execute("INSERT INTO users(username, pw_hash, user_handle, created, role) VALUES('ann','!x',x'00',0,'admin')").lastrowid
    core.tokens = apitokens.Tokens(store, clock=lambda: now)
    core.api_ingest = ingest.Ingest(store, clock=lambda: now - 3600)

    class Hooks:
        def list(self):
            return [{"id": "wh-1"}]

        def summary(self, since, until):
            return {"hooks": 1, "sent": 4, "failed": 2, "failed_hooks": ["Chat"], "pending": 0, "failing_now": ["Chat"]}
    core.webhooks = Hooks()
    tok = core.tokens.create({"id": uid, "role": "admin"}, "Backup script", "read", expires_days=7)
    core.api_ingest.add({"source": "disk", "title": "Disk check", "level": "warn", "summary": "sectors"}, 50)
    core.api_ingest.add({"source": "cert", "title": "Certificates", "level": "ok"}, 50)
    ctx = Ctx(core=core, journal=None, since=now - 86400, until=now, prev=None)
    assert api_report._available(ctx)
    sec = api_report._build(ctx)
    keys = [i["key"] for i in sec["items"]]
    assert "api.report.expiring" in keys and "api.report.hook_failed" in keys and "api.report.hook_down" in keys
    assert keys.count("api.report.ingest") == 2 and sec["level"] == "warn"
    disk = next(i for i in sec["items"] if i["key"] == "api.report.ingest" and i["vars"]["source"] == "disk")
    assert disk["point"] and disk["level"] == "warn"
    assert tok["token"] not in repr(sec)


def test_nightly_section_stays_away_without_api_use():
    store = Store(":memory:")
    core = _Core()
    core.tokens, core.api_ingest = apitokens.Tokens(store), ingest.Ingest(store)

    class Hooks:
        def list(self):
            return []
    core.webhooks = Hooks()
    assert not api_report._available(Ctx(core=core, journal=None, since=0, until=1e10, prev=None))
