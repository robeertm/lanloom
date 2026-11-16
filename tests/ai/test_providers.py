"""Provider adapter: addresses, dialects, errors, model lists, secrets."""
import ipaddress
import json

import pytest

from lanloom.ai import providers
from lanloom.ai.providers import AIError, Client, Limits

from .fakes import KEY, Fake, ndjson, sse


# ---------------------------------------------------------------------- addresses
@pytest.mark.parametrize("kind,raw,want", [
    ("ollama", "192.168.1.9", "http://192.168.1.9"),
    ("ollama", "192.168.1.9:11434", "http://192.168.1.9:11434"),
    ("ollama", "http://192.168.1.9:11434/", "http://192.168.1.9:11434"),
    ("ollama", "http://192.168.1.9:11434/v1", "http://192.168.1.9:11434"),
    ("ollama", "http://192.168.1.9:11434/v1/chat/completions", "http://192.168.1.9:11434"),
    ("ollama", "http://192.168.1.9:11434/api/tags", "http://192.168.1.9:11434"),
    ("ollama", "http://192.168.1.9:11434/api/chat", "http://192.168.1.9:11434"),
    ("ollama", "http://[::1]:11434", "http://[::1]:11434"),
    ("ollama", "[::1]:11434/api/tags", "http://[::1]:11434"),
    ("openai_local", "192.168.1.9:1234", "http://192.168.1.9:1234/v1"),
    ("openai_local", "http://192.168.1.9:1234/v1", "http://192.168.1.9:1234/v1"),
    ("openai_local", "http://192.168.1.9:1234/v1/", "http://192.168.1.9:1234/v1"),
    ("openai_local", "http://192.168.1.9:1234/v1/chat/completions", "http://192.168.1.9:1234/v1"),
    ("openai_local", "http://192.168.1.9:1234/v1/models", "http://192.168.1.9:1234/v1"),
    ("openai_local", "http://[::1]:1234", "http://[::1]:1234/v1"),
    ("openai_compat", "https://203.0.113.5/api/v1", "https://203.0.113.5/api/v1"),
    ("openai_compat", "https://203.0.113.5/v1beta", "https://203.0.113.5/v1beta"),
])
def test_normalise_url(kind, raw, want):
    assert providers.normalise_url(kind, raw) == want


@pytest.mark.parametrize("raw", [
    "", "   ", "http://user:pw@192.168.1.9:11434", "http://user@192.168.1.9", "ftp://192.168.1.9",
    "http://192.168.1.9:11434/?x=1", "http://192.168.1.9#frag", "http://192.168.1.9:99999", "http://192.168.1.9:abc",
    "http://[::1", "http://",
])
def test_normalise_url_refuses(raw):
    with pytest.raises(AIError) as e:
        providers.normalise_url("ollama", raw)
    assert e.value.key == "ai.err.url"


@pytest.mark.parametrize("kind,base", [
    ("openai", "https://api.openai.com/v1"), ("anthropic", "https://api.anthropic.com"),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta"), ("mistral", "https://api.mistral.ai/v1"),
    ("openrouter", "https://openrouter.ai/api/v1")])
def test_public_services_have_a_fixed_base(kind, base):
    assert providers.normalise_url(kind, "") == base
    assert providers.normalise_url(kind, "http://203.0.113.9:1/evil") == base       # a pasted address is ignored
    assert Client(kind, "http://203.0.113.9:1/evil", KEY).base == base


def test_unknown_kind():
    with pytest.raises(AIError) as e:
        providers.get("nope")
    assert e.value.key == "ai.err.provider"


# Python counts the documentation ranges as private, so an address from the shared space (not private, not
# loopback, not link-local) stands in for "the internet"; it is built from a number, never contacted.
OUTSIDE = str(ipaddress.IPv4Address(0x64400001))


def _resolver(*addrs):
    return lambda host, port: [(2, 1, 6, "", (a, 0)) for a in addrs]


