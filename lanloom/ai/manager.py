"""AI in Lanloom: settings, jobs, "Explain", local setup, pairing of another computer.

Ground rules (tested):

* **Off by default.** Nothing leaves the device until a provider is set up and switched on.
* **Keys** live in the encrypted secret store (``Vault``), are never returned, logged, exported or put in a report.
* **Public services only through the privacy layer** (``privacy.py``): placeholders out, real values back, a leak
  check before sending, a daily limit, and every function needs its own permission. A local service gets the real
  data; it stays inside the network. A "local" address that leads to the internet counts as public.
* **Acknowledgement before work.** Every model call is a job: the request returns at once with a job id, the answer
  is fetched by polling. The provider call itself has separate limits for connecting, the first byte and the rest.
* **Reachability is checked by the Lanloom server**, never by the browser, and only on known addresses: the
  configured one, this host, the service the updater may have started, and devices Lanloom already knows. No
  address ranges are swept.
* **Success means an answer**: a local setup or a paired computer only counts once the model answered a question.
* **AI never writes configuration.** It explains; any change still goes through the executor with preview and
  confirmation.
"""
import concurrent.futures
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Optional

from . import features, hardware, privacy, providers, runtime
from .providers import AIError, Client

log = logging.getLogger("lanloom.ai")

FUNCTIONS = ("explain_device", "explain_finding", "explain_rule", "ask", "report_plain", "identify", "suggest_firewall")
SUBJECTS = {"device": "explain_device", "finding": "explain_finding", "rule": "explain_rule"}
# what the page may offer (key in /api/state "ai") -> function
FEATURES = {"ask": "ask", "report": "report_plain", "identify": "identify", "firewall": "suggest_firewall"}
LANG_NAMES = {"en": "English", "de": "German"}
DEFAULTS = {
    "local": {"enabled": False, "kind": "ollama", "url": "", "model": "", "origin": ""},
    "public": {"enabled": False, "kind": "openai", "url": "", "model": ""},
    "order": "local_first",              # local_first | public_first
    "allow": {f: False for f in FUNCTIONS},   # per function: may it use a public service?
    "daily_limit": 50,                   # requests to public services per day (0 = none at all)
    "features": {
        "report_plain": True,            # write the "In plain words" paragraph of each nightly report
        "deliver_plain": False,          # ... and put it into the outside delivery (mail, webhook, ...)
        "nightly_identify": False,       # at night, suggest a kind for a few devices Lanloom is unsure about
    },
}
FEATURE_FLAGS = tuple(DEFAULTS["features"])
ORDERS = ("local_first", "public_first")
JOB_KEEP_S = 900
MAX_JOBS = 64
PAIR_VALID_S = 4 * 3600
PAIR_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_RX = re.compile(r"\A[A-Z0-9]{4}-?[A-Z0-9]{4}\Z")
PROBE_MAX_HOSTS = 24
SUGGEST_KINDS = {"server": 4, "nas": 3, "computer": 2}
INSTALLER_OS = {"linux": "setup.sh", "mac": "setup.sh", "windows": "setup.ps1"}
OS_LABELS = {"linux": "Linux", "mac": "macOS", "windows": "Windows"}
# demo mode: pictures of the sample setup show no operating-system names
DEMO_OS_LABELS = {"linux": "System A (shell script)", "mac": "System B (shell script)", "windows": "System C (PowerShell script)"}
HOST_HEADER_RX = re.compile(r"\A[A-Za-z0-9.\-]+(:\d{1,5})?\Z|\A\[[0-9A-Fa-f:.]+\](:\d{1,5})?\Z")
INSTALLER_DIR = Path(__file__).resolve().parent / "installer"
USAGE_KEEP_DAYS = 14

SYSTEM_PROMPT = (
    "You are the assistant inside Lanloom, a read-only map of a home or small office network. Explain the item below "
    "to a person without network knowledge, in plain words. Answer in {lang}. At most 120 words, no headings, at most "
    "three short points. Placeholders in square brackets such as [IP-1] or [HOST-2] stand for hidden real values: keep "
    "them exactly as written. Never give commands or step-by-step instructions that change a configuration. If a "
    "change could help, say so in one sentence and add that Lanloom shows a preview and asks for confirmation before "
    "anything is changed.")


def _deep_merge(base: dict, patch: dict) -> dict:
    out = json.loads(json.dumps(base))
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _today(now: float = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now if now is not None else time.time()))


def _hash_code(code: str) -> str:
    return hashlib.sha256(("lanloom-ai-pair:" + code).encode("utf-8")).hexdigest()


def normalise_code(code) -> str:
    c = re.sub(r"[\s-]", "", str(code or "")).upper()
    return c if re.fullmatch(r"[A-Z0-9]{8}", c) else ""


