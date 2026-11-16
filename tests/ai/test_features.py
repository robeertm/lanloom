"""KI2 server-side features: ask, plain-words paragraph, identification help, firewall suggestions, hints, nightly."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lanloom.ai import features, providers, report as ai_report
from lanloom.ai.providers import Limits
from lanloom.classify import KINDS
from lanloom.firewall import model
from lanloom.firewall.adapter import sample_state
from lanloom.nightly import Reports, contrib, deliver as deliver_mod
from lanloom.nightly.contrib import item, section

from .conftest import START
from .fakes import KEY, FakeCore, device, ndjson, sse
from .test_stream_limits import Server, head

FEATURE_FNS = ("ask", "identify", "suggest_firewall", "report_plain")
DAY = "2026-11-15"


# ---------------------------------------------------------------------- helpers
class FakeReports:
    def __init__(self, latest=None):
        self._latest = latest
        self.plain = {}

    def latest(self):
        return self._latest

    def get(self, day):
        return self._latest if self._latest and self._latest["day"] == day else None

    def tz(self):
        return None

    def set_plain(self, day, rec):
        self.plain[day] = rec


class FakeFirewall:
    """Answers state(); records every other call (the AI must never touch anything else)."""

    def __init__(self, state=None):
        self._st = state if state is not None else sample_state()
        self.other_calls = []

    def state(self):
        return self._st

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def call(*a, **k):
            self.other_calls.append(name)
        self.other_calls.append("get:" + name)
        return call


def make_report(points=1):
    items = [item("a1", "ai.report.limit", "warn", point=True, n=3, limit=5)] if points else []
    return {"day": DAY, "points": points, "created": START, "since": START - 86400, "until": START,
            "sections": [section("ai", "ai.report.title", "ai.report.quiet", items)]}


def with_reports(env, rep=None):
    env.core.reports = FakeReports(rep or make_report())
    return env.core.reports


def snapshot(env):
    return (sorted((r["key"], r["value"]) for r in env.store.query("SELECT key, value FROM settings")),
            [dict(r) for r in env.store.query("SELECT * FROM devices")])


def body_of(call):
    return json.dumps(call.body)


def ids_refs(st):
    f, possible = features.firewall_facts(st, None)
    return {v["id"]: k for k, v in f.devices.items()}, {v["id"]: k for k, v in f.networks.items()}, possible


def public_only(env, allow=FEATURE_FNS):
    env.enable_public(allow=allow)


# ---------------------------------------------------------------------- 1. ask: links, question, history
def test_ask_keeps_known_refs_and_links_and_drops_unknown(env):
    env.local_answers("[D1] prints. See [view:paths] and [D99] and [view:evil] and [N1].")
    env.enable_local()
    res = env.wait(env.ai.ask("What is the printer?", lang="en"))
    assert res["state"] == "done", res
    out = res["result"]
    assert "[D1]" in out["text"] and "[view:paths]" in out["text"]
    assert "D99" not in out["text"] and "evil" not in out["text"] and "[N1]" not in out["text"]
    hrefs = {l["href"] for l in out["links"]}
    assert hrefs == {"#/devices/dev1", "#/paths"}
    dev = next(l for l in out["links"] if l["kind"] == "device")
    assert dev["id"] == "dev1"


def test_ask_requires_a_question(env):
    env.enable_local()
    for q in ("", "   ", None):
        with pytest.raises(ValueError) as e:
            env.ai.ask(q)
        assert str(e.value) == "ai.err.question"


def test_question_is_clipped_to_500_chars(env):
    env.local_answers("ok")
    env.enable_local()
    env.wait(env.ai.ask("q" * 900 + "END"))
    sent = body_of(env.fake.calls[-1])
    assert "q" * 500 in sent and "q" * 501 not in sent and "END" not in sent


def test_history_keeps_only_user_and_assistant_and_six_turns():
    hist = [{"role": "system", "text": "evil"}, {"role": "tool", "text": "x"}, "junk", None, {"role": "user", "text": ""}]
    hist += [{"role": "user" if i % 2 else "assistant", "text": "t%d" % i} for i in range(10)]
    out = features.clean_history(hist)
    assert len(out) <= features.HISTORY_TURNS == 6
    assert all(t["role"] in ("user", "assistant") for t in out)
    assert [t["text"] for t in out] == ["t%d" % i for i in range(4, 10)]
    assert features.clean_history("nope") == []
    assert features.clean_history([{"role": "user", "text": "z" * 5000}])[0]["text"] == "z" * features.HISTORY_CHARS


def test_ask_sends_cleaned_history(env):
    env.local_answers("ok")
    env.enable_local()
    hist = [{"role": "system", "text": "SYSTEMEVIL"}, {"role": "user", "text": "earlier question"}]
    env.wait(env.ai.ask("now?", hist))
    sent = body_of(env.fake.calls[-1])
    assert "earlier question" in sent and "SYSTEMEVIL" not in sent


# ---------------------------------------------------------------------- 2. read-only
def test_ask_identify_firewall_change_nothing_but_usage_and_hints(env):
    env.local_answers('{"kind": "printer", "label": "x", "reason": "r", "moves": []}')
    env.enable_local()
    env.ai.update_settings({"allow": {f: True for f in FEATURE_FNS}})
    fw = env.ai.firewall = FakeFirewall()
    before = snapshot(env)
    for job in (env.ai.ask("what?"), env.ai.identify("dev1"), env.ai.firewall_suggest()):
        assert env.wait(job)["state"] == "done"
    after = snapshot(env)
    assert after[1] == before[1]
    changed = {k for k, _ in set(after[0]) ^ set(before[0])}
    assert changed <= {"ai_usage", "ai_hints"}
    assert not any(k.startswith("fw_draft") for k, _ in after[0])
    assert fw.other_calls == []


# ---------------------------------------------------------------------- 3. leak test
def assert_no_real_values(sent):
    for real in ("192.168.1.20", "aa:bb:cc:dd:ee:01", "attic-printer.lan", "Attic Printer", "attic-printer"):
        assert real not in sent, real


def test_ask_public_service_gets_placeholders_and_answer_is_translated_back(env):
    env.public_answers("[D1] sits at [IP-1] (see [view:devices]).")
    public_only(env)
    res = env.wait(env.ai.ask("Where is the Attic Printer at 192.168.1.20?"))
    assert res["state"] == "done", res
    sent = body_of(env.fake.calls[-1])
    assert_no_real_values(sent)
    assert "[HOST-1]" in sent or "[HOST-2]" in sent
    assert "[IP-1]" in sent
    assert res["result"]["pseudonymised"] is True
    assert "192.168.1.20" in res["result"]["text"] and "[IP-1]" not in res["result"]["text"]


def test_identify_public_service_gets_placeholders(env):
    env.public_answers('{"kind": "printer", "label": "Printer at [IP-1]", "reason": "Name [HOST-1] suggests it"}')
    public_only(env)
    env.core._devices[0]["vendor"] = "Acme"
    res = env.wait(env.ai.identify("dev1"))
    assert res["state"] == "done", res
    sent = body_of(env.fake.calls[-1])
    assert_no_real_values(sent)
    assert "[IP-1]" in sent and "[HOST-1]" in sent or "[HOST-2]" in sent
    assert res["result"]["pseudonymised"] is True
    assert "192.168.1.20" in res["result"]["what"] or "192.168.1.20" in res["result"]["reason"]
    assert "[HOST-1]" not in res["result"]["reason"]


def test_report_plain_public_service_gets_placeholders(env):
    env.public_answers("Look at [IP-1] today.")
    public_only(env)
    rep = make_report()
    rep["sections"][0]["items"].append(item("a2", "report.devices.new", "info", point=True, name="Attic Printer", ip="192.168.1.20", at="02:00"))
    with_reports(env, rep)
    plain = env.ai.plain_for(rep)
    assert plain and plain["pseudonymised"] is True
    sent = body_of(env.fake.calls[-1])
    assert_no_real_values(sent)
    assert "192.168.1.20" in plain["text"]


def test_report_plain_job_public_service_no_leak(env):
    env.public_answers("Calm night.")
    public_only(env)
    with_reports(env)
    res = env.wait(env.ai.report_plain(DAY))
    assert res["state"] == "done", res
    assert_no_real_values(body_of(env.fake.calls[-1]))
    assert env.core.reports.plain[DAY]["text"] == "Calm night."


def test_firewall_public_service_gets_placeholders(env):
    st = sample_state()
    env.core._devices[0].update(title=st["devices"][7]["name"], ip=st["devices"][7]["ip"])
    env.public_answers('{"moves": [], "summary": "ok"}')
    public_only(env)
    env.ai.firewall = FakeFirewall(st)
    res = env.wait(env.ai.firewall_suggest())
    assert res["state"] == "done", res
    sent = body_of(env.fake.calls[-1])
    assert res["result"]["pseudonymised"] is True
    assert "leak" not in res["result"]
    assert st["devices"][7]["ip"] not in sent


# ---------------------------------------------------------------------- 4. per-function permission, daily limit
def test_public_allowed_for_explain_only_gets_no_ask(env):
    env.public_answers()
    env.enable_public(allow=("explain_device",))
    with pytest.raises(ValueError) as e:
        env.ai.ask("hello?")
    assert str(e.value) == "ai.err.not_ready"
    assert env.fake.calls == []
    assert env.ai.ready()["ask"] is False and env.ai.ready()["device"] is True


def test_public_allowed_for_explain_only_local_still_answers_ask(env):
    env.public_answers("PUBLIC")
    env.local_answers("LOCAL")
    env.enable_public(allow=("explain_device",))
    env.enable_local()
    res = env.wait(env.ai.ask("hello?"))
    assert res["result"]["text"] == "LOCAL" and res["result"]["route"] == "local"
    assert not any("chat/completions" in p for p in env.fake.paths())


@pytest.mark.parametrize("fn", ("ask", "identify", "report_plain", "suggest_firewall"))
def test_each_function_needs_its_own_permission(env, fn):
    env.enable_public(allow=tuple(f for f in FEATURE_FNS if f != fn))
    assert env.ai.routes(fn) == []
    assert env.ai.routes(next(f for f in FEATURE_FNS if f != fn)) == ["public"]


def test_daily_limit_counts_ask_requests(env):
    env.public_answers("fine")
    public_only(env)
    env.ai.update_settings({"daily_limit": 1})
    assert env.wait(env.ai.ask("one?"))["state"] == "done"
    second = env.wait(env.ai.ask("two?"))
    assert second["state"] == "error" and second["error"] == "ai.err.daily_limit"
    assert env.ai.usage_today()["public"] == 1 and env.ai.usage_today()["limited"] == 1


# ---------------------------------------------------------------------- 5. local gets real data
def test_local_service_gets_real_data_without_placeholders(env):
    env.local_answers("fine")
    env.enable_local()
    res = env.wait(env.ai.ask("Tell me about 192.168.1.20"))
    assert res["result"]["pseudonymised"] is False and res["result"]["route"] == "local"
    user = env.fake.calls[-1].body["messages"][-1]["content"]
    assert "192.168.1.20" in user and "Attic Printer" in user
    assert "[IP-1]" not in user and "[HOST-1]" not in user


# ---------------------------------------------------------------------- 6. time limits
@pytest.fixture()
def hanging(monkeypatch):
    short = Limits(connect=1, first=0.3, done=1, idle=0.3)
    monkeypatch.setattr(providers, "LOCAL_LIMITS", short)
    monkeypatch.setattr(providers, "PUBLIC_LIMITS", short)
    srv = Server(lambda h, stop: stop.wait(10))
    yield srv
    srv.close()


def test_provider_that_never_answers_ends_the_job_with_an_error(env, hanging):
    env.ai.opener = providers._Stream
    env.enable_local(url=hanging.url)
    import time
    t0 = time.monotonic()
    res = env.wait(env.ai.ask("anyone there?"), timeout=8)
    assert res["state"] == "error" and res["error"] in ("ai.err.no_answer", "ai.err.timeout")
    assert time.monotonic() - t0 < 6
    assert env.ai.usage_today()["failed"] >= 1


def test_plain_for_returns_none_when_the_provider_never_answers(env, hanging):
    env.ai.opener = providers._Stream
    env.enable_local(url=hanging.url)
    with_reports(env)
    assert env.ai.plain_for(make_report()) is None


def test_plain_for_returns_none_on_provider_failure(env):
    env.fake.on("/api/chat", 500)
    env.enable_local()
    with_reports(env)
    assert env.ai.plain_for(make_report()) is None


def test_plain_for_none_when_switched_off_or_no_route(env):
    with_reports(env)
    assert env.ai.plain_for(make_report()) is None            # no service
    env.local_answers("text")
    env.enable_local()
    assert env.ai.plain_for(make_report())["text"] == "text"
    env.ai.update_settings({"features": {"report_plain": False}})
    assert env.ai.plain_for(make_report()) is None


# ---------------------------------------------------------------------- 7. identify and hints
def test_identify_parses_json_in_prose_and_code_fences(env):
    env.local_answers('Sure! ```json\n{"kind": "printer", "label": "Laser printer", "reason": "port 631"}\n``` Hope it helps.')
    env.enable_local()
    res = env.wait(env.ai.identify("dev1"))
    out = res["result"]
    assert out["kind"] == "printer" and out["what"] == "Laser printer" and out["reason"] == "port 631"
    assert out["state"] == "open" and out["device"] == "dev1"
    assert env.ai.hints.get("dev1")["state"] == "open"


@pytest.mark.parametrize("kind", ("self", "internet", "spaceship", "", None))
def test_identify_invalid_kinds_become_unknown(env, kind):
    env.local_answers(json.dumps({"kind": kind, "label": "x", "reason": "y"}))
    env.enable_local()
    assert env.wait(env.ai.identify("dev1"))["result"]["kind"] == "unknown"


def test_identify_garbage_answer_is_unknown_with_text_as_reason(env):
    env.local_answers("I really cannot tell.")
    env.enable_local()
    out = env.wait(env.ai.identify("dev1"))["result"]
    assert out["kind"] == "unknown" and "cannot tell" in out["reason"]


def test_identify_prompt_lists_kinds_without_self_and_internet(env):
    env.local_answers("{}")
    env.enable_local()
    env.wait(env.ai.identify("dev1"))
    sent = body_of(env.fake.calls[-1])
    assert "printer" in sent and "unknown" in sent
    assert "self," not in sent and "internet," not in sent


def test_identify_unknown_device_and_self_are_refused(env):
    env.enable_local()
    with pytest.raises(ValueError) as e:
        env.ai.identify("nope")
    assert str(e.value) == "ai.err.subject"
    env.core._devices.append(device("me", is_self=True))
    with pytest.raises(ValueError):
        env.ai.identify("me")


def test_hint_accept_dismiss_forget(env):
    env.local_answers('{"kind": "camera", "label": "c", "reason": "r"}')
    env.enable_local()
    env.wait(env.ai.identify("dev1"))
    assert env.ai.hint_decide("dev1", "accept")["state"] == "accepted"
    assert env.ai.hints.get("dev1")["state"] == "accepted"
    assert env.ai.hint_decide("dev1", "dismiss")["state"] == "dismissed"
    assert env.ai.hint_decide("dev1", "forget") == {"state": "none"}
    assert env.ai.hints.get("dev1") is None
    with pytest.raises(ValueError) as e:
        env.ai.hint_decide("dev1", "accept")
    assert str(e.value) == "ai.err.hint_missing"


def test_accepting_an_unknown_kind_is_refused(env):
    env.local_answers('{"kind": "banana", "label": "c", "reason": "r"}')
    env.enable_local()
    env.wait(env.ai.identify("dev1"))
    with pytest.raises(ValueError) as e:
        env.ai.hint_decide("dev1", "accept")
    assert str(e.value) == "ai.err.hint_unknown"
    assert env.ai.hints.get("dev1")["state"] == "open"


def test_hint_decide_bad_action_and_missing_device(env):
    env.local_answers('{"kind": "camera"}')
    env.enable_local()
    env.wait(env.ai.identify("dev1"))
    with pytest.raises(ValueError) as e:
        env.ai.hint_decide("dev1", "explode")
    assert str(e.value) == "ai.err.hint_action"
    with pytest.raises(ValueError):
        env.ai.hint_decide("ghost", "accept")


def test_hints_view_lists_only_open_hints_of_existing_devices(env):
    env.local_answers('{"kind": "camera", "label": "c", "reason": "r"}')
    env.enable_local()
    env.wait(env.ai.identify("dev1"))
    env.ai.hints.put("ghost", {"state": "open", "kind": "nas", "t": 1})
    assert [h["device"] for h in env.ai.hints_view()] == ["dev1"]
    env.ai.hint_decide("dev1", "dismiss")
    assert env.ai.hints_view() == []


def test_hints_table_is_capped():
    from lanloom.store import Store
    h = features.Hints(Store(":memory:"))
    for i in range(features.HINT_KEEP + 20):
        h.put("d%d" % i, {"state": "open", "kind": "nas", "t": i})
    assert len(h.all()) == features.HINT_KEEP
    assert h.get("d0") is None and h.get("d%d" % (features.HINT_KEEP + 19))


# ---------------------------------------------------------------------- 8. apply_hint
def view_of(**kw):
    v = {"id": "d", "kind": "unknown", "auto_kind": "unknown", "confidence": 0.1, "certainty": "unknown", "reasons": ["mac"]}
    v.update(kw)
    return v


def hint_of(state="accepted", kind="camera"):
    return {"state": state, "kind": kind, "what": "w", "reason": "r", "t": 5}


def test_accepted_hint_fills_an_unknown_kind():
    v = features.apply_hint(view_of(), hint_of())
    assert v["kind"] == "camera" and v["confidence"] == features.HINT_CONFIDENCE == 0.3
    assert v["reasons"] == ["mac", "ai:hint"] and v["certainty"] == "probable"
    assert v["ai_hint"]["state"] == "accepted"


def test_accepted_hint_fills_a_low_confidence_kind():
    v = features.apply_hint(view_of(kind="iot", auto_kind="iot", confidence=0.2), hint_of())
    assert v["kind"] == "camera"


def test_accepted_hint_does_not_override_a_confident_kind():
    v = features.apply_hint(view_of(kind="nas", auto_kind="nas", confidence=0.8), hint_of())
    assert v["kind"] == "nas" and v["confidence"] == 0.8 and "ai:hint" not in v["reasons"]
    assert v["ai_hint"]["state"] == "accepted"


def test_accepted_hint_does_not_override_the_users_kind():
    v = features.apply_hint(view_of(kind="tv", auto_kind="unknown"), hint_of())
    assert v["kind"] == "tv" and "ai:hint" not in v["reasons"]


@pytest.mark.parametrize("flag", ("is_self", "is_gateway"))
def test_accepted_hint_never_changes_self_or_gateway(flag):
    v = features.apply_hint(view_of(**{flag: True}), hint_of())
    assert v["kind"] == "unknown" and "ai:hint" not in v["reasons"]


def test_open_hint_only_adds_the_hint_field():
    v = features.apply_hint(view_of(), hint_of("open"))
    assert v["kind"] == "unknown" and v["confidence"] == 0.1 and v["reasons"] == ["mac"]
    assert v["ai_hint"]["state"] == "open" and v["ai_hint"]["kind"] == "camera"


@pytest.mark.parametrize("h", (hint_of("dismissed"), None, {}))
def test_dismissed_or_missing_hint_adds_nothing(h):
    v = features.apply_hint(view_of(), h)
    assert "ai_hint" not in v and v["kind"] == "unknown"


# ---------------------------------------------------------------------- 9. firewall suggestions
def test_firewall_only_allowed_moves_survive(env):
    st = sample_state()
    dref, nref, possible = ids_refs(st)
    laptop, other, protected = dref["d08"], dref["d07"], dref["d05"]
    work, media, main = nref["n_work"], nref["n_media"], nref["n_main"]
    assert work in possible[laptop] and main not in possible[other] and protected not in possible
    moves = [{"device": laptop, "network": work, "reason": "good"},              # (a) allowed
             {"device": other, "network": main, "reason": "same network"},        # (b) same network
             {"device": protected, "network": work, "reason": "protected"},       # (c) protected
             {"device": "D999", "network": work, "reason": "unknown device"},     # (d) unknown refs
             {"device": laptop, "network": "N99", "reason": "unknown network"},
             {"device": laptop, "network": media, "reason": "duplicate"},         # (e) duplicate
             "garbage", {"device": None}]
    env.local_answers("Here: " + json.dumps({"moves": moves, "summary": "One move."}))
    env.enable_local()
    fw = env.ai.firewall = FakeFirewall(st)
    res = env.wait(env.ai.firewall_suggest())
    assert res["state"] == "done", res
    out = res["result"]
    assert [(m["device"], m["network"]) for m in out["moves"]] == [("d08", "n_work")]
    assert out["moves"][0]["device_title"] == "Office laptop" and out["moves"][0]["network_name"] == "Work"
    assert out["summary"] == "One move."
    assert fw.other_calls == []


def test_firewall_moves_are_capped_at_max_objects(env):
    st = sample_state()
    dref, nref, possible = ids_refs(st)
    assert len(possible) > model.MAX_OBJECTS
    moves = [{"device": d, "network": nets[0], "reason": "r"} for d, nets in possible.items()]
    env.local_answers(json.dumps({"moves": moves, "summary": "many"}))
    env.enable_local()
    env.ai.firewall = FakeFirewall(st)
    out = env.wait(env.ai.firewall_suggest())["result"]
    assert len(out["moves"]) == model.MAX_OBJECTS


def test_firewall_without_state_is_refused(env):
    env.local_answers("{}")
    env.enable_local()
    env.ai.firewall = None
    with pytest.raises(ValueError) as e:
        env.ai.firewall_suggest()
    assert str(e.value) == "ai.err.no_firewall"
    env.ai.firewall = FakeFirewall()
    env.ai.firewall._st = None
    with pytest.raises(ValueError) as e:
        env.ai.firewall_suggest()
    assert str(e.value) == "ai.err.no_firewall"
    assert env.fake.calls == []


def test_firewall_without_possible_moves_is_refused(env):
    st = sample_state()
    st["devices"] = [d for d in st["devices"] if d.get("protected")]
    env.enable_local()
    env.ai.firewall = FakeFirewall(st)
    with pytest.raises(ValueError) as e:
        env.ai.firewall_suggest()
    assert str(e.value) == "ai.err.no_moves"


# ---------------------------------------------------------------------- 10. nightly
@pytest.fixture()
def reports(tmp_path):
    from lanloom import core as core_mod
    from lanloom.crypto import KEY_BYTES, Vault
    from lanloom.store import Store
    store = Store(":memory:")
    cfg = SimpleNamespace(data_dir=Path(tmp_path), host="127.0.0.1", port=0, update_check=False, demo=False, probe=False,
                          release_feed="", image_repo="", cookie_secure="never", trusted_origins=(), trusted_proxies=())
    c = core_mod.Core(cfg, store, Vault(store, b"k" * KEY_BYTES))
    rep = Reports(c, Path(tmp_path), clock=lambda: START)
    rep.cert_check = lambda: []
    c.reports = rep
    store.set_setting("report_cfg", {"time": "06:30", "tz": "Europe/Amsterdam", "keep_days": 30, "link": ""})
    r = make_report()
    r["created"] = START
    rep.save(r)
    return rep, r


def run(coro):
    return asyncio.run(coro)


def test_add_plain_stores_the_paragraph(reports):
    rep, r = reports
    rec = {"text": "All calm.", "lang": "en", "t": 1}
    rep.plain_hook = lambda report: rec
    run(rep.add_plain(r))
    assert r["plain"] == rec
    assert rep.latest()["plain"]["text"] == "All calm."


@pytest.mark.parametrize("hook", (lambda r: None, lambda r: {"text": ""}, lambda r: {}, "raise", None))
def test_add_plain_leaves_it_out_without_text(reports, hook):
    rep, r = reports

    def boom(report):
        raise RuntimeError("model down")
    rep.plain_hook = boom if hook == "raise" else hook
    run(rep.add_plain(r))
    assert "plain" not in r and "plain" not in rep.latest()


def test_add_plain_gives_up_after_plain_wait(reports, monkeypatch):
    import threading
    import time
    from lanloom import nightly
    rep, r = reports
    monkeypatch.setattr(nightly, "PLAIN_WAIT_S", 0.1)
    gate = threading.Event()
    rep.plain_hook = lambda report: (gate.wait(3), {"text": "too late"})[1]
    took = []

    async def timed():
        t0 = time.monotonic()
        await rep.add_plain(r)
        took.append(time.monotonic() - t0)
    try:
        run(timed())              # (asyncio.run itself waits for the worker thread afterwards; add_plain must not)
    finally:
        gate.set()
    assert took[0] < 1.5
    assert "plain" not in r


def test_short_text_includes_plain_only_when_passed():
    from lanloom.nightly.render import short_text
    r = make_report()
    with_plain = short_text(r, "en", None, "", plain="In plain words.")
    without = short_text(r, "en", None, "")
    assert "In plain words." in with_plain["text"] and with_plain["plain"] == "In plain words."
    assert "plain" not in without and "In plain words." not in without["text"]
    assert len(short_text(r, "en", None, "", plain="x" * 2000)["plain"]) == 900


@pytest.mark.parametrize("on,expect", ((True, "Plain paragraph."), (False, None)))
def test_deliver_passes_plain_only_when_delivery_is_allowed(reports, monkeypatch, on, expect):
    rep, r = reports
    r["plain"] = {"text": "Plain paragraph."}
    seen = []
    monkeypatch.setattr(deliver_mod, "send", lambda ch, cfg, sec, msg: (seen.append(msg), (True, ""))[1])
    monkeypatch.setattr(rep, "_secrets", lambda ch: {"url": "https://example.invalid/hook"})
    rep.plain_delivery = lambda: on
    run(rep.deliver(r, only=["webhook"]))
    assert len(seen) == 1 and seen[0].get("plain") == expect
    assert ("Plain paragraph." in seen[0]["text"]) is on


def test_deliver_survives_a_failing_plain_delivery_check(reports, monkeypatch):
    rep, r = reports
    r["plain"] = {"text": "Plain paragraph."}
    seen = []
    monkeypatch.setattr(deliver_mod, "send", lambda ch, cfg, sec, msg: (seen.append(msg), (True, ""))[1])
    monkeypatch.setattr(rep, "_secrets", lambda ch: {"url": "https://example.invalid/hook"})

    def boom():
        raise RuntimeError("x")
    rep.plain_delivery = boom
    run(rep.deliver(r, only=["webhook"]))
    assert seen and "plain" not in seen[0]


def test_webhook_payload_has_plain_only_when_present(monkeypatch):
    posted = []
    monkeypatch.setattr(deliver_mod, "_post", lambda url, data, headers: posted.append(json.loads(data)))
    base = {"title": "t", "headline": "h", "lines": [], "points": 0, "day": DAY, "url": ""}
    deliver_mod.send("webhook", {}, {"url": "https://example.invalid/h"}, dict(base, plain="P"))
    deliver_mod.send("webhook", {}, {"url": "https://example.invalid/h"}, base)
    assert posted[0]["plain"] == "P" and "plain" not in posted[1]


# ---------------------------------------------------------------------- 11. nightly identify
def night_devices():
    return [device("unsure1", ip="192.168.1.31", certainty="unknown", kind="unknown", auto_kind="unknown"),
            device("unsure2", ip="192.168.1.32", certainty="unknown", kind="iot", auto_kind="iot"),
            device("unsure3", ip="192.168.1.33", certainty="low", kind="unknown", auto_kind="unknown"),
            device("sure", ip="192.168.1.34", kind="printer", auto_kind="printer"),
            device("hidden", ip="192.168.1.35", certainty="unknown", kind="unknown", auto_kind="unknown", hidden=1),
            device("me", ip="192.168.1.36", certainty="unknown", kind="unknown", auto_kind="unknown", is_self=True),
            device("gw", ip="192.168.1.37", certainty="unknown", kind="unknown", auto_kind="unknown", is_gateway=True),
            device("chosen", ip="192.168.1.38", certainty="unknown", kind="tv", auto_kind="unknown"),
            device("hinted", ip="192.168.1.39", certainty="unknown", kind="unknown", auto_kind="unknown")]


@pytest.fixture()
def night(env, monkeypatch):
    monkeypatch.setattr("lanloom.ai.manager.time.sleep", lambda s: None)     # the poll loop between jobs
    env.core._devices[:] = night_devices()
    env.local_answers('{"kind": "camera", "label": "c", "reason": "r"}')
    env.enable_local()
    env.ai.hints.put("hinted", {"state": "dismissed", "kind": "nas", "t": 1})
    return env


def asked(env):
    return sorted({b for c in env.fake.calls if c.path.endswith("/api/chat") for b in
                   [s for s in ("192.168.1.3%d" % i for i in range(1, 10)) if s in body_of(c)]})


def test_nightly_identify_is_off_by_default(night):
    assert night.ai.nightly_identify() == 0
    assert night.fake.calls == []


def test_nightly_identify_asks_only_for_unsure_devices(night):
    night.ai.update_settings({"features": {"nightly_identify": True}})
    done = night.ai.nightly_identify()
    assert done == 3
    assert asked(night) == ["192.168.1.31", "192.168.1.32", "192.168.1.33"]
    assert set(night.ai.hints.all()) == {"unsure1", "unsure2", "unsure3", "hinted"}
    assert night.ai.hints.get("hinted")["state"] == "dismissed"


def test_nightly_identify_respects_the_limit(night):
    night.ai.update_settings({"features": {"nightly_identify": True}})
    assert night.ai.nightly_identify(limit=1) == 1
    assert len(asked(night)) == 1
    assert features.NIGHT_MAX == 5


def test_nightly_identify_needs_a_route(env):
    env.ai.update_settings({"features": {"nightly_identify": True}})
    assert env.ai.nightly_identify() == 0 and env.fake.calls == []


def test_nightly_identify_stops_at_the_daily_limit(night):
    night.public_answers('{"kind": "camera"}')
    night.ai.update_settings({"features": {"nightly_identify": True}, "local": {"enabled": False}, "daily_limit": 1,
                              "allow": {"identify": True}})
    night.enable_public(allow=("identify",))
    night.ai.update_settings({"daily_limit": 1})
    assert night.ai.nightly_identify() == 1          # one answered, the second hit the limit and ended the night
    assert len([c for c in night.fake.calls if "chat/completions" in c.path]) == 1


# ---------------------------------------------------------------------- 12. demo mode
@pytest.fixture()
def demo_ready(demo_env):
    env = demo_env
    kind = next(k for k, p in providers.PROVIDERS.items() if p.demo_only and p.scope == "local")
    p = providers.PROVIDERS[kind]
    models = [m for m in (getattr(p, "models", None) or [])] or ["demo"]
    env.ai.update_settings({"local": {"kind": kind, "model": models[0], "enabled": True}})
    assert env.ai.routes("ask") == ["local"]
    return env


def test_demo_ask_has_no_network_and_links_resolve(demo_ready, monkeypatch):
    from lanloom.ai import demo as demo_mod
    env = demo_ready
    env.core.reports = FakeReports(None)
    env.core._devices.append(device("dev2", title="Cellar Nas", status="down"))
    monkeypatch.setattr(demo_mod, "_text", lambda lang, key: "About {name} of {n}: " + key)    # independent of the translations
    res = env.wait(env.ai.ask("Which devices are offline?"))
    assert res["state"] == "done", res
    assert env.fake.calls == []
    out = res["result"]
    assert "ask_offline" in out["text"]
    assert [l["id"] for l in out["links"]] == ["dev2"]
    ids = {d["id"] for d in env.core._devices}
    assert all(l["id"] in ids for l in out["links"] if l["kind"] == "device")


def test_demo_ask_default_answer_links_to_a_listed_device(demo_ready, monkeypatch):
    from lanloom.ai import demo as demo_mod
    env = demo_ready
    monkeypatch.setattr(demo_mod, "_text", lambda lang, key: "About {name}.")
    out = env.wait(env.ai.ask("Hello?"))["result"]
    assert out["links"] and out["links"][0]["id"] == "dev1" and "[D1]" in out["text"]


@pytest.mark.parametrize("lang", ("en", "de"))
def test_demo_answer_texts_exist_in_every_language(lang):
    from lanloom.ai import demo as demo_mod
    keys = ["ai.demo.answer.%s" % k for k in ("ask", "ask_offline", "ask_paths", "report", "report_calm", "identify",
                                              "firewall_reason", "firewall", "firewall_none")]
    keys += ["ai.demo.answer.identify_label.%s" % k for _, k in demo_mod.DEMO_KINDS] + ["ai.demo.answer.identify_label.iot"]
    missing = [k for k in keys if demo_mod._text(lang, k) == k]
    assert missing == []


def test_demo_identify_returns_a_kind_from_the_list(demo_ready):
    env = demo_ready
    env.core._devices[0]["ports"] = [{"port": 554, "service": "rtsp"}]
    res = env.wait(env.ai.identify("dev1"))
    assert res["state"] == "done", res
    assert res["result"]["kind"] in KINDS and res["result"]["kind"] not in ("self", "internet", "unknown")
    assert res["result"]["reason"] and env.fake.calls == []


def test_demo_firewall_returns_valid_moves(demo_ready):
    env = demo_ready
    st = sample_state()
    env.ai.firewall = FakeFirewall(st)
    res = env.wait(env.ai.firewall_suggest())
    assert res["state"] == "done", res
    out = res["result"]
    assert out["summary"] and env.fake.calls == []
    nets = {n["id"] for n in st["networks"]}
    devs = {d["id"]: d for d in st["devices"]}
    for m in out["moves"]:
        assert m["network"] in nets and m["device"] in devs
        assert model.drop_targets(st, devs[m["device"]])[m["network"]] is None
    assert len(out["moves"]) <= model.MAX_OBJECTS


def test_demo_report_paragraph(demo_ready):
    env = demo_ready
    with_reports(env)
    plain = env.ai.plain_for(make_report(points=2))
    assert plain and plain["text"] and env.fake.calls == []
    calm = env.ai.plain_for(make_report(points=0))
    assert calm["text"] and calm["text"] != plain["text"]


# ---------------------------------------------------------------------- 13. nightly section
def test_report_section_has_hint_item_for_new_open_hints(env):
    env.enable_local()
    ctx = SimpleNamespace(core=SimpleNamespace(ai=env.ai), since=START - 3600, until=START + 3600)
    assert not any(i["id"] == "ai-hints" for i in ai_report._build(ctx)["items"])
    env.ai.hints.put("dev1", {"state": "open", "kind": "camera", "t": START})
    env.ai.hints.put("old", {"state": "open", "kind": "camera", "t": START - 99999})
    env.ai.hints.put("done", {"state": "accepted", "kind": "camera", "t": START})
    sec = ai_report._build(ctx)
    hint = next(i for i in sec["items"] if i["id"] == "ai-hints")
    assert hint["vars"] == {"n": 1} and hint["point"] is False
