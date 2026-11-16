"""AI manager: settings, secrets, routes, limits, privacy on the way out, jobs."""
import json
import threading

import pytest

from lanloom.ai import manager as manager_mod, privacy, providers
from lanloom.ai.providers import AIError

from .conftest import LOCAL_URL
from .fakes import KEY, FakeCore, device, ndjson, sse

OUTSIDE = providers.ipaddress.IPv4Address(0x64400001)      # shared space: not private, never contacted


def sent_user(call):
    return call.body["messages"][1]["content"]


# ---------------------------------------------------------------------- defaults and settings
def test_everything_is_off_by_default(env):
    assert env.ai.ready() == {"device": False, "finding": False, "rule": False}
    for fn in manager_mod.FUNCTIONS:
        assert env.ai.routes(fn) == []
    s = env.ai.settings()
    assert s["local"]["enabled"] is False and s["public"]["enabled"] is False
    assert all(v is False for v in s["allow"].values())
    with pytest.raises(ValueError) as e:
        env.ai.explain("device", {"id": "dev1"}, "en")
    assert str(e.value) == "ai.err.not_ready"
    assert env.fake.calls == []


def test_public_needs_a_key_to_switch_on(env):
    with pytest.raises(ValueError) as e:
        env.ai.update_settings({"public": {"kind": "openai", "model": "gpt-4o-mini", "enabled": True}})
    assert str(e.value) == "ai.err.key_missing"
    assert env.ai.settings()["public"]["enabled"] is False
    env.ai.update_settings({"public": {"kind": "openai", "model": "gpt-4o-mini", "enabled": True}}, {"public_key": KEY})
    assert env.ai.settings()["public"]["enabled"] is True


def test_local_needs_an_address_to_switch_on(env):
    with pytest.raises(ValueError) as e:
        env.ai.update_settings({"local": {"kind": "ollama", "enabled": True}})
    assert str(e.value) == "ai.err.url"


def test_key_is_never_returned_or_stored_in_clear(env):
    env.enable_public()
    pub = env.ai.public_settings()
    assert pub["settings"]["public"]["has_key"] is True
    for doc in (pub, env.ai.settings(), env.store.get_setting("ai"), env.ai.usage_today(), env.ai.setup_state()):
        assert KEY not in json.dumps(doc)
    raw = env.store.all("SELECT * FROM settings") if hasattr(env.store, "all") else []
    assert KEY not in json.dumps([tuple(map(str, r)) for r in raw])
    cipher = env.store.one("SELECT ciphertext FROM secrets WHERE name=?", (env.ai._key_name("public", "openai"),))
    assert cipher is not None and KEY.encode() not in bytes(cipher["ciphertext"])
    assert env.vault.get(env.ai._key_name("public", "openai")) == KEY
    assert "key" not in env.store.get_setting("ai")["public"]


def test_key_is_kept_per_provider_and_can_be_cleared(env):
    env.enable_public()
    assert env.ai.public_settings()["settings"]["public"]["has_key"] is True
    env.ai.update_settings({"public": {"kind": "anthropic", "enabled": False}})
    assert env.ai.public_settings()["settings"]["public"]["has_key"] is False       # the other provider's key is not reused
    env.ai.update_settings({"public": {"kind": "openai", "enabled": False}}, {"clear_public_key": True})
    assert env.ai.public_settings()["settings"]["public"]["has_key"] is False
    assert not env.vault.has(env.ai._key_name("public", "openai"))


def test_cleared_key_switches_the_route_off(env):
    env.enable_public()
    assert env.ai.routes("explain_device") == ["public"]
    env.vault.delete(env.ai._key_name("public", "openai"))
    assert env.ai.routes("explain_device") == [] and env.ai.ready()["device"] is False


@pytest.mark.parametrize("patch,err", [
    ({"local": {"kind": "openai"}}, "ai.err.provider"), ({"public": {"kind": "ollama"}}, "ai.err.provider"),
    ({"local": {"kind": "demo"}}, "ai.err.provider"), ({"public": {"kind": "nope"}}, "ai.err.provider"),
    ({"local": {"url": "ftp://192.168.1.9"}}, "ai.err.url"), ({"local": {"url": "http://u:p@192.168.1.9"}}, "ai.err.url"),
    ({"daily_limit": "many"}, "ai.err.limit")])
def test_settings_validation(env, patch, err):
    with pytest.raises(ValueError) as e:
        env.ai.update_settings(patch)
    assert str(e.value) == err


@pytest.mark.parametrize("value", ["has space", "x" * 401, "tab\there", "new\nline"])
def test_key_format_is_checked(env, value):
    with pytest.raises(ValueError) as e:
        env.ai.update_settings({"public": {"kind": "openai"}}, {"public_key": value})
    assert str(e.value) == "ai.err.key_format" and value.strip() not in str(e.value)