@pytest.mark.parametrize("url,want", [
    ("http://192.168.1.9:11434", True), ("http://10.0.0.2", True), ("http://127.0.0.1:1", True), ("http://[::1]:1", True),
    ("http://{out}", False), ("http://{out}:80", False), ("http://[::ffff:{out}]", False),
    ("http://[::ffff:192.168.1.9]", True), ("", False)])
def test_host_is_local_literals(url, want):
    url = url.replace("{out}", OUTSIDE)
    def boom(*a):
        raise AssertionError("literal addresses are never resolved")
    assert providers.host_is_local(url, boom) is want


def test_host_is_local_resolves_names():
    assert providers.host_is_local("http://box.example", _resolver("192.168.1.7")) is True
    assert providers.host_is_local("http://box.example", _resolver("192.168.1.7", OUTSIDE)) is False   # one public address is enough
    assert providers.host_is_local("http://box.example", _resolver("fe80::1%eth0")) is True
    assert providers.host_is_local("http://box.example", _resolver("not-an-ip")) is False
    assert providers.host_is_local("http://box.example", _resolver()) is False

    def fail(host, port):
        raise OSError("no such host")
    assert providers.host_is_local("http://box.example", fail) is False


# ---------------------------------------------------------------------- dialects
def _hdr(call, name):
    return {k.lower(): v for k, v in call.headers.items()}.get(name.lower())


def _assert_key_only_in(call, header):
    for k, v in call.headers.items():
        if k.lower() != header.lower():
            assert KEY not in v, k
    assert KEY not in call.url
    assert KEY not in json.dumps(call.body)


def test_native_chat():
    f = Fake().on("/api/chat", ndjson({"message": {"content": "Hel"}}, {"message": {"content": "lo"}, "done": True}))
    c = Client("ollama", "http://192.168.1.9:11434", opener=f)
    res = c.chat("sys", "usr", "llama3.1:8b")
    assert res["text"] == "Hello" and res["model"] == "llama3.1:8b" and res["ms"] >= 0
    call = f.calls[0]
    assert call.url == "http://192.168.1.9:11434/api/chat" and call.method == "POST"
    assert call.body["stream"] is True and call.body["model"] == "llama3.1:8b"
    assert [m["role"] for m in call.body["messages"]] == ["system", "user"]
    assert not any(k.lower() in ("authorization", "x-api-key", "x-goog-api-key") for k in call.headers)


def test_openai_chat_bearer_and_stream():
    f = Fake().on("/chat/completions", sse({"choices": [{"delta": {"content": "Hi "}}]},
                                           {"choices": [{"delta": {"content": "there"}}]}, {"choices": [{"delta": {}}]}))
    c = Client("openai", "", KEY, opener=f)
    assert c.chat("s", "u", "gpt-4o-mini")["text"] == "Hi there"
    call = f.calls[0]
    assert call.url == "https://api.openai.com/v1/chat/completions"
    assert _hdr(call, "authorization") == "Bearer " + KEY
    _assert_key_only_in(call, "Authorization")
    assert not _hdr(call, "x-api-key") and not _hdr(call, "x-goog-api-key")
    assert call.body["stream"] is True and call.body["max_completion_tokens"] >= 1024


def test_openai_compatible_other_services_use_max_tokens():
    f = Fake().on("/chat/completions", sse({"choices": [{"delta": {"content": "ok"}}]}))
    Client("mistral", "", KEY, opener=f).chat("s", "u", "mistral-small-latest")
    assert "max_tokens" in f.calls[0].body and "max_completion_tokens" not in f.calls[0].body
    assert f.calls[0].url == "https://api.mistral.ai/v1/chat/completions"


def test_openrouter_sends_title_header():
    f = Fake().on("/chat/completions", sse({"choices": [{"delta": {"content": "ok"}}]}))
    Client("openrouter", "", KEY, opener=f).chat("s", "u", "openrouter/auto")
    assert _hdr(f.calls[0], "x-title") == "Lanloom"