class AI:
    def __init__(self, core, store, vault, cfg, opener=None, clock=time.time):
        self.core = core
        self.store = store
        self.vault = vault
        self.cfg = cfg
        self.demo = bool(getattr(cfg, "demo", False))
        self.opener = opener or providers._Stream
        self.clock = clock
        self.data_dir = Path(getattr(cfg, "data_dir", "/nonexistent"))
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai")
        self._jobs = {}
        self._lock = threading.Lock()
        self.setup = {"state": "idle"}
        self._setup_thread = None
        self._discovered = {"t": 0, "found": [], "probed": 0}
        self._where = {}          # local address -> (checked at, is it inside the network?)
        self.hints = features.Hints(store, clock)
        self.firewall = None      # the firewall editor (set by the app): read for suggestions, never written

    # ------------------------------------------------------------------ settings
    def settings(self) -> dict:
        stored = self.store.get_setting("ai", {}) or {}
        s = _deep_merge(DEFAULTS, stored if isinstance(stored, dict) else {})
        if self.demo and not stored:
            s = _deep_merge(s, {"local": {"enabled": True, "kind": "demo", "model": "sample-medium", "origin": "sample"},
                                "public": {"kind": "demo_cloud", "model": "sample-cloud"}})
        return s

    def _host(self) -> dict:
        """Memory and cores of this host (a sample computer in demo mode, so pictures do not show the real one)."""
        return {"ram_gb": 16.0, "cores": 8, "gpu": "", "arch": "x86_64"} if self.demo else hardware.this_host()

    def _key_name(self, scope: str, kind: str) -> str:
        return "ai.key.%s.%s" % (scope, kind)

    def _key(self, scope: str, kind: str):
        from ..integrations.credentials import Secret
        try:
            return Secret(self.vault.get(self._key_name(scope, kind)) if self.vault else "")
        except Exception:
            return Secret("")

    def _has_key(self, scope: str, kind: str) -> bool:
        try:
            return bool(self.vault and self.vault.has(self._key_name(scope, kind)))
        except Exception:
            return False

    def public_settings(self) -> dict:
        s = self.settings()
        for scope in ("local", "public"):
            s[scope]["has_key"] = self._has_key(scope, s[scope]["kind"])
            try:
                s[scope]["label"] = providers.get(s[scope]["kind"]).label
            except AIError:
                s[scope]["label"] = ""
        host = self._host()
        return {"settings": s, "providers": providers.public_list(self.demo), "functions": list(FUNCTIONS),
                "usage": self.usage_today(), "ready": self.ready(), "host": host,
                "advice": hardware.advise(host["ram_gb"], host["cores"], host["gpu"], host["arch"], demo=self.demo),
                "sidecar": self._sidecar_alive(), "setup": self.setup_state(), "demo": self.demo,
                "notice": providers.NO_AFFILIATION}

    def update_settings(self, patch: dict, secrets_: dict = None) -> dict:
        s = self.settings()
        patch = patch if isinstance(patch, dict) else {}
        for scope in ("local", "public"):
            part = patch.get(scope)
            if not isinstance(part, dict):
                continue
            cur = s[scope]
            if "kind" in part:
                kind = str(part["kind"] or "")
                p = providers.PROVIDERS.get(kind)
                if p is None or p.scope != scope or p.demo_only != self.demo:
                    raise ValueError("ai.err.provider")
                if kind != cur["kind"]:
                    cur["model"] = ""
                cur["kind"] = kind
            if "url" in part:
                url = str(part["url"] or "").strip()[:300]
                p = providers.get(cur["kind"])
                if url and not p.base_url and p.dialect != "demo":
                    try:
                        url = providers.normalise_url(cur["kind"], url)
                    except AIError as exc:
                        raise ValueError(exc.key)
                cur["url"] = "" if p.base_url else url
            if "model" in part:
                cur["model"] = str(part["model"] or "").strip()[:160]
            if "enabled" in part:
                cur["enabled"] = bool(part["enabled"])
            if scope == "local" and "origin" in part and part["origin"] in ("manual", "detected"):
                cur["origin"] = part["origin"]
        if patch.get("order") in ORDERS:
            s["order"] = patch["order"]
        if isinstance(patch.get("allow"), dict):
            for f, v in patch["allow"].items():
                if f in FUNCTIONS:
                    s["allow"][f] = bool(v)
        if isinstance(patch.get("features"), dict):
            for f, v in patch["features"].items():
                if f in FEATURE_FLAGS:
                    s["features"][f] = bool(v)
        if "daily_limit" in patch:
            try:
                s["daily_limit"] = max(0, min(1000, int(patch["daily_limit"])))
            except (TypeError, ValueError):
                raise ValueError("ai.err.limit")
        for scope in ("local", "public"):
            cur = s[scope]
            if cur["enabled"]:
                p = providers.get(cur["kind"])
                if p.dialect != "demo" and not p.base_url and not cur["url"]:
                    raise ValueError("ai.err.url")
        sec = secrets_ if isinstance(secrets_, dict) else {}
        for scope in ("local", "public"):          # check before anything is written: a refused change keeps the old key
            cur = s[scope]
            new_key = isinstance(sec.get("%s_key" % scope), str) and sec["%s_key" % scope].strip()
            if cur["enabled"] and providers.get(cur["kind"]).key == "required" and not new_key and \
                    (sec.get("clear_%s_key" % scope) or not self._has_key(scope, cur["kind"])):
                raise ValueError("ai.err.key_missing")
        self._store_secrets(s, sec)
        for scope in ("local", "public"):
            cur = s[scope]
            p = providers.get(cur["kind"])
            if cur["enabled"] and p.key == "required" and not self._has_key(scope, cur["kind"]):
                raise ValueError("ai.err.key_missing")
        stored = {k: s[k] for k in DEFAULTS}
        for scope in ("local", "public"):
            stored[scope] = {k: v for k, v in s[scope].items() if k in DEFAULTS[scope]}
        self.store.set_setting("ai", stored)
        return self.public_settings()

    def _store_secrets(self, s: dict, sec: dict) -> None:
        if not self.vault or not isinstance(sec, dict):
            return
        for scope in ("local", "public"):
            kind = s[scope]["kind"]
            if sec.get("clear_%s_key" % scope):
                self.vault.delete(self._key_name(scope, kind))
            value = sec.get("%s_key" % scope)
            if isinstance(value, str) and value.strip():
                if len(value) > 400 or any(ord(ch) < 33 for ch in value.strip()):
                    raise ValueError("ai.err.key_format")
                self.vault.put(self._key_name(scope, kind), value.strip())

    # ------------------------------------------------------------------ readiness and routes
    def _route_ok(self, scope: str, s: dict) -> bool:
        cur = s[scope]
        if not cur["enabled"] or not cur["model"]:
            return False
        try:
            p = providers.get(cur["kind"])
        except AIError:
            return False
        if p.demo_only != self.demo:
            return False
        if p.dialect != "demo" and not p.base_url and not cur["url"]:
            return False
        if p.key == "required" and not self._has_key(scope, cur["kind"]):
            return False
        return True

    def _local_is_inside(self, url: str) -> bool:
        """Does a "local" address stay inside the network? Names are resolved at most once a minute."""
        now = self.clock()
        hit = self._where.get(url)
        if hit and now - hit[0] < 60:
            return hit[1]
        inside = providers.host_is_local(url)
        self._where[url] = (now, inside)
        return inside

    def _local_leaves_network(self, s: dict) -> bool:
        cur = s["local"]
        try:
            p = providers.get(cur["kind"])
        except AIError:
            return False
        return p.dialect != "demo" and bool(cur["url"]) and not self._local_is_inside(cur["url"])

    def routes(self, function: str, s: dict = None) -> list:
        s = s or self.settings()
        out = []
        if self._route_ok("local", s):
            # a "local" address that leads to the internet is a public service: it needs the same permission and limit
            if not self._local_leaves_network(s) or (s["allow"].get(function) and s["daily_limit"] > 0):
                out.append("local")
        if self._route_ok("public", s) and s["allow"].get(function) and s["daily_limit"] > 0:
            out.append("public")
        if s["order"] == "public_first":
            out.reverse()
        return out

    def ready(self) -> dict:
        s = self.settings()
        out = {subj: bool(self.routes(fn, s)) for subj, fn in SUBJECTS.items()}
        for key, fn in FEATURES.items():
            out[key] = bool(self.routes(fn, s))
        return out

    # ------------------------------------------------------------------ usage and daily limit
    def _usage(self) -> dict:
        u = self.store.get_setting("ai_usage", {}) or {}
        return u if isinstance(u, dict) else {}

    def usage_today(self) -> dict:
        day = self._usage().get(_today(self.clock())) or {}
        return {"local": int(day.get("local", 0)), "public": int(day.get("public", 0)),
                "failed": int(day.get("failed", 0)), "limited": int(day.get("limited", 0)),
                "limit": int(self.settings()["daily_limit"])}

    def _count(self, field: str) -> None:
        with self._lock:
            u = self._usage()
            day = _today(self.clock())
            entry = u.setdefault(day, {})
            entry[field] = int(entry.get(field, 0)) + 1
            for old in sorted(u)[:-USAGE_KEEP_DAYS]:
                u.pop(old, None)
            self.store.set_setting("ai_usage", u)

    def _take_public_slot(self, limit: int) -> bool:
        with self._lock:
            u = self._usage()
            day = u.setdefault(_today(self.clock()), {})
            if int(day.get("public", 0)) >= limit:
                return False
            day["public"] = int(day.get("public", 0)) + 1
            self.store.set_setting("ai_usage", u)
            return True

    def usage_between(self, since: float, until: float) -> dict:
        out = {"local": 0, "public": 0, "failed": 0, "limited": 0}
        days = set()
        t = since
        while t <= until:
            days.add(_today(t))
            t += 3600
        days.add(_today(until))
        for d, entry in self._usage().items():
            if d in days and isinstance(entry, dict):
                for k in out:
                    out[k] += int(entry.get(k, 0))
        return out

    # ------------------------------------------------------------------ jobs (acknowledge first, then work)
    def _new_job(self, kind: str, fn, *args) -> dict:
        now = self.clock()
        job = {"id": secrets.token_hex(8), "kind": kind, "state": "queued", "t0": now, "t": now, "result": None, "error": None}
        with self._lock:
            for jid in [j for j, v in self._jobs.items() if now - v["t"] > JOB_KEEP_S]:
                self._jobs.pop(jid, None)
            if len(self._jobs) >= MAX_JOBS:
                oldest = min(self._jobs.values(), key=lambda v: v["t"])
                self._jobs.pop(oldest["id"], None)
            self._jobs[job["id"]] = job

        def run():
            job["state"] = "running"
            job["t"] = self.clock()
            try:
                job["result"] = fn(job, *args)
                job["state"] = "done"
            except AIError as exc:
                job["state"], job["error"] = "error", exc.key
            except ValueError as exc:
                job["state"], job["error"] = "error", str(exc) if str(exc).startswith("ai.") else "ai.err.internal"
            except Exception as exc:          # never let a worker die silently; no detail that could hold data
                log.warning("AI job failed: %s", type(exc).__name__)
                job["state"], job["error"] = "error", "ai.err.internal"
            job["t"] = self.clock()

        self._pool.submit(run)
        return self.job_view(job["id"])

    def job_view(self, job_id: str) -> Optional[dict]:
        job = self._jobs.get(str(job_id or ""))
        if not job:
            return None
        return {k: job[k] for k in ("id", "kind", "state", "result", "error")} | {"age_s": round(self.clock() - job["t0"], 1)}

    # ------------------------------------------------------------------ clients
    def _client(self, scope: str, cfgpart: dict, key=None, limits=None) -> Client:
        if key is None:
            key = self._key(scope, cfgpart["kind"])
        return Client(cfgpart["kind"], cfgpart.get("url", ""), key, limits=limits, opener=self.opener)

    def _draft(self, scope: str, data: dict) -> tuple:
        s = self.settings()
        cur = dict(s[scope])
        kind = str(data.get("kind") or cur["kind"])
        p = providers.PROVIDERS.get(kind)
        if p is None or p.scope != scope or p.demo_only != self.demo:
            raise ValueError("ai.err.provider")
        cur["kind"] = kind
        if "url" in data:
            cur["url"] = str(data.get("url") or "").strip()[:300]
        if data.get("model"):
            cur["model"] = str(data["model"]).strip()[:160]
        from ..integrations.credentials import Secret
        typed = data.get("key")
        key = Secret(typed.strip()) if isinstance(typed, str) and typed.strip() else self._key(scope, kind)
        return cur, key

    def models(self, scope: str, data: dict) -> dict:
        """Model list of a (possibly not yet saved) provider, fetched by the server."""
        if scope not in ("local", "public"):
            raise ValueError("ai.err.scope")
        cur, key = self._draft(scope, data or {})
        try:
            c = self._client(scope, cur, key)
            ids = c.models()
        except AIError as exc:
            return {"ok": False, "error": exc.key, "models": [], "recommended": ""}
        extra = ()
        if scope == "local" and providers.get(cur["kind"]).dialect == "native":
            host = self._host() if self._is_this_host(c.base) else None
            if host:
                adv = hardware.advise(host["ram_gb"], host["cores"], host["gpu"], host["arch"], demo=self.demo)
                if adv["model"]:
                    extra = (r"^%s$" % re.escape(adv["model"]),)
        return {"ok": True, "models": ids, "recommended": c.recommended(ids, extra), "url": c.base}

    @staticmethod
    def _is_this_host(url: str) -> bool:
        host = urllib.parse.urlsplit(url).hostname or ""
        return host in ("127.0.0.1", "localhost", "::1", runtime.CONTAINER)

    def test(self, scope: str, data: dict) -> dict:
        """Start a connection test (a real question) and return the job at once."""
        if scope not in ("local", "public"):
            raise ValueError("ai.err.scope")
        cur, key = self._draft(scope, data or {})
        if not cur["model"]:
            raise ValueError("ai.err.model_missing")
        limits = providers.TEST_LIMITS_LOCAL if scope == "local" else None
        client = self._client(scope, cur, key, limits)        # validates address and key before acknowledging

        def work(job):
            job["state"] = "waiting"
            res = client.test(cur["model"])
            return {"text": res["text"][:200], "ms": res["ms"], "first_ms": res["first_ms"], "model": res["model"]}
        return self._new_job("test", work)

    # ------------------------------------------------------------------ explain
    def _lang(self, lang) -> str:
        lang = str(lang or self.store.get_setting("language", None) or "en")
        return lang if lang in LANG_NAMES else "en"

    def context(self, subject: str, ref: dict, lang: str) -> str:
        """The facts about one item, built from Lanloom's own data (never from text the browser sends)."""
        ref = ref if isinstance(ref, dict) else {}
        if subject == "device":
            dev = self.core.store.device(str(ref.get("id") or "")[:80])
            if not dev:
                raise ValueError("ai.err.subject")
            return _device_text(self.core.device_view(dev), self.clock())
        if subject == "finding":
            day, item_id = str(ref.get("day") or "")[:10], str(ref.get("item") or "")[:200]
            rep = self.core.reports.get(day)
            if not rep:
                raise ValueError("ai.err.subject")
            from ..nightly.render import Renderer
            r = Renderer(lang, self.core.reports.tz(), day)
            for sec in rep.get("sections") or []:
                for it in sec.get("items") or []:
                    if it.get("id") == item_id:
                        if sec.get("id") in features.PRIVATE_SECTIONS:     # names of automations may name people
                            it = dict(it, vars={k: ("(name hidden)" if k in ("name", "names") else v)
                                                for k, v in (it.get("vars") or {}).items()})
                        return "\n".join(["Subject: finding", "Section: " + r.tr(sec.get("title") or ""),
                                          "Note: " + r.line(it), "Level: " + str(it.get("level") or "info"),
                                          "Report day: " + day])
            raise ValueError("ai.err.subject")
        if subject == "rule":
            source, rule_id = str(ref.get("source") or "")[:24], str(ref.get("id") or "")[:120]
            for entry in self.core.live_configs():
                if entry.get("key") != source:
                    continue
                st = entry.get("state") or {}
                for p in st.get("policies") or []:
                    if str(p.get("id")) == rule_id:
                        return _rule_text(st, p)
            raise ValueError("ai.err.subject")
        raise ValueError("ai.err.subject")

    def _messages(self, subject: str, ref: dict, lang: str) -> tuple:
        return SYSTEM_PROMPT.format(lang=LANG_NAMES[lang]), self.context(subject, ref, lang)

    def _route_plan(self, scope: str, s: dict) -> dict:
        """How a route sends: public services (and local addresses that lead outside) get placeholders."""
        cur = s[scope]
        p = providers.get(cur["kind"])
        outside = scope == "public"
        if scope == "local" and p.dialect != "demo" and cur["url"]:
            outside = not self._local_is_inside(cur["url"])
        return {"scope": scope, "kind": cur["kind"], "label": p.label, "model": cur["model"], "pseudonymise": outside}

    def preview(self, subject: str, ref: dict, lang=None) -> dict:
        """Exactly the text each route would send (with placeholders where they apply)."""
        fn = SUBJECTS.get(subject)
        if not fn:
            raise ValueError("ai.err.subject")
        lang = self._lang(lang)
        system, user = self._messages(subject, ref, lang)
        return self._preview(fn, system, user)

    def _preview(self, fn: str, system: str, user: str) -> dict:
        s = self.settings()
        known = privacy.known_values(self.core)
        out = []
        for scope in self.routes(fn, s):
            plan = self._route_plan(scope, s)
            if plan["pseudonymise"]:
                ps = privacy.Pseudonymizer(known)
                plan.update(system=ps.out(system), user=ps.out(user), hidden=ps.mapping())
                plan["blocked"] = bool(privacy.leaks(plan["user"]) or privacy.leaks(plan["system"]))
            else:
                plan.update(system=system, user=user, hidden=[], blocked=False)
            out.append(plan)
        s_pub = s["public"]
        return {"routes": out, "allowed_public": bool(s["allow"].get(fn)), "public_ready": self._route_ok("public", s),
                "public_label": providers.get(s_pub["kind"]).label, "usage": self.usage_today()}

    def explain(self, subject: str, ref: dict, lang=None) -> dict:
        fn = SUBJECTS.get(subject)
        if not fn:
            raise ValueError("ai.err.subject")
        lang = self._lang(lang)
        s = self.settings()
        routes = self.routes(fn, s)
        if not routes:
            raise ValueError("ai.err.not_ready")
        system, user = self._messages(subject, ref, lang)     # fails fast for an unknown item
        known = privacy.known_values(self.core)
        return self._new_job("explain", self._explain_work, s, routes, system, user, known)

    def _explain_work(self, job, s, routes, system, user, known):
        first_error = None
        for scope in routes:
            plan = self._route_plan(scope, s)
            job["route"] = scope
            job["state"] = "sending"
            ps = None
            sys_out, user_out = system, user
            if plan["pseudonymise"]:
                ps = privacy.Pseudonymizer(known)
                sys_out, user_out = ps.out(system), ps.out(user)
                if privacy.leaks(sys_out) or privacy.leaks(user_out):
                    first_error = first_error or "ai.err.privacy"
                    self._count("failed")
                    continue
                if not self._take_public_slot(s["daily_limit"]):
                    first_error = first_error or "ai.err.daily_limit"
                    self._count("limited")
                    continue
            try:
                client = self._client(scope, s[scope])

                def answering():
                    job["state"] = "answering"
                res = client.chat(sys_out, user_out, s[scope]["model"], on_first=answering)
            except AIError as exc:
                first_error = first_error or exc.key
                self._count("failed")
                continue
            if not plan["pseudonymise"]:
                self._count("local")          # outside requests were counted when their slot was taken
            text = ps.back(res["text"]) if ps else res["text"]
            return {"text": text, "ms": res["ms"], "route": scope, "label": plan["label"], "model": res["model"],
                    "pseudonymised": bool(ps)}
        raise AIError(first_error or "ai.err.not_ready")

    # ------------------------------------------------------------------ KI2: features built on the same routes
    def _ready_for(self, fn: str, s: dict = None) -> list:
        routes = self.routes(fn, s or self.settings())
        if not routes:
            raise ValueError("ai.err.not_ready")
        return routes

    def _run(self, kind: str, fn: str, system: str, user: str, post, preview: bool = False):
        """Preview, or a job that asks the model and hands the answer to ``post(result) -> dict``."""
        if preview:
            return self._preview(fn, system, user)
        s = self.settings()
        routes = self._ready_for(fn, s)
        known = privacy.known_values(self.core)

        def work(job):
            res = self._explain_work(job, s, routes, system, user, known)
            return post(res)
        return self._new_job(kind, work)

    @staticmethod
    def _meta(res: dict) -> dict:
        return {k: res[k] for k in ("ms", "route", "label", "model", "pseudonymised")}

    # "Ask your network" ------------------------------------------------------
    def ask(self, question, history=None, lang=None, preview: bool = False):
        """A question about the network: read-only facts from Lanloom's data, an answer with links."""
        question = features.clean_question(question)
        hist = features.clean_history(history)
        lang = self._lang(lang)
        facts = features.network_facts(self.core, question, hist, self.clock(), self.firewall)
        system = features.ASK_PROMPT.format(lang=LANG_NAMES[lang])
        user = features.ask_user_text(facts, question, hist)

        def post(res):
            out = features.resolve_refs(res["text"], facts)
            out.update(self._meta(res))
            return out
        return self._run("ask", "ask", system, user, post, preview)

    # nightly report: "In plain words" -------------------------------------------
    def _report_text(self, rep: dict) -> str:
        lines = ["Subject: report", "Report day: %s" % rep.get("day"), "Points that need a look: %d" % int(rep.get("points") or 0)]
        lines += features.report_lines(rep, getattr(self.core, "reports", None), limit=30)
        return "\n".join(lines)

    def _plain_record(self, res: dict, lang: str) -> dict:
        text = re.sub(r"\s*\n\s*", " ", str(res.get("text") or "")).strip()[:900]
        return {"text": text, "lang": lang, "t": self.clock(), **self._meta(res)}

    def report_plain(self, day: str, lang=None, preview: bool = False):
        """Write (or rewrite) the paragraph of a saved report on request."""
        reports = getattr(self.core, "reports", None)
        rep = reports.get(str(day or "")[:10]) if reports else None
        if not rep:
            raise ValueError("ai.err.subject")
        lang = self._lang(lang)
        system = features.REPORT_PROMPT.format(lang=LANG_NAMES[lang])

        def post(res):
            rec = self._plain_record(res, lang)
            reports.set_plain(rep["day"], rec)
            return rec
        return self._run("report_plain", "report_plain", system, self._report_text(rep), post, preview)

    def plain_for(self, rep: dict):
        """Called by the nightly report after it was saved: the paragraph, or None (no AI, switched off, failed).
        Runs in the caller's thread; the provider limits bound how long it takes."""
        s = self.settings()
        if not s["features"].get("report_plain"):
            return None
        routes = self.routes("report_plain", s)
        if not routes:
            return None
        lang = self._lang(None)
        system = features.REPORT_PROMPT.format(lang=LANG_NAMES[lang])
        job = {"state": "running"}
        try:
            res = self._explain_work(job, s, routes, system, self._report_text(rep), privacy.known_values(self.core))
        except (AIError, ValueError):
            return None
        return self._plain_record(res, lang)

    def deliver_plain(self) -> bool:
        return bool(self.settings()["features"].get("deliver_plain"))

    # identification help ----------------------------------------------------------
    def _device(self, device_id) -> dict:
        dev = self.core.store.device(str(device_id or "")[:80])
        if not dev:
            raise ValueError("ai.err.subject")
        view = self.core.device_view(dev)
        if view.get("is_self"):
            raise ValueError("ai.err.subject")
        return view

    def identify(self, device_id, lang=None, preview: bool = False):
        """Suggest a kind for one device; the suggestion waits for the user's decision."""
        from ..classify import KINDS
        dev = self._device(device_id)
        lang = self._lang(lang)
        kinds = [k for k in KINDS if k not in ("self", "internet")]
        system = features.IDENTIFY_PROMPT.format(kinds=", ".join(kinds), lang=LANG_NAMES[lang])
        user = features.identify_text(self.core, dev, self.clock())

        def post(res):
            sug = features.parse_identify(res["text"], kinds)
            hint = dict(sug, state="open", t=self.clock(), lang=lang, **self._meta(res))
            self.hints.put(dev["id"], hint)
            return dict(hint, device=dev["id"])
        return self._run("identify", "identify", system, user, post, preview)

    def hint_decide(self, device_id, action: str) -> dict:
        res = self.hints.decide(str(device_id or "")[:80], str(action or ""))
        if res is None:
            raise ValueError("ai.err.hint_missing")
        try:
            self.core.structure_version += 1        # the map redraws with the new kind
        except Exception:
            pass
        return res

    def hints_view(self) -> list:
        out = []
        for did, hnt in self.hints.all().items():
            dev = self.core.store.device(did)
            if not dev or hnt.get("state") != "open":
                continue
            out.append({"device": did, "title": dev.get("user_title") or dev.get("title") or dev.get("ip"), "ip": dev.get("ip"),
                        "kind": hnt.get("kind"), "what": hnt.get("what") or "", "reason": hnt.get("reason") or "", "t": hnt.get("t")})
        out.sort(key=lambda x: -(x["t"] or 0))
        return out

    def nightly_identify(self, limit: int = features.NIGHT_MAX) -> int:
        """At night (only when switched on): suggestions for a few devices Lanloom is unsure about. Never a change."""
        s = self.settings()
        if not s["features"].get("nightly_identify") or not self.routes("identify", s):
            return 0
        done = 0
        for d in self.core.devices():
            if done >= limit:
                break
            if d.get("hidden") or d.get("is_self") or d.get("is_gateway") or d.get("kind") != d.get("auto_kind"):
                continue
            if d.get("certainty") not in ("unknown", None) and d.get("kind") != "unknown":
                continue
            if self.hints.peek(d["id"]):
                continue                   # asked before: open, accepted or dismissed - never ask again on its own
            try:
                job = self.identify(d["id"])
            except ValueError:
                break
            end = time.monotonic() + 600
            while time.monotonic() < end:
                v = self.job_view(job["id"])
                if v and v["state"] in ("done", "error"):
                    break
                time.sleep(0.5)
            v = self.job_view(job["id"]) or {}
            if v.get("error") in ("ai.err.daily_limit", "ai.err.not_ready"):
                break
            done += 1
        return done

    def seed_demo_hint(self) -> None:
        """Demo mode: an open suggestion for the first device Lanloom is unsure about (sample AI, no network)."""
        if not self.demo:
            return
        for d in self.core.devices():
            if d.get("is_self") or d.get("is_gateway") or d.get("hidden") or self.hints.peek(d["id"]):
                continue
            if not d.get("ip") or d.get("merged_into"):     # a device known only by its hardware address, or an interface of another
                continue
            if d.get("kind") == "unknown" or d.get("certainty") == "unknown":
                try:
                    job = self.identify(d["id"], "en")
                except ValueError:
                    return
                end = time.monotonic() + 10
                while time.monotonic() < end and (self.job_view(job["id"]) or {}).get("state") not in ("done", "error"):
                    time.sleep(0.05)
                return

    # firewall / network suggestions --------------------------------------------------
    def firewall_suggest(self, lang=None, preview: bool = False):
        """Suggested moves as a draft proposal. The AI never touches the draft: the user takes it over."""
        st = self.firewall.state() if self.firewall is not None else None
        if not st:
            raise ValueError("ai.err.no_firewall")
        from ..firewall.model import MAX_OBJECTS
        lang = self._lang(lang)
        facts, possible = features.firewall_facts(st, self.core)
        if not possible:
            raise ValueError("ai.err.no_moves")
        system = features.FIREWALL_PROMPT.format(max=MAX_OBJECTS, lang=LANG_NAMES[lang])

        def post(res):
            out = features.parse_firewall(res["text"], facts, possible, MAX_OBJECTS)
            out.update(self._meta(res))
            return out
        return self._run("firewall", "suggest_firewall", system, facts.text(), post, preview)

    # ------------------------------------------------------------------ local service: detect, install, pull, test
    def _update_dir(self) -> Path:
        return self.data_dir / "update"

    def _sidecar_alive(self) -> bool:
        if self.demo:
            return True
        try:
            info = json.loads((self._update_dir() / "sidecar.json").read_text(encoding="utf-8"))
            return self.clock() - float(info.get("t", 0)) <= 120
        except (OSError, ValueError, TypeError):
            return False

    def _sidecar_status(self) -> dict:
        try:
            return json.loads((self._update_dir() / runtime.STATUS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"state": "idle"}

    def setup_state(self) -> dict:
        st = dict(self.setup)
        if st.get("state") in ("installing",):
            side = self._sidecar_status()
            st["sidecar"] = {"state": side.get("state"), "log": (side.get("log") or [])[-1:]}
        return st

    def _candidates_basic(self) -> list:
        s = self.settings()
        urls = []
        if s["local"]["url"] and providers.get(s["local"]["kind"]).dialect == "native":
            urls.append(s["local"]["url"])
        urls += [runtime.service_url(True), runtime.service_url(False), "http://host.docker.internal:%d" % runtime.PORT]
        seen, out = set(), []
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def suggestions(self) -> list:
        """Computers in the inventory that could carry a model: servers, storage boxes and computers that are on,
        best first. Lanloom knows them already; nothing is scanned for this."""
        now = self.clock()
        out = []
        for d in self.core.devices():
            if d.get("hidden") or d.get("is_self") or d.get("kind") not in SUGGEST_KINDS or not _private(d.get("ip")):
                continue
            score = SUGGEST_KINDS[d["kind"]] * 2
            if d.get("status") == "up":
                score += 3
            if d.get("first_seen") and now - float(d["first_seen"]) > 3 * 86400:
                score += 1
            out.append({"id": d["id"], "ip": d["ip"], "title": d.get("title") or d["ip"], "kind": d["kind"],
                        "status": d.get("status"), "score": score})
        out.sort(key=lambda x: (-x["score"], x["title"].lower()))
        return out[:6]

    def discover(self, device_ids=None) -> dict:
        """Ask known addresses once on the local server's port: the basics plus the chosen (or suggested) devices."""
        if self.demo:
            found = [{"url": "http://192.0.2.20:%d" % runtime.PORT, "title": "Sample workstation", "version": "sample",
                      "models": ["sample-medium"], "this_host": False}]
            self._discovered = {"t": self.clock(), "found": found, "probed": 4}
            return dict(self._discovered)
        ids = [str(i)[:80] for i in (device_ids or [])][:PROBE_MAX_HOSTS] if isinstance(device_ids, list) else []
        devices = {d["id"]: d for d in self.core.devices()}
        targets = [(u, None) for u in self._candidates_basic()]
        chosen = [devices[i] for i in ids if i in devices] if ids else [devices[x["id"]] for x in self.suggestions() if x["id"] in devices]
        for d in chosen[:PROBE_MAX_HOSTS]:
            if _private(d.get("ip")):
                host = d["ip"] if ":" not in d["ip"] else "[%s]" % d["ip"]
                targets.append(("http://%s:%d" % (host, runtime.PORT), d))

        def probe(item):
            url, dev = item
            version = providers.native_version(url, self.opener)
            if not version:
                return None
            try:
                ids_ = Client("ollama", url, opener=self.opener).models()
            except AIError:
                ids_ = []
            return {"url": url, "title": (dev or {}).get("title") or "", "version": version, "models": ids_,
                    "this_host": self._is_this_host(url)}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="ai-probe") as ex:
            results = list(ex.map(probe, targets))
        found, seen = [], set()
        for r in results:
            if r and r["url"] not in seen:
                seen.add(r["url"])
                found.append(r)
        self._discovered = {"t": self.clock(), "found": found, "probed": len(targets)}
        return dict(self._discovered)

    def install_local(self) -> dict:
        """Ask the updater sidecar to start the local service next to Lanloom, then download and test a model."""
        if self._setup_busy():
            raise ValueError("ai.err.busy")
        if not self.demo and not self._sidecar_alive():
            raise ValueError("ai.err.no_sidecar")
        host = self._host()
        adv = hardware.advise(host["ram_gb"], host["cores"], host["gpu"], host["arch"], demo=self.demo)
        if adv["speed"] == "unsuitable":
            raise ValueError("ai.err.host_too_small")
        if not self.demo:
            self._write_sidecar_request("install")
        self._start_setup("sidecar", None, adv["model"])
        return self.setup_state()

    def use_existing(self, url: str, model: str = "") -> dict:
        """Take a server that discovery found; download the recommended model if it has none, then test."""
        if self._setup_busy():
            raise ValueError("ai.err.busy")
        known = {f["url"] for f in self._discovered.get("found") or []}
        try:
            url = providers.normalise_url("ollama", url)
        except AIError as exc:
            raise ValueError(exc.key)
        if url not in known:
            raise ValueError("ai.err.not_found")        # only what the server itself found
        if not model:
            host = self._host() if self._is_this_host(url) else None
            if host:
                model = hardware.advise(host["ram_gb"], host["cores"], host["gpu"], host["arch"], demo=self.demo)["model"]
        self._start_setup("existing", url, str(model or "")[:160])
        return self.setup_state()

    def remove_local(self) -> dict:
        if not self.demo:
            if not self._sidecar_alive():
                raise ValueError("ai.err.no_sidecar")
            self._write_sidecar_request("remove")
        s = self.settings()
        if s["local"].get("origin") == "sidecar":
            s["local"]["enabled"] = False
            self.store.set_setting("ai", {k: s[k] for k in DEFAULTS})
        self.setup = {"state": "idle"}
        return self.setup_state()

    def _write_sidecar_request(self, action: str) -> None:
        if action not in runtime.ACTIONS:
            raise ValueError("ai.err.internal")
        d = self._update_dir()
        d.mkdir(parents=True, exist_ok=True)
        payload = {"action": action, "nonce": secrets.token_hex(16), "requested": time.time()}
        tmp = d / (".ai-request.%s.tmp" % secrets.token_hex(6))
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, d / runtime.REQUEST_FILE)
        self.setup["nonce"] = payload["nonce"]

    def _setup_busy(self) -> bool:
        return self.setup.get("state") in ("installing", "waiting", "pulling", "testing") and \
            self._setup_thread is not None and self._setup_thread.is_alive()

    def _start_setup(self, source: str, url, model: str) -> None:
        nonce = self.setup.get("nonce")
        self.setup = {"state": "installing" if source == "sidecar" else "waiting", "source": source, "model": model,
                      "url": url, "completed": 0, "total": 0, "t": self.clock(), "nonce": nonce}
        self._setup_thread = threading.Thread(target=self._setup_run, args=(source, url, model), name="ai-setup", daemon=True)
        self._setup_thread.start()

    def _setup_fail(self, key: str) -> None:
        self.setup.update(state="failed", message_key=key, t=self.clock())

    def _setup_run(self, source: str, url, model: str, poll: float = 2.0, sleep=time.sleep) -> None:
        try:
            if self.demo:
                for pct in (0, 35, 70, 100):
                    self.setup.update(state="pulling", completed=pct, total=100)
                    sleep(0.3)
                self.setup.update(state="ready", url="", ms=420, answer="OK", t=self.clock())
                return
            if source == "sidecar":
                deadline = self.clock() + 45 * 60
                while True:
                    side = self._sidecar_status()
                    if side.get("nonce") == self.setup.get("nonce"):
                        if side.get("state") == "running" and side.get("url"):
                            url = side["url"]
                            break
                        if side.get("state") == "failed":
                            return self._setup_fail(side.get("message_key") or "ai.err.install_failed")
                    if self.clock() > deadline:
                        return self._setup_fail("ai.err.install_timeout")
                    sleep(poll)
            self.setup.update(state="waiting", url=url)
            deadline = self.clock() + 120
            while not providers.native_version(url, self.opener):
                if self.clock() > deadline:
                    return self._setup_fail("ai.err.unreachable")
                sleep(poll)
            client = Client("ollama", url, limits=providers.TEST_LIMITS_LOCAL, opener=self.opener)
            have = client.models()
            if not model:
                model = client.recommended(have) if have else hardware.TIERS[1][1]
            if model not in have:
                self.setup.update(state="pulling", model=model, completed=0, total=0)

                def progress(status, completed, total):
                    if total:
                        self.setup.update(completed=completed, total=total, status=status)
                providers.native_pull(url, model, progress, self.opener)
            self.setup.update(state="testing", model=model)
            res = client.test(model)
            s = self.settings()
            s["local"].update(enabled=True, kind="ollama", url=url, model=model, origin=source if source == "sidecar" else "detected")
            self.store.set_setting("ai", {k: s[k] for k in DEFAULTS})
            self.setup.update(state="ready", ms=res["ms"], answer=res["text"][:80], t=self.clock())
        except AIError as exc:
            self._setup_fail(exc.key)
        except Exception as exc:
            log.warning("local AI setup failed: %s", type(exc).__name__)
            self._setup_fail("ai.err.internal")

    # ------------------------------------------------------------------ another computer: installer and pairing
    def new_pairing(self) -> dict:
        code = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(8))
        expires = self.clock() + PAIR_VALID_S
        self.store.set_setting("ai_pairing", {"hash": _hash_code(code), "expires": expires, "job": None})
        return {"code": code[:4] + "-" + code[4:], "expires": expires,
                "os": [{"id": k, "label": (DEMO_OS_LABELS if self.demo else OS_LABELS)[k], "shell": "ps1" if v.endswith(".ps1") else "sh"} for k, v in INSTALLER_OS.items()]}

    def _pairing_ok(self, code) -> bool:
        c = normalise_code(code)
        rec = self.store.get_setting("ai_pairing", None) or {}
        if not c or not rec.get("hash") or self.clock() > float(rec.get("expires") or 0):
            return False
        return hmac.compare_digest(rec["hash"], _hash_code(c))

    def installer(self, os_name: str, code, host_header: str, scheme: str, prefix: str = "") -> tuple:
        """(filename, text) of the setup script for another computer, with this Lanloom's address and the code."""
        if os_name not in INSTALLER_OS:
            raise ValueError("ai.err.os")
        if not self._pairing_ok(code):
            raise PermissionError("ai.err.code")
        host = str(host_header or "")
        if not HOST_HEADER_RX.match(host) or scheme not in ("http", "https"):
            raise ValueError("ai.err.host_header")
        c = normalise_code(code)
        text = (INSTALLER_DIR / INSTALLER_OS[os_name]).read_text(encoding="utf-8")
        if prefix and not re.fullmatch(r"(/[A-Za-z0-9._~-]+)+", prefix):
            prefix = ""
        text = text.replace("__LANLOOM_URL__", "%s://%s%s" % (scheme, host, prefix)).replace("__PAIR_CODE__", c[:4] + "-" + c[4:])
        text = text.replace("__AI_PORT__", str(runtime.PORT))
        name = "lanloom-ai-setup.ps1" if os_name == "windows" else "lanloom-ai-setup.sh"
        return name, text

    def pair_hello(self, code, hw: dict) -> dict:
        """First call of the setup script: the code is checked before anything is downloaded; the advice comes back."""
        if not self._pairing_ok(code):
            raise PermissionError("ai.err.code")
        hw = hw if isinstance(hw, dict) else {}
        adv = hardware.advise(hw.get("ram_gb"), hw.get("cores"), str(hw.get("gpu") or "")[:40], str(hw.get("arch") or "")[:20])
        return {"ok": True, "model": adv["model"], "download_gb": adv["download_gb"], "speed": adv["speed"],
                "notes": adv["notes"], "port": runtime.PORT}

    def pair(self, code, peer_ip: str, data: dict) -> dict:
        """The setup script is done on its side: Lanloom now reaches the computer itself and asks the model a question."""
        if not self._pairing_ok(code):
            raise PermissionError("ai.err.code")
        if not _private(peer_ip):
            raise ValueError("ai.err.peer")
        try:
            port = int(data.get("port") or runtime.PORT)
        except (TypeError, ValueError):
            raise ValueError("ai.err.port")
        if not 1024 <= port <= 65535:
            raise ValueError("ai.err.port")
        model = str(data.get("model") or "").strip()[:160]
        if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,160}", model):
            raise ValueError("ai.err.model_missing")
        host = peer_ip if ":" not in peer_ip else "[%s]" % peer_ip
        url = "http://%s:%d" % (host, port)
        rec = self.store.get_setting("ai_pairing", {}) or {}

        def work(job):
            job["state"] = "waiting"
            if not providers.native_version(url, self.opener):
                raise AIError("ai.err.lan_unreachable")
            c = Client("ollama", url, limits=providers.TEST_LIMITS_LOCAL, opener=self.opener)
            if model not in c.models():
                raise AIError("ai.err.model")
            res = c.test(model)
            s = self.settings()
            s["local"].update(enabled=True, kind="ollama", url=url, model=model, origin="paired")
            self.store.set_setting("ai", {k: s[k] for k in DEFAULTS})
            with self._lock:
                self.store.set_setting("ai_pairing", {"hash": "", "expires": 0, "job": None})    # one-time code: used up
            return {"url": url, "model": model, "ms": res["ms"], "answer": res["text"][:80]}
        job = self._new_job("pair", work)
        self._jobs[job["id"]]["code_hash"] = _hash_code(normalise_code(code))
        with self._lock:         # a job that was quick must not bring the used-up code back to life
            cur = self.store.get_setting("ai_pairing", {}) or {}
            if cur.get("hash") and cur.get("hash") == rec.get("hash"):
                self.store.set_setting("ai_pairing", dict(cur, job=job["id"]))
        return job

    def pair_status(self, code, job_id: str) -> Optional[dict]:
        """State of a pairing job for the setup script; it proves itself with the same code it paired with."""
        job = self._jobs.get(str(job_id or ""))
        c = normalise_code(code)
        if not job or job["kind"] != "pair" or not c or not hmac.compare_digest(job.get("code_hash", ""), _hash_code(c)):
            return None
        out = {"state": job["state"], "error": job["error"]}
        if job["state"] == "done":
            out["result"] = {"ms": job["result"]["ms"], "model": job["result"]["model"]}
        return out