def test_settings_clamps_and_ignores_unknowns(env):
    s = env.ai.update_settings({"daily_limit": 99999, "order": "weird", "allow": {"explain_device": 1, "nonsense": True}})["settings"]
    assert s["daily_limit"] == 1000 and s["order"] == "local_first" and "nonsense" not in s["allow"] and s["allow"]["explain_device"] is True
    assert env.ai.update_settings({"daily_limit": -5})["settings"]["daily_limit"] == 0
    assert env.ai.update_settings({"order": "public_first"})["settings"]["order"] == "public_first"


def test_changing_the_provider_resets_the_model(env):
    env.enable_local()
    s = env.ai.update_settings({"local": {"kind": "openai_local", "url": "http://192.168.1.9:1234"}})["settings"]["local"]
    assert s["model"] == "" and s["url"] == "http://192.168.1.9:1234/v1"


def test_public_fixed_url_is_not_stored(env):
    env.enable_public()
    s = env.ai.update_settings({"public": {"url": "http://evil.example/v1"}})["settings"]["public"]
    assert s["url"] == ""


# ---------------------------------------------------------------------- permissions and routes
def test_function_without_allow_does_not_use_the_cloud(env):
    env.public_answers()
    env.enable_public(allow=())
    assert env.ai.routes("explain_device") == []
    with pytest.raises(ValueError):
        env.ai.explain("device", {"id": "dev1"}, "en")
    env.ai.update_settings({"allow": {"explain_device": True}})
    assert env.ai.routes("explain_device") == ["public"]
    assert env.ai.routes("explain_rule") == [] and env.ai.ready() == {"device": True, "finding": False, "rule": False}
    assert env.fake.calls == []


def test_local_needs_no_allow(env):
    env.local_answers()
    env.enable_local()
    assert env.ai.routes("explain_rule") == ["local"]
    job = env.explain()
    assert job["state"] == "done" and job["result"]["route"] == "local" and job["result"]["pseudonymised"] is False


def test_order_and_fallback(env):
    env.enable_local()
    env.enable_public()
    assert env.ai.routes("explain_device") == ["local", "public"]
    env.ai.update_settings({"order": "public_first"})
    assert env.ai.routes("explain_device") == ["public", "local"]

    # local first, local is down -> public answers
    env.ai.update_settings({"order": "local_first"})
    env.fake.on("/api/chat", AIError("ai.err.unreachable"))
    env.public_answers("From the cloud.")
    job = env.explain()
    assert job["state"] == "done" and job["result"]["route"] == "public" and job["result"]["text"] == "From the cloud."
    assert env.ai.usage_today()["failed"] == 1 and env.ai.usage_today()["public"] == 1

    # public first, public is down -> local answers
    env.ai.update_settings({"order": "public_first"})
    env.fake.routes.clear()
    env.fake.on("/chat/completions", AIError("ai.err.unreachable"))
    env.local_answers("From home.")
    job = env.explain()
    assert job["result"]["route"] == "local" and job["result"]["text"] == "From home."


def test_all_routes_failing_reports_the_first_error(env):
    env.enable_local()
    env.enable_public()
    env.fake.on("/api/chat", AIError("ai.err.unreachable"))
    env.fake.on("/chat/completions", 429)
    job = env.explain()
    assert job["state"] == "error" and job["error"] == "ai.err.unreachable"


def test_daily_limit(env):
    env.public_answers()
    env.enable_public()
    env.ai.update_settings({"daily_limit": 2})
    results = [env.explain() for _ in range(3)]
    assert [r["state"] for r in results] == ["done", "done", "error"]
    assert results[2]["error"] == "ai.err.daily_limit"
    u = env.ai.usage_today()
    assert u["public"] == 2 and u["limited"] == 1 and u["limit"] == 2
    assert len(env.fake.calls) == 2                       # the third never left
    env.clock.advance(86400 + 5)                          # next day: counters start again
    assert env.ai.usage_today()["public"] == 0
    assert env.explain()["state"] == "done"


def test_daily_limit_zero_means_no_cloud(env):
    env.enable_public()
    env.ai.update_settings({"daily_limit": 0})
    assert env.ai.routes("explain_device") == []


def test_usage_is_pruned(env):
    for i in range(30):
        env.clock.advance(86400)
        env.ai._count("local")
    assert len(env.store.get_setting("ai_usage")) == manager_mod.USAGE_KEEP_DAYS


# ---------------------------------------------------------------------- privacy on the way out
def test_preview_is_exactly_what_the_service_receives(env):
    env.public_answers("[HOST-1] looks fine.")
    env.enable_public()
    pv = env.ai.preview("device", {"id": "dev1"}, "en")
    assert [r["scope"] for r in pv["routes"]] == ["public"] and pv["allowed_public"] and pv["public_ready"]
    route = pv["routes"][0]
    assert route["pseudonymise"] is True and route["blocked"] is False and route["hidden"]
    job = env.explain()
    assert job["state"] == "done"
    call = env.fake.calls[-1]
    assert call.body["messages"][0]["content"] == route["system"]
    assert call.body["messages"][1]["content"] == route["user"]
    for real in ("192.168.1.20", "aa:bb:cc:dd:ee:01", "Attic Printer", "attic-printer.lan"):
        assert real not in route["user"] and real not in json.dumps(pv)
    assert "[IP-" in route["user"] and "[MAC-" in route["user"] and "[HOST-" in route["user"]
    assert all(set(h) == {"placeholder", "kind"} for h in route["hidden"])