def test_no_key_header_without_key():
    f = Fake().on("/chat/completions", sse({"choices": [{"delta": {"content": "ok"}}]}))
    Client("openai_local", "http://192.168.1.9:1234", opener=f).chat("s", "u", "m")
    names = {k.lower() for k in f.calls[0].headers}
    assert not names & {"authorization", "x-api-key", "x-goog-api-key"}
    assert f.calls[0].url == "http://192.168.1.9:1234/v1/chat/completions"
    f2 = Fake().on("/chat/completions", sse({"choices": [{"delta": {"content": "ok"}}]}))
    Client("openai_local", "http://192.168.1.9:1234", KEY, opener=f2).chat("s", "u", "m")
    assert _hdr(f2.calls[0], "authorization") == "Bearer " + KEY


def test_anthropic_chat():
    f = Fake().on("/v1/messages", sse(
        {"type": "message_start"}, {"type": "content_block_delta", "delta": {"text": "A"}},
        {"type": "content_block_delta", "delta": {"text": "B"}}, {"type": "message_stop"}, {"type": "content_block_delta", "delta": {"text": "late"}},
        done=False))
    res = Client("anthropic", "", KEY, opener=f).chat("sys", "usr", "claude-haiku-x")
    assert res["text"] == "AB"
    call = f.calls[0]
    assert call.url == "https://api.anthropic.com/v1/messages"
    assert _hdr(call, "x-api-key") == KEY and _hdr(call, "anthropic-version")
    assert not _hdr(call, "authorization") and not _hdr(call, "x-goog-api-key")
    _assert_key_only_in(call, "x-api-key")
    assert call.body["system"] == "sys" and call.body["messages"] == [{"role": "user", "content": "usr"}]


def test_gemini_chat():
    f = Fake().on(":streamGenerateContent?alt=sse", sse(
        {"candidates": [{"content": {"parts": [{"text": "thinking...", "thought": True}, {"text": "Fine "}]}}]},
        {"candidates": [{"content": {"parts": [{"text": "answer"}]}}]}, done=False))
    res = Client("gemini", "", KEY, opener=f).chat("sys", "usr", "gemini-2.5-flash")
    assert res["text"] == "Fine answer"
    call = f.calls[0]
    assert call.url == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse"
    assert _hdr(call, "x-goog-api-key") == KEY and not _hdr(call, "authorization") and not _hdr(call, "x-api-key")
    _assert_key_only_in(call, "x-goog-api-key")
    assert call.body["systemInstruction"]["parts"][0]["text"] == "sys"


def test_gemini_model_name_is_quoted():
    f = Fake().on("?alt=sse", sse({"candidates": [{"content": {"parts": [{"text": "x"}]}}]}))
    Client("gemini", "", KEY, opener=f).chat("s", "u", "a/b c")
    assert "/models/a%2Fb%20c:streamGenerateContent" in f.calls[0].url


def test_think_blocks_are_removed():
    f = Fake().on("/api/chat", ndjson({"message": {"content": "<think>secret\nreasoning"}}, {"message": {"content": " more</think>\n\nThe answer."}, "done": True}))
    assert Client("ollama", "http://127.0.0.1:11434", opener=f).chat("s", "u", "m")["text"] == "The answer."


@pytest.mark.parametrize("lines", [
    ndjson({"message": {"content": ""}, "done": True}),
    ndjson({"message": {"content": "<think>only thoughts</think>"}, "done": True}),
    [],
])
def test_empty_answer(lines):
    f = Fake().on("/api/chat", lines)
    with pytest.raises(AIError) as e:
        Client("ollama", "http://127.0.0.1:11434", opener=f).chat("s", "u", "m")
    assert e.value.key == "ai.err.empty"


def test_empty_openai_answer():
    f = Fake().on("/chat/completions", sse({"choices": [{"delta": {}}]}))
    with pytest.raises(AIError) as e:
        Client("openai", "", KEY, opener=f).chat("s", "u", "m")
    assert e.value.key == "ai.err.empty"


def test_model_required_and_stream_closed():
    c = Client("ollama", "http://127.0.0.1:11434", opener=Fake())
    with pytest.raises(AIError) as e:
        c.chat("s", "u", "")
    assert e.value.key == "ai.err.model_missing"