# ---------------------------------------------------------------------- helpers
def _private(ip) -> bool:
    try:
        a = ipaddress.ip_address(str(ip or "").split("%")[0])
    except ValueError:
        return False
    return (a.is_private or a.is_loopback or a.is_link_local) and not a.is_multicast and not a.is_unspecified


def _ago(now: float, t) -> str:
    try:
        s = max(0, now - float(t))
    except (TypeError, ValueError):
        return "unknown"
    if s < 3600:
        return "%d minutes ago" % (s // 60)
    if s < 172800:
        return "%d hours ago" % (s // 3600)
    return "%d days ago" % (s // 86400)


def _device_text(d: dict, now: float) -> str:
    from ..nightly.render import strings
    kind = strings("en").get("kind.%s" % d.get("kind"), str(d.get("kind") or "device"))
    lines = ["Subject: device", "Name: %s" % (d.get("title") or d.get("ip") or "?"), "Kind: %s" % kind,
             "Address: %s" % d.get("ip")]
    if d.get("mac"):
        lines.append("Hardware address: %s" % d["mac"])
    if d.get("hostnames"):
        lines.append("Host names: %s" % ", ".join(map(str, d["hostnames"][:6])))
    lines.append("Status: %s" % d.get("status"))
    if d.get("is_gateway"):
        lines.append("Role: the network's gateway (router)")
    if d.get("is_self"):
        lines.append("Role: the computer Lanloom runs on")
    svc = [str(x.get("name") if isinstance(x, dict) else x) for x in (d.get("services") or [])][:10]
    if svc:
        lines.append("Services: %s" % ", ".join(svc))
    ports = []
    for p in (d.get("ports") or [])[:12]:
        if isinstance(p, dict):
            ports.append(("%s/%s %s" % (p.get("port"), p.get("proto", "tcp"), p.get("service") or "")).strip())
    if ports:
        lines.append("Open ports: %s" % "; ".join(ports))
    names = []
    for m in (d.get("mdns") or [])[:12]:
        n = str(m.get("name") or m.get("type") or "") if isinstance(m, dict) else ""
        if n and n not in names:
            names.append(n)
    if names:
        lines.append("Announced services: %s" % ", ".join(names[:6]))
    lines.append("Identification: %s (%s)" % (d.get("certainty"), round(float(d.get("confidence") or 0), 2)))
    lines.append("First seen: %s · last seen: %s" % (_ago(now, d.get("first_seen")), _ago(now, d.get("last_seen"))))
    return "\n".join(lines)


def _rule_text(st: dict, p: dict) -> str:
    zones = {z.get("id"): z.get("name") for z in st.get("zones") or [] if isinstance(z, dict)}
    nets = {n.get("id"): n.get("name") for n in st.get("networks") or [] if isinstance(n, dict)}

    def side(sd):
        sd = sd if isinstance(sd, dict) else {}
        what = zones.get(sd.get("zone"), sd.get("zone") or "?")
        m = sd.get("match") if isinstance(sd.get("match"), dict) else None
        if m:
            items = m.get("items") or []
            if m.get("kind") == "network":
                items = [nets.get(i, i) for i in items]
            what += " (%s: %s)" % (m.get("kind"), ", ".join(map(str, items[:6])) or m.get("count", ""))
            if m.get("opposite"):
                what = "not " + what
        if sd.get("ports"):
            what += " ports %s" % sd["ports"]
        return what
    lines = ["Subject: rule", "Rule: %s" % (p.get("name") or "?"), "Action: %s" % p.get("action"),
             "From: %s" % side(p.get("src")), "To: %s" % side(p.get("dst")),
             "Protocol: %s" % (p.get("proto") or "any")]
    if p.get("ports"):
        lines.append("Ports: %s" % p["ports"])
    if p.get("states"):
        lines.append("Connection states: %s" % ", ".join(map(str, p["states"])))
    lines.append("Enabled: %s" % ("yes" if p.get("enabled") else "no"))
    if p.get("builtin"):
        lines.append("Built in: yes (made by the controller itself)")
    if p.get("scheduled"):
        lines.append("Scheduled: yes")
    return "\n".join(lines)