def test_answer_is_translated_back(env):
    env.enable_public()
    env.fake.on("/chat/completions", lambda call: sse({"choices": [{"delta": {"content": "Check [IP-1] and [HOST-1]. Ignore [IP-9]."}}]}))
    job = env.explain()
    sent = sent_user(env.fake.calls[-1])
    ip_ph = sent.split("Address: ")[1].split("\n")[0]
    assert ip_ph == "[IP-1]"
    text = job["result"]["text"]
    assert "192.168.1.20" in text and "[HOST-1]" not in text and ("attic-printer.lan" in text or "Attic Printer" in text) and "[IP-9]" in text
    assert job["result"]["pseudonymised"] is True


def test_local_service_gets_the_real_data(env):
    env.local_answers()
    env.enable_local()
    pv = env.ai.preview("device", {"id": "dev1"}, "en")
    assert pv["routes"][0]["pseudonymise"] is False and "192.168.1.20" in pv["routes"][0]["user"]
    env.explain()
    msg = sent_user(env.fake.calls[-1])
    assert "192.168.1.20" in msg and "aa:bb:cc:dd:ee:01" in msg and "[IP-" not in msg


def test_local_name_that_leads_outside_is_treated_as_public(env, monkeypatch):
    monkeypatch.setattr(providers, "host_is_local", lambda url, *a, **k: False)
    env.local_answers("Fine [IP-1].")
    env.enable_local()
    # without permission it is not used at all, like a public service
    assert env.ai.routes("explain_device") == [] and env.ai.ready()["device"] is False
    env.ai.update_settings({"allow": {"explain_device": True}})
    assert env.ai.routes("explain_device") == ["local"]
    pv = env.ai.preview("device", {"id": "dev1"}, "en")
    assert pv["routes"][0]["pseudonymise"] is True and "192.168.1.20" not in pv["routes"][0]["user"]
    job = env.explain()
    assert job["state"] == "done" and job["result"]["pseudonymised"] is True and "192.168.1.20" in job["result"]["text"]
    assert "192.168.1.20" not in sent_user(env.fake.calls[-1]) and "aa:bb:cc" not in sent_user(env.fake.calls[-1])
    u = env.ai.usage_today()
    assert u["public"] == 1 and u["local"] == 0                  # it counts against the daily limit
    env.ai.update_settings({"daily_limit": 1})
    assert env.explain()["error"] == "ai.err.daily_limit"


def test_local_name_inside_the_network_stays_local(env, monkeypatch):
    monkeypatch.setattr(providers, "host_is_local", lambda url, *a, **k: True)
    env.local_answers()
    env.enable_local()
    assert env.ai.routes("explain_device") == ["local"]
    assert env.ai.preview("device", {"id": "dev1"})["routes"][0]["pseudonymise"] is False


def test_leak_guard_stops_everything(env, monkeypatch):
    env.public_answers()
    env.enable_public()
    monkeypatch.setattr(privacy, "leaks", lambda text: ["IP"])
    pv = env.ai.preview("device", {"id": "dev1"}, "en")
    assert pv["routes"][0]["blocked"] is True
    job = env.explain()
    assert job["state"] == "error" and job["error"] == "ai.err.privacy"
    assert env.fake.calls == []
    u = env.ai.usage_today()
    assert u["failed"] == 1 and u["public"] == 0


def test_leak_guard_lets_the_local_route_answer(env, monkeypatch):
    env.enable_local()
    env.enable_public()
    env.local_answers("Home answer.")
    env.ai.update_settings({"order": "public_first"})
    monkeypatch.setattr(privacy, "leaks", lambda text: ["MAC"])
    job = env.explain()
    assert job["state"] == "done" and job["result"]["route"] == "local"
    assert [c.path for c in env.fake.calls] == ["/api/chat"]


def test_the_real_text_never_leaves_to_a_public_service_for_random_devices(env):
    env.public_answers()
    env.enable_public()
    env.core._devices[:] = [device("d%d" % i, "10.%d.%d.%d" % (i, i + 1, i + 2), title="Box %d" % i, mac="de:ad:be:ef:00:%02x" % i,
                                   hostnames=["box%d.local" % i]) for i in range(1, 6)]
    for i in range(1, 6):
        env.explain({"id": "d%d" % i})
        msg = sent_user(env.fake.calls[-1])
        assert privacy.leaks(msg) == [] and ("10.%d." % i) not in msg and "de:ad" not in msg and "box%d" % i not in msg.lower()