def test_streams_are_closed_after_use():
    streams = []

    class Rec(Fake):
        def _respond(self, call, response):
            st = super()._respond(call, response)
            streams.append(st)
            return st
    f = Rec().on("/api/chat", ndjson({"message": {"content": "x"}, "done": True})).on("/api/tags", {"models": []})
    c = Client("ollama", "http://127.0.0.1:11434", opener=f)
    c.chat("s", "u", "m")
    c.models()
    assert streams and all(s.closed for s in streams)


def test_in_stream_errors():
    f = Fake().on("/api/chat", ndjson({"error": "model 'x' not found"}))
    with pytest.raises(AIError) as e:
        Client("ollama", "http://127.0.0.1:11434", opener=f).chat("s", "u", "x")
    assert e.value.key == "ai.err.model"
    f = Fake().on("/api/chat", ndjson({"error": "model requires more system memory"}))
    with pytest.raises(AIError) as e:
        Client("ollama", "http://127.0.0.1:11434", opener=f).chat("s", "u", "x")
    assert e.value.key == "ai.err.memory"
    f = Fake().on("/chat/completions", sse({"error": {"message": "boom"}}))
    with pytest.raises(AIError) as e:
        Client("openai", "", KEY, opener=f).chat("s", "u", "x")
    assert e.value.key == "ai.err.http"


def test_answer_is_capped():
    big = "x" * 5000
    f = Fake().on("/api/chat", ndjson(*[{"message": {"content": big}}] * 5, {"message": {"content": "tail"}, "done": True}))
    res = Client("ollama", "http://127.0.0.1:11434", opener=f).chat("s", "u", "m")
    assert len(res["text"]) <= providers.MAX_ANSWER_CHARS


# ---------------------------------------------------------------------- HTTP errors
@pytest.mark.parametrize("status,body,key", [
    (401, b"", "ai.err.auth"), (403, b"", "ai.err.auth"), (404, b'{"error":"The model does not exist"}', "ai.err.model"),
    (404, b"nothing here", "ai.err.not_found"), (429, b"", "ai.err.rate"), (400, b"unknown model", "ai.err.model"),
    (400, b"bad", "ai.err.http"), (500, b"", "ai.err.http"), (503, b"", "ai.err.http")])
def test_http_error_mapping(status, body, key):
    e = providers._http_error(status, body)
    assert e.key == key and e.status == status


@pytest.mark.parametrize("status,key", [(401, "ai.err.auth"), (429, "ai.err.rate")])
def test_chat_maps_http_errors_from_the_opener(status, key):
    f = Fake().on("/chat/completions", status)
    with pytest.raises(AIError) as e:
        Client("openai", "", KEY, opener=f).chat("s", "u", "m")
    assert e.value.key == key


def test_chat_404_with_model_word():
    f = Fake().on("/chat/completions", providers._http_error(404, b"model not found"))
    with pytest.raises(AIError) as e:
        Client("openai", "", KEY, opener=f).chat("s", "u", "m")
    assert e.value.key == "ai.err.model"


# ---------------------------------------------------------------------- model lists
def test_models_empty_list_is_a_list():
    for kind, route, doc in (("ollama", "/api/tags", {"models": []}), ("ollama", "/api/tags", {}),
                             ("ollama", "/api/tags", {"models": None}), ("openai_local", "/models", {"data": []}),
                             ("openai", "/models", {"data": None})):
        url = "http://127.0.0.1:1" if kind != "openai" else ""
        c = Client(kind, url, KEY if kind == "openai" else None, opener=Fake().on(route, doc))
        res = c.models()
        assert res == [] and isinstance(res, list)


def test_models_native_filters_embeddings_and_dedupes():
    doc = {"models": [{"name": "llama3.1:8b"}, {"model": "qwen2.5:7b"}, {"name": "nomic-embed-text:latest"},
                      {"name": "bge-m3"}, {"name": "llama3.1:8b"}, "junk", {"name": ""}, {"name": "x" * 200}]}
    c = Client("ollama", "http://127.0.0.1:11434", opener=Fake().on("/api/tags", doc))
    assert c.models() == ["llama3.1:8b", "qwen2.5:7b"]