# ---------------------------------------------------------------------- jobs
def test_explain_answers_before_the_model_is_done(env):
    started, release = threading.Event(), threading.Event()

    def slow(call):
        started.set()
        assert release.wait(10)
        return ndjson({"message": {"content": "Done."}, "done": True})
    env.fake.on("/api/chat", slow)
    env.enable_local()
    job = env.ai.explain("device", {"id": "dev1"}, "en")
    assert job["state"] in ("queued", "running", "sending") and job["result"] is None and job["error"] is None
    assert started.wait(5)
    mid = env.ai.job_view(job["id"])
    assert mid["state"] == "sending" and mid["result"] is None
    release.set()
    done = env.wait(job)
    assert done["state"] == "done" and done["result"]["text"] == "Done."


def test_job_errors_never_carry_details(env):
    env.enable_local()
    env.fake.on("/api/chat", lambda call: 1 / 0)
    job = env.explain()
    assert job["state"] == "error" and job["error"] == "ai.err.internal"


def test_unknown_subject_and_items(env):
    env.local_answers()
    env.enable_local()
    for subject, ref in (("nonsense", {}), ("device", {"id": "missing"}), ("finding", {"day": "2020-01-01", "item": "x"}), ("rule", {"source": "k", "id": "1"})):
        with pytest.raises(ValueError) as e:
            env.ai.explain(subject, ref, "en")
        assert str(e.value) == "ai.err.subject"
    assert env.ai.job_view("nope") is None and env.ai.job_view(None) is None


def test_job_table_is_bounded(env):
    env.local_answers()
    env.enable_local()
    ids = [env.ai.explain("device", {"id": "dev1"}, "en")["id"] for _ in range(manager_mod.MAX_JOBS + 6)]
    for i in ids[-6:]:
        env.wait(i)
    assert len(env.ai._jobs) <= manager_mod.MAX_JOBS


def test_language_goes_into_the_prompt(env):
    env.local_answers()
    env.enable_local()
    env.wait(env.ai.explain("device", {"id": "dev1"}, "de"))
    assert "German" in env.fake.calls[-1].body["messages"][0]["content"]
    env.wait(env.ai.explain("device", {"id": "dev1"}, "xx"))
    assert "English" in env.fake.calls[-1].body["messages"][0]["content"]


# ---------------------------------------------------------------------- models and test
def test_models_with_a_typed_key_does_not_store_it(env):
    env.fake.on("/models", {"data": [{"id": "gpt-4.1-mini"}, {"id": "gpt-4o"}]})
    res = env.ai.models("public", {"kind": "openai", "key": KEY})
    assert res["ok"] and res["models"] == ["gpt-4.1-mini", "gpt-4o"] and res["recommended"] == "gpt-4.1-mini"
    assert KEY not in json.dumps(res)
    assert not env.vault.has(env.ai._key_name("public", "openai"))


def test_models_errors_are_reported_not_raised(env):
    env.fake.on("/models", 401)
    res = env.ai.models("public", {"kind": "openai", "key": KEY})
    assert res == {"ok": False, "error": "ai.err.auth", "models": [], "recommended": ""}
    assert env.ai.models("public", {"kind": "openai"})["error"] == "ai.err.key_missing"
    with pytest.raises(ValueError):
        env.ai.models("nowhere", {})
    with pytest.raises(ValueError):
        env.ai.models("local", {"kind": "openai"})


def test_models_empty_list(env):
    env.fake.on("/api/tags", {"models": []})
    res = env.ai.models("local", {"kind": "ollama", "url": LOCAL_URL})
    assert res["ok"] is True and res["models"] == [] and res["recommended"] == ""


def test_connection_test_is_a_real_question(env):
    env.local_answers("OK")
    job = env.ai.test("local", {"kind": "ollama", "url": LOCAL_URL, "model": "llama3.1:8b"})
    done = env.wait(job)
    assert done["state"] == "done" and done["result"]["text"] == "OK"
    assert "OK" in sent_user(env.fake.calls[-1])
    with pytest.raises(ValueError) as e:
        env.ai.test("local", {"kind": "ollama", "url": LOCAL_URL})
    assert str(e.value) == "ai.err.model_missing"
    with pytest.raises(AIError):
        env.ai.test("public", {"kind": "openai", "model": "m"})          # no key: refused before the acknowledgement


# ---------------------------------------------------------------------- pairing
def test_pairing_code_and_installer(env):
    p = env.ai.new_pairing()
    code = p["code"]
    assert len(code) == 9 and code[4] == "-" and {o["id"] for o in p["os"]} == {"linux", "mac", "windows"}
    name, text = env.ai.installer("linux", code, "192.168.1.5:7380", "http")
    assert name.endswith(".sh") and code in text and "http://192.168.1.5:7380" in text
    env.ai.installer("linux", code.lower().replace("-", ""), "192.168.1.5:7380", "http")       # typed sloppily
    assert env.ai.installer("windows", code, "[::1]:7380", "https")[0].endswith(".ps1")
    for bad in ("AAAA-BBBB", "", None, "x"):
        with pytest.raises(PermissionError):
            env.ai.installer("linux", bad, "192.168.1.5:7380", "http")
    with pytest.raises(ValueError) as e:
        env.ai.installer("linux", code, "evil.example/x y", "http")
    assert str(e.value) == "ai.err.host_header"
    with pytest.raises(ValueError):
        env.ai.installer("beos", code, "192.168.1.5", "http")


def test_pairing_code_expires(env):
    code = env.ai.new_pairing()["code"]
    env.clock.advance(manager_mod.PAIR_VALID_S - 5)
    env.ai.installer("linux", code, "192.168.1.5", "http")
    env.clock.advance(10)
    with pytest.raises(PermissionError):
        env.ai.installer("linux", code, "192.168.1.5", "http")
    with pytest.raises(PermissionError):
        env.ai.pair_hello(code, {})
    with pytest.raises(PermissionError):
        env.ai.pair(code, "192.168.1.50", {"model": "m"})


def test_a_new_code_replaces_the_old_one(env):
    old = env.ai.new_pairing()["code"]
    new = env.ai.new_pairing()["code"]
    with pytest.raises(PermissionError):
        env.ai.installer("linux", old, "192.168.1.5", "http")
    env.ai.installer("linux", new, "192.168.1.5", "http")
    assert old not in json.dumps(env.store.get_setting("ai_pairing"))            # only a hash is stored


def test_pair_hello(env):
    code = env.ai.new_pairing()["code"]
    with pytest.raises(PermissionError):
        env.ai.pair_hello("AAAA-BBBB", {})
    out = env.ai.pair_hello(code, {"ram_gb": 16, "cores": 8, "gpu": "", "arch": "x86_64"})
    assert out["ok"] and out["model"] == "llama3.1:8b" and out["speed"] == "ok" and out["port"] == 11434
    assert env.ai.pair_hello(code, "junk")["ok"] is True


def pairing_service(env, model="llama3.1:8b", tags=None):
    env.fake.on("/api/version", {"version": "0.5.1"})
    env.fake.on("/api/tags", {"models": [{"name": m} for m in (tags if tags is not None else [model])]})
    env.local_answers("OK")


def test_pair_from_a_public_address_is_refused(env):
    code = env.ai.new_pairing()["code"]
    with pytest.raises(ValueError) as e:
        env.ai.pair(code, str(OUTSIDE), {"model": "llama3.1:8b"})
    assert str(e.value) == "ai.err.peer"
    with pytest.raises(PermissionError):
        env.ai.pair("AAAA-BBBB", "192.168.1.50", {"model": "llama3.1:8b"})
    assert env.fake.calls == []


@pytest.mark.parametrize("data,err", [({"model": "llama3.1:8b", "port": 80}, "ai.err.port"), ({"model": "m", "port": "x"}, "ai.err.port"),
                                      ({"model": ""}, "ai.err.model_missing"), ({"model": "bad model;rm"}, "ai.err.model_missing")])
def test_pair_checks_its_input(env, data, err):
    code = env.ai.new_pairing()["code"]
    with pytest.raises(ValueError) as e:
        env.ai.pair(code, "192.168.1.50", data)
    assert str(e.value) == err


def test_pairing_succeeds_and_the_code_is_used_up(env):
    pairing_service(env)
    code = env.ai.new_pairing()["code"]
    job = env.ai.pair(code, "192.168.1.50", {"model": "llama3.1:8b", "port": 11434})
    done = env.wait(job)
    assert done["state"] == "done" and done["result"]["url"] == "http://192.168.1.50:11434"
    loc = env.ai.settings()["local"]
    assert loc["enabled"] and loc["origin"] == "paired" and loc["model"] == "llama3.1:8b" and loc["url"] == "http://192.168.1.50:11434"
    assert env.ai.ready()["device"] is True
    # the script follows the job with the same code, anyone else does not
    st = env.ai.pair_status(code, job["id"])
    assert st["state"] == "done" and st["result"]["model"] == "llama3.1:8b" and "url" not in st["result"]
    assert env.ai.pair_status("AAAA-BBBB", job["id"]) is None
    assert env.ai.pair_status(code, "nope") is None and env.ai.pair_status("", job["id"]) is None
    # one-time: nothing works with it any more
    for call in (lambda: env.ai.installer("linux", code, "192.168.1.5", "http"), lambda: env.ai.pair_hello(code, {}),
                 lambda: env.ai.pair(code, "192.168.1.50", {"model": "llama3.1:8b"})):
        with pytest.raises(PermissionError):
            call()


def test_pairing_job_is_not_a_public_job_status(env):
    pairing_service(env)
    code = env.ai.new_pairing()["code"]
    job = env.ai.pair(code, "192.168.1.50", {"model": "llama3.1:8b"})
    env.wait(job)
    assert env.ai._jobs[job["id"]]["kind"] == "pair"