def test_models_openai_filters():
    doc = {"data": [{"id": "gpt-4o-mini"}, {"id": "text-embedding-3-small"}, {"id": "whisper-1"}, {"id": "gpt-image-1"},
                    {"id": "omni-moderation-latest"}, {"id": "gpt-4.1-mini"}]}
    f = Fake().on("/models", doc)
    c = Client("openai", "", KEY, opener=f)
    assert c.models() == ["gpt-4o-mini", "gpt-4.1-mini"]
    assert f.calls[0].url == "https://api.openai.com/v1/models"


def test_models_anthropic_path():
    f = Fake().on("/v1/models?limit=100", {"data": [{"id": "claude-haiku-x"}, {"id": "claude-sonnet-x"}]})
    assert Client("anthropic", "", KEY, opener=f).models() == ["claude-haiku-x", "claude-sonnet-x"]
    assert _hdr(f.calls[0], "x-api-key") == KEY


def test_models_gemini_prefix_and_methods():
    doc = {"models": [{"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent", "countTokens"]},
                      {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
                      {"name": "models/gemini-2.5-pro"}]}
    f = Fake().on("/models?pageSize=200", doc)
    got = Client("gemini", "", KEY, opener=f).models()
    assert got == ["gemini-2.5-flash", "gemini-2.5-pro"]
    assert _hdr(f.calls[0], "x-goog-api-key") == KEY


def test_models_bad_answer_and_cap():
    f = Fake().on("/api/tags", ["not json"])
    with pytest.raises(AIError) as e:
        Client("ollama", "http://127.0.0.1:11434", opener=f).models()
    assert e.value.key == "ai.err.bad_answer"
    many = {"models": [{"name": "m%d" % i} for i in range(providers.MAX_MODELS + 50)]}
    got = Client("ollama", "http://127.0.0.1:11434", opener=Fake().on("/api/tags", many)).models()
    assert len(got) == providers.MAX_MODELS


def test_models_uses_short_limits():
    f = Fake().on("/api/tags", {"models": []})
    Client("ollama", "http://127.0.0.1:11434", opener=f).models()
    lim = f.calls[0].limits
    assert lim.first == providers.LIST_TIMEOUT and lim.connect <= 5


def test_recommended():
    c = Client("ollama", "http://127.0.0.1:11434", opener=Fake())
    assert c.recommended(["qwen2.5:7b", "llama3.1:8b-instruct"]) == "llama3.1:8b-instruct"
    assert c.recommended(["other", "qwen2.5:7b"]) == "qwen2.5:7b"
    assert c.recommended(["zzz", "yyy"]) == "zzz"
    assert c.recommended([]) == ""
    assert c.recommended(["llama3.1:8b", "special"], (r"^special$",)) == "special"
    o = Client("openai", "", KEY, opener=Fake())
    assert o.recommended(["gpt-4o", "gpt-4.1-mini", "gpt-4o-mini"]) == "gpt-4.1-mini"
    a = Client("anthropic", "", KEY, opener=Fake())
    assert a.recommended(["claude-sonnet-x", "claude-haiku-x"]) == "claude-haiku-x"
    g = Client("gemini", "", KEY, opener=Fake())
    assert g.recommended(["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"]) == "gemini-2.5-flash-lite"


# ---------------------------------------------------------------------- secrets
def test_key_required_for_public():
    with pytest.raises(AIError) as e:
        Client("openai", "", None)
    assert e.value.key == "ai.err.key_missing"
    with pytest.raises(AIError):
        Client("anthropic", "", "")


def test_key_never_in_repr_or_str():
    c = Client("openai", "", KEY, opener=Fake())
    for text in (repr(c.key), str(c.key), "%s" % c.key, "%r" % c.key, "{}".format(c.key), f"{c.key}", repr(c.__dict__)):
        assert KEY not in text
    with pytest.raises(TypeError):
        json.dumps(c.key)