def test_failed_pairing_keeps_the_code_and_the_old_setup(env):
    code = env.ai.new_pairing()["code"]
    # nothing answers on the other computer
    done = env.wait(env.ai.pair(code, "192.168.1.50", {"model": "llama3.1:8b"}))
    assert done["state"] == "error" and done["error"] == "ai.err.lan_unreachable"
    # the model is not there
    pairing_service(env, tags=["other:1b"])
    done = env.wait(env.ai.pair(code, "192.168.1.50", {"model": "llama3.1:8b"}))
    assert done["error"] == "ai.err.model"
    # the model does not answer
    pairing_service(env)
    env.fake.routes[:] = [r for r in env.fake.routes if r[0] != "/api/chat"]
    env.fake.routes.insert(0, ("/api/chat", None, ndjson({"message": {"content": ""}, "done": True})))
    done = env.wait(env.ai.pair(code, "192.168.1.50", {"model": "llama3.1:8b"}))
    assert done["state"] == "error"
    assert env.ai.settings()["local"]["enabled"] is False
    env.ai.installer("linux", code, "192.168.1.5", "http")                       # still usable for another try


def test_pairing_job_with_a_fast_service_does_not_revive_the_code(env):
    pairing_service(env)
    for _ in range(15):
        code = env.ai.new_pairing()["code"]
        job = env.ai.pair(code, "192.168.1.50", {"model": "llama3.1:8b"})
        env.wait(job)
        with pytest.raises(PermissionError):
            env.ai.pair_hello(code, {})


# ---------------------------------------------------------------------- discovery
def many_devices(n, kind="server"):
    return [device("s%02d" % i, "192.168.2.%d" % (i + 10), title="Server %02d" % i, kind=kind, status="up") for i in range(n)]


def hosts_of(fake):
    import urllib.parse
    return {urllib.parse.urlsplit(c.url).hostname for c in fake.calls}


def test_discover_asks_only_known_addresses(env):
    env.core._devices[:] = many_devices(40) + [device("pub", str(OUTSIDE), kind="nas"), device("printer", "192.168.3.3", kind="printer"),
                                                device("hid", "192.168.4.4", kind="server", hidden=True)]
    env.fake.on("/api/version", {"version": "0.5.1"}, method="GET")
    env.fake.on("/api/tags", {"models": [{"name": "llama3.1:8b"}]})
    ids = [d["id"] for d in env.core._devices]
    res = env.ai.discover(ids)
    inventory = {d["ip"] for d in env.core._devices if d["id"] not in ("pub",)}
    basics = {"127.0.0.1", "lanloom-ai", "host.docker.internal"}
    asked = hosts_of(env.fake)
    assert asked <= basics | inventory and str(OUTSIDE) not in asked
    assert {urllib_path for urllib_path in (c.path for c in env.fake.calls)} <= {"/api/version", "/api/tags"}
    assert all(c.url.split("://")[1].split("/")[0].endswith(":11434") for c in env.fake.calls)
    assert res["probed"] == len(basics) + 24 and len(asked - basics) <= 24
    assert all(c.method == "GET" for c in env.fake.calls)


def test_discover_without_ids_uses_suggestions_only(env):
    env.core._devices[:] = many_devices(10) + [device("printer", "192.168.3.3", kind="printer")]
    env.fake.on("/api/version", {"version": "0.5.1"})
    env.fake.on("/api/tags", {"models": []})
    res = env.ai.discover(None)
    assert res["probed"] == 3 + 6 and "192.168.3.3" not in hosts_of(env.fake)
    assert len(env.ai.suggestions()) == 6
    found = {f["url"]: f for f in res["found"]}
    assert found and all(f["models"] == [] and f["version"] == "0.5.1" for f in found.values())
    assert env.ai.discover("junk")["probed"] == 9


def test_discover_finds_only_what_answers_and_marks_this_host(env):
    env.core._devices[:] = [device("s1", "192.168.2.11", kind="nas", status="up", title="Box")]

    def only_one(call):
        if "192.168.2.11" in call.url or "127.0.0.1" in call.url:
            return {"version": "0.5.1"}
        return AIError("ai.err.unreachable")
    env.fake.on("/api/version", only_one)
    env.fake.on("/api/tags", {"models": [{"name": "qwen2.5:7b"}]})
    res = env.ai.discover(["s1"])
    by = {f["url"]: f for f in res["found"]}
    assert set(by) == {"http://127.0.0.1:11434", "http://192.168.2.11:11434"}
    assert by["http://127.0.0.1:11434"]["this_host"] is True and by["http://192.168.2.11:11434"]["this_host"] is False
    assert by["http://192.168.2.11:11434"]["title"] == "Box" and by["http://192.168.2.11:11434"]["models"] == ["qwen2.5:7b"]


def test_discover_brackets_ipv6(env):
    env.core._devices[:] = [device("s1", "fe80::5", kind="server", status="up")]
    env.fake.on("/api/version", AIError("ai.err.unreachable"))
    env.ai.discover(["s1"])
    assert any("[fe80::5]:11434" in c.url for c in env.fake.calls)


def test_suggestions_ranking(env):
    env.core._devices[:] = [device("a", "192.168.2.2", kind="nas", status="up", title="Nas"),
                            device("b", "192.168.2.3", kind="server", status="up", title="Srv"),
                            device("c", "192.168.2.4", kind="server", status="down", title="Down"),
                            device("d", str(OUTSIDE), kind="server", status="up"), device("e", "192.168.2.5", kind="tv", status="up"),
                            device("f", "192.168.2.6", kind="server", status="up", is_self=True)]
    assert [s["id"] for s in env.ai.suggestions()] == ["b", "a", "c"]


def test_use_existing_only_takes_what_discovery_found(env):
    with pytest.raises(ValueError) as e:
        env.ai.use_existing("http://192.168.9.9:11434", "m")
    assert str(e.value) == "ai.err.not_found"
    with pytest.raises(ValueError) as e:
        env.ai.use_existing("ftp://x", "m")
    assert str(e.value) == "ai.err.url"


# ---------------------------------------------------------------------- local service: sidecar
@pytest.fixture()
def host16(monkeypatch):
    from lanloom.ai import hardware
    monkeypatch.setattr(hardware, "this_host", lambda: {"ram_gb": 16.0, "cores": 8, "gpu": "", "arch": "x86_64"})


def write_sidecar(env, **extra):
    d = env.data / "update"
    d.mkdir(exist_ok=True)
    (d / "sidecar.json").write_text(json.dumps(dict({"version": "1", "t": env.clock()}, **extra)))


def test_install_needs_a_live_sidecar(env, host16):
    with pytest.raises(ValueError) as e:
        env.ai.install_local()
    assert str(e.value) == "ai.err.no_sidecar"
    write_sidecar(env)
    env.clock.advance(500)                                  # sidecar has not reported for too long
    with pytest.raises(ValueError):
        env.ai.install_local()
    assert not (env.data / "update" / "ai-request.json").exists()