@pytest.mark.parametrize("kind", ["openai", "anthropic", "gemini", "mistral", "openrouter"])
def test_no_error_text_holds_the_key(kind):
    failures = [AIError("ai.err.unreachable", "x"), providers._http_error(401, KEY.encode()), providers._http_error(500, KEY.encode()),
                providers._http_error(404, ("model " + KEY).encode()), providers._http_error(429, KEY.encode())]
    for exc in failures:
        f = Fake().on("", exc)
        c = Client(kind, "", KEY, opener=f)
        with pytest.raises(AIError) as e:
            c.chat("s", "u", "m")
        for text in (str(e.value), repr(e.value), e.value.key, e.value.detail):
            assert KEY not in text
    # the answer body of a failing stream is never part of an error either
    f = Fake().on("", sse({"error": {"message": "echo " + KEY}}))
    with pytest.raises(AIError) as e:
        Client(kind, "", KEY, opener=f).chat("s", "u", "m")
    assert KEY not in str(e.value) + e.value.detail + repr(e.value)


def test_detail_is_short():
    assert len(AIError("ai.err.x", "y" * 1000).detail) == 200


# ---------------------------------------------------------------------- demo
def test_demo_dialect_needs_no_network():
    c = Client("demo", "", opener=Fake())
    assert c.models() == ["sample-small", "sample-medium"]
    res = c.test("sample-small")
    assert res["text"] == "OK"
    res = c.chat("Answer in English.", "Subject: device\nName: Box\nKind: nas", "sample-small")
    assert "Box" in res["text"] and res["text"].startswith("This looks like Box")


def test_public_list_hides_product_names_in_demo():
    demo = providers.public_list(True)
    assert {p["kind"] for p in demo} == {"demo", "demo_cloud"}
    low = json.dumps(demo).lower()
    for name in ("openai", "anthropic", "gemini", "mistral", "ollama", "openrouter", "lm studio", "llama", "vllm"):
        assert name not in low
    real = {p["kind"] for p in providers.public_list(False)}
    assert "demo" not in real and "openai" in real


def test_local_helpers_native_version_and_pull():
    f = Fake().on("/api/version", {"version": "0.5.1"})
    assert providers.native_version("http://127.0.0.1:11434", f) == "0.5.1"
    assert providers.native_version("http://127.0.0.1:11434", Fake().on("/api/version", {})) is None
    assert providers.native_version("http://127.0.0.1:11434", Fake().on("/api/version", ["junk"])) is None
    assert providers.native_version("http://127.0.0.1:11434", Fake()) is None          # unreachable never raises
    seen = []
    f = Fake().on("/api/pull", ndjson({"status": "pulling x", "completed": 5, "total": 10}, {"status": "success"}))
    providers.native_pull("http://127.0.0.1:11434", "m", lambda *a: seen.append(a), f)
    assert seen[0] == ("pulling x", 5, 10) and seen[-1][0] == "success"
    f = Fake().on("/api/pull", ndjson({"status": "pulling x"}))
    with pytest.raises(AIError) as e:
        providers.native_pull("http://127.0.0.1:11434", "m", lambda *a: None, f)
    assert e.value.key == "ai.err.pull_incomplete"
    f = Fake().on("/api/pull", ndjson({"error": "pull model manifest: file does not exist, not found"}))
    with pytest.raises(AIError) as e:
        providers.native_pull("http://127.0.0.1:11434", "m", lambda *a: None, f)
    assert e.value.key == "ai.err.model"


def test_limits_defaults():
    assert Client("ollama", "http://127.0.0.1:11434", opener=Fake()).limits == providers.LOCAL_LIMITS
    assert Client("openai", "", KEY, opener=Fake()).limits == providers.PUBLIC_LIMITS
    lim = Limits(1, 2, 3, 4)
    assert Client("openai", "", KEY, limits=lim, opener=Fake()).limits is lim