def test_install_writes_a_minimal_request(env, host16, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(manager_mod.AI, "_setup_run", lambda self, *a, **k: release.wait(5))
    write_sidecar(env)
    st = env.ai.install_local()
    req_file = env.data / "update" / "ai-request.json"
    req = json.loads(req_file.read_text())
    assert set(req) == {"action", "nonce", "requested"} and req["action"] == "install"
    assert len(req["nonce"]) == 32 and int(req["nonce"], 16) >= 0 and abs(req["requested"] - __import__("time").time()) < 60
    assert (req_file.stat().st_mode & 0o777) == 0o600
    assert st["state"] == "installing" and env.ai.setup["nonce"] == req["nonce"]
    assert not list((env.data / "update").glob(".ai-request.*"))
    with pytest.raises(ValueError) as e:
        env.ai.install_local()
    assert str(e.value) == "ai.err.busy"
    release.set()
    env.ai._setup_thread.join(5)


def test_install_refuses_a_too_small_computer(env, monkeypatch):
    from lanloom.ai import hardware
    monkeypatch.setattr(hardware, "this_host", lambda: {"ram_gb": 2.0, "cores": 4, "gpu": "", "arch": "x86_64"})
    write_sidecar(env)
    with pytest.raises(ValueError) as e:
        env.ai.install_local()
    assert str(e.value) == "ai.err.host_too_small" and not (env.data / "update" / "ai-request.json").exists()


def test_remove_writes_a_request_and_switches_the_local_route_off(env, monkeypatch):
    env.enable_local(origin="detected")
    write_sidecar(env)
    env.ai.update_settings({"local": {"origin": "detected"}})
    assert env.ai.remove_local()["state"] == "idle"
    assert json.loads((env.data / "update" / "ai-request.json").read_text())["action"] == "remove"
    assert env.ai.settings()["local"]["enabled"] is True        # only a service the sidecar made is switched off
    s = env.ai.settings()
    s["local"]["origin"] = "sidecar"
    env.store.set_setting("ai", {k: s[k] for k in manager_mod.DEFAULTS})
    env.ai.remove_local()
    assert env.ai.settings()["local"]["enabled"] is False


def run_setup(env, source="sidecar", url=None, model="llama3.1:8b", tick=lambda: None):
    def sleep(s):
        env.clock.advance(max(s, 1))
        tick()
    env.ai._setup_run(source, url, model, poll=0, sleep=sleep)
    return env.ai.setup


def sidecar_status(env, nonce, **extra):
    (env.data / "update").mkdir(exist_ok=True)
    (env.data / "update" / "ai-status.json").write_text(json.dumps(dict({"state": "running", "url": "http://127.0.0.1:11434", "nonce": nonce}, **extra)))


def test_setup_run_reaches_ready_only_after_the_model_answered(env):
    nonce = "n" * 32
    env.ai.setup = {"state": "installing", "nonce": nonce}
    sidecar_status(env, nonce)
    seen = {}
    env.fake.on("/api/version", {"version": "0.5.1"})
    env.fake.on("/api/tags", {"models": []})
    env.fake.on("/api/pull", ndjson({"status": "pulling a", "completed": 50, "total": 100}, {"status": "pulling a", "completed": 100, "total": 100},
                                    {"status": "success"}))

    def chat(call):
        seen["state"] = env.ai.setup["state"]
        seen["enabled"] = env.ai.settings()["local"]["enabled"]
        return ndjson({"message": {"content": "OK"}, "done": True})
    env.fake.on("/api/chat", chat)
    st = run_setup(env)
    assert seen == {"state": "testing", "enabled": False}
    assert st["state"] == "ready" and st["answer"] == "OK" and st["completed"] == 100 and st["total"] == 100
    loc = env.ai.settings()["local"]
    assert loc == {"enabled": True, "kind": "ollama", "url": "http://127.0.0.1:11434", "model": "llama3.1:8b", "origin": "sidecar"}
    paths = [c.path for c in env.fake.calls]
    assert paths.index("/api/pull") < paths.index("/api/chat")
    pull = next(c for c in env.fake.calls if c.path == "/api/pull")
    assert pull.body["model"] == "llama3.1:8b"


def test_setup_run_skips_the_download_when_the_model_is_there(env):
    nonce = "a" * 32
    env.ai.setup = {"state": "installing", "nonce": nonce}
    sidecar_status(env, nonce)
    env.fake.on("/api/version", {"version": "0.5.1"})
    env.fake.on("/api/tags", {"models": [{"name": "qwen2.5:7b"}]})
    env.local_answers("OK")
    st = run_setup(env, model="")
    assert st["state"] == "ready" and env.ai.settings()["local"]["model"] == "qwen2.5:7b"
    assert "/api/pull" not in [c.path for c in env.fake.calls]


def test_setup_run_failing_test_chat_does_not_switch_anything_on(env):
    nonce = "b" * 32
    env.ai.setup = {"state": "installing", "nonce": nonce}
    sidecar_status(env, nonce)
    env.fake.on("/api/version", {"version": "0.5.1"})
    env.fake.on("/api/tags", {"models": [{"name": "llama3.1:8b"}]})
    env.fake.on("/api/chat", ndjson({"message": {"content": ""}, "done": True}))
    st = run_setup(env)
    assert st["state"] == "failed" and st["message_key"] == "ai.err.empty"
    assert env.ai.settings()["local"]["enabled"] is False and env.store.get_setting("ai") is None
    assert env.ai.ready()["device"] is False


def test_setup_run_failing_pull(env):
    nonce = "c" * 32
    env.ai.setup = {"state": "installing", "nonce": nonce}
    sidecar_status(env, nonce)
    env.fake.on("/api/version", {"version": "0.5.1"})
    env.fake.on("/api/tags", {"models": []})
    env.fake.on("/api/pull", ndjson({"status": "pulling a"}))
    assert run_setup(env)["message_key"] == "ai.err.pull_incomplete"
    assert env.ai.settings()["local"]["enabled"] is False


def test_setup_run_sidecar_states(env):
    nonce = "d" * 32
    env.ai.setup = {"state": "installing", "nonce": nonce}
    sidecar_status(env, nonce, state="failed", message_key="ai.err.pull_failed", url="")
    assert run_setup(env)["message_key"] == "ai.err.pull_failed"
    # a status of another request is ignored until the time is up
    env.ai.setup = {"state": "installing", "nonce": nonce}
    sidecar_status(env, "z" * 32)
    st = run_setup(env, tick=lambda: env.clock.advance(600))
    assert st["state"] == "failed" and st["message_key"] == "ai.err.install_timeout"
    assert env.fake.calls == []


def test_setup_run_service_never_answers(env):
    nonce = "e" * 32
    env.ai.setup = {"state": "installing", "nonce": nonce}
    sidecar_status(env, nonce)
    st = run_setup(env, tick=lambda: env.clock.advance(30))
    assert st["state"] == "failed" and st["message_key"] == "ai.err.unreachable"
    assert env.ai.settings()["local"]["enabled"] is False


def test_setup_run_existing_server(env):
    env.fake.on("/api/version", {"version": "0.5.1"})
    env.fake.on("/api/tags", {"models": [{"name": "llama3.1:8b"}]})
    env.local_answers("OK")
    st = run_setup(env, "existing", "http://192.168.2.11:11434", "llama3.1:8b")
    assert st["state"] == "ready"
    loc = env.ai.settings()["local"]
    assert loc["origin"] == "detected" and loc["url"] == "http://192.168.2.11:11434" and loc["enabled"]


def test_setup_run_unexpected_error_is_a_plain_failure(env):
    env.ai.setup = {"state": "waiting"}
    env.fake.on("/api/version", lambda call: 1 / 0)
    env.fake.on("/api/tags", {})
    st = run_setup(env, "existing", "http://192.168.2.11:11434", "m")
    assert st["state"] in ("failed",) and st["message_key"].startswith("ai.err.")


# ---------------------------------------------------------------------- demo
def test_demo_manager_never_touches_the_network(demo_env):
    ai = demo_env.ai
    assert ai.settings()["local"]["kind"] == "demo" and ai.ready()["device"] is True
    demo_env.core._devices[:] = [device()]
    res = demo_env.explain()
    assert res["state"] == "done" and res["result"]["text"] and res["result"]["route"] == "local"
    assert demo_env.fake.calls == []
    assert {p["kind"] for p in ai.public_settings()["providers"]} == {"demo", "demo_cloud"}
    with pytest.raises(ValueError):
        ai.update_settings({"local": {"kind": "ollama"}})
    d = ai.discover()
    assert d["found"] and demo_env.fake.calls == []
