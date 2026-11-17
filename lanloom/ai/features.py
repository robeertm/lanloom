"""What the AI does with Lanloom's data (KI2): "Ask your network", the plain-words paragraph of the nightly report,
help with identifying unknown devices, and firewall/network suggestions.

Everything here only reads. The text a model sees is built on the server from Lanloom's own data; the browser only
sends the question. The manager (``manager.py``) sends it: through the privacy layer for public services, with the
daily limit and the per-function permission. What comes back is checked before anyone sees it:

* device references ``[D3]`` and view references ``[view:paths]`` are resolved against the very list the model got,
  unknown ones are dropped (an answer can only link to what exists);
* an identification suggestion must name one of Lanloom's device kinds, and becomes a *hint with low confidence*
  only after the user confirmed it;
* a firewall suggestion is a list of moves that the editor itself allows; it is shown as a draft proposal, and only
  the user's click puts it into the draft, which then goes through the usual preview, confirmation and executor.
  The AI never writes a draft, a plan or a configuration.
"""
import json
import re
import time

VIEWS = ("map", "network", "devices", "paths", "firewall", "reports", "settings")
ASK_MAX_DEVICES = 40
ASK_MAX_CHARS = 9000
QUESTION_MAX = 500
HISTORY_TURNS = 6
HISTORY_CHARS = 1200
HINT_CONFIDENCE = 0.3          # a confirmed AI hint counts like weak evidence (the "probable" floor of the classifier)
HINT_KEEP = 400
NIGHT_MAX = 5                  # identification suggestions per night at most
REF_RX = re.compile(r"\[(D|N)(\d{1,3})\]")
VIEW_RX = re.compile(r"\[view:([a-z]{3,12})\]")
WORD_RX = re.compile(r"[\w.:-]{3,}", re.U)
INFRA = ("router", "switch", "ap", "nas", "server", "hub", "broker")

ASK_PROMPT = (
    "You are the assistant inside Lanloom, a read-only map of a home or small office network. Answer the user's "
    "question using only the facts below; if they do not contain the answer, say so plainly and suggest which view "
    "could help. Answer in {lang}, in plain words, at most 150 words, no headings. Every device in the facts has a "
    "reference such as [D3]: when you mention a device, write its reference right after its name, exactly like "
    "[D3]. To point to a view, write one of: [view:map], [view:network], [view:devices], [view:paths], "
    "[view:firewall], [view:reports]. Placeholders in square brackets such as [IP-1] or [HOST-2] stand for hidden "
    "real values: keep them exactly as written. You can only read. Never give commands or step-by-step instructions "
    "that change a configuration; if a change could help, say so in one sentence and add that Lanloom shows a "
    "preview and asks for confirmation before anything is changed.")

REPORT_PROMPT = (
    "You write the short opening paragraph of a network's nightly report for a person without network knowledge. "
    "Use only the report below. Answer in {lang}, one paragraph, at most 90 words, no headings, no lists. Say first "
    "whether something needs a look, then the most important points in plain words. Do not invent anything and do "
    "not add advice that changes a configuration. Placeholders in square brackets such as [IP-1] or [HOST-2] stand "
    "for hidden real values: keep them exactly as written.")

IDENTIFY_PROMPT = (
    "You help identify one device in a home network from the clues Lanloom collected. Choose the most likely kind "
    "from this list only: {kinds}. Reply with JSON only, no other text: "
    '{{"kind": "<one kind from the list>", "label": "<short neutral description, at most 6 words>", '
    '"reason": "<one or two sentences in {lang} naming the clues>"}}. '
    'If the clues are not enough, use "unknown". Placeholders in square brackets such as [IP-1] stand for hidden '
    "real values: keep them exactly as written.")

FIREWALL_PROMPT = (
    "You review the networks of a home network and suggest which devices could move to a better network, for "
    "example cameras or smart-home gadgets into a separate network, guests into the guest network. Use only the "
    "facts below, and only the moves listed under 'Possible moves'. Suggest at most {max} moves, fewer is fine, none "
    "is fine. Reply with JSON only, no other text: "
    '{{"moves": [{{"device": "D1", "network": "N2", "reason": "<one sentence in {lang}>"}}], "summary": "<one sentence in {lang}>"}}. '
    "These are suggestions for a draft: the user reviews them, Lanloom shows a preview and asks for confirmation "
    "before anything is changed. Placeholders in square brackets such as [IP-1] stand for hidden real values: keep "
    "them exactly as written.")


# ---------------------------------------------------------------------- small helpers
def _clip(text, n: int) -> str:
    text = str(text or "").replace("\r", " ").strip()
    return text[:n]


def _words(text: str) -> set:
    return {w.lower().strip(".:-") for w in WORD_RX.findall(str(text or "")) if len(w.strip(".:-")) >= 3}


def _status(d: dict) -> str:
    return str(d.get("status") or "unknown")


def _title(d: dict) -> str:
    if d.get("is_self"):
        return "this host (Lanloom)"
    return str(d.get("title") or d.get("auto_title") or d.get("ip") or "?")


def _kind_word(kind: str) -> str:
    from ..nightly.render import strings
    return strings("en").get("kind.%s" % kind, str(kind or "device"))


def fmt_bytes(n) -> str:
    n = float(n or 0)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1000.0
    return "%.1f TB" % n


def clean_question(q) -> str:
    q = _clip(q, QUESTION_MAX)
    if not q:
        raise ValueError("ai.err.question")
    return q


def clean_history(history) -> list:
    """The last turns of the conversation (kept by the browser): [{role: user|assistant, text}]"""
    out = []
    for turn in (history if isinstance(history, list) else [])[-HISTORY_TURNS:]:
        if not isinstance(turn, dict) or turn.get("role") not in ("user", "assistant"):
            continue
        text = _clip(turn.get("text"), HISTORY_CHARS)
        if text:
            out.append({"role": turn["role"], "text": text})
    return out


def parse_json(text: str):
    """The first JSON object in a model's answer (models like to wrap it in prose or code fences)."""
    text = str(text or "")
    start = text.find("{")
    while start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        pass
                    break
        start = text.find("{", start + 1)
    return None


# ---------------------------------------------------------------------- "Ask your network": facts and references
class Facts:
    """The facts for one question and the references in them (D1… devices, N1… networks)."""

    def __init__(self):
        self.lines = []
        self.devices = {}       # "D3" -> {"id", "title"}
        self.networks = {}      # "N2" -> {"id", "name"}
        self._dev_ref = {}      # device id -> "D3"

    def dev(self, d: dict) -> str:
        did = str(d.get("id") or "")
        ref = self._dev_ref.get(did)
        if ref is None:
            ref = "D%d" % (len(self.devices) + 1)
            self._dev_ref[did] = ref
            self.devices[ref] = {"id": did, "title": _title(d)}
        return ref

    def ref_of(self, device_id) -> str:
        return self._dev_ref.get(str(device_id or ""), "")

    def add(self, line: str = "") -> None:
        self.lines.append(line)

    def text(self, limit: int = ASK_MAX_CHARS) -> str:
        out, n = [], 0
        for line in self.lines:
            if n + len(line) + 1 > limit:
                out.append("(more facts left out)")
                break
            out.append(line)
            n += len(line) + 1
        return "\n".join(out)


def _relevance(d: dict, words: set) -> int:
    hay = _words(" ".join([str(d.get("title") or ""), str(d.get("auto_title") or ""), " ".join(map(str, d.get("hostnames") or [])),
                           str(d.get("kind") or ""), _kind_word(d.get("kind")), str(d.get("vendor") or ""), str(d.get("ip") or ""),
                           str(d.get("model") or "")]))
    score = 10 * len(words & hay)
    if d.get("is_gateway"):
        score += 4
    if d.get("kind") in INFRA:
        score += 2
    if _status(d) == "down":
        score += 2
    if d.get("certainty") == "unknown" or d.get("kind") == "unknown":
        score += 1
    return score


def network_facts(core, question: str, history: list, now: float, firewall=None) -> Facts:
    """Everything a question may need, in a compact form: devices, availability, data paths, firewall, report."""
    f = Facts()
    words = _words(question + " " + " ".join(t["text"] for t in history if t["role"] == "user"))
    try:
        devices = [d for d in core.devices() if not d.get("hidden")]
    except Exception:
        devices = []
    by_id = {d.get("id"): d for d in devices}
    up = sum(1 for d in devices if _status(d) == "up")
    down = sum(1 for d in devices if _status(d) == "down")
    unknown_kind = sum(1 for d in devices if d.get("kind") == "unknown")
    f.add("Network overview: %d devices (%d online, %d offline, %d status unknown), %d of unknown kind." %
          (len(devices), up, down, len(devices) - up - down, unknown_kind))
    kinds = {}
    for d in devices:
        kinds[d.get("kind") or "unknown"] = kinds.get(d.get("kind") or "unknown", 0) + 1
    f.add("Kinds: " + ", ".join("%s %d" % (_kind_word(k), n) for k, n in sorted(kinds.items(), key=lambda x: -x[1])))
    f.add("")
    ranked = sorted(devices, key=lambda d: (-_relevance(d, words), _title(d).lower()))[:ASK_MAX_DEVICES]
    f.add("Devices (reference: name | kind | address | status | maker | identification):")
    for d in ranked:
        ref = f.dev(d)
        parts = [_title(d), _kind_word(d.get("kind")), str(d.get("ip") or "-"), _status(d)]
        parts.append(str(d.get("vendor") or "-"))
        parts.append(str(d.get("certainty") or "unknown"))
        extra = []
        if d.get("is_gateway"):
            extra.append("the gateway (router)")
        if d.get("model"):
            extra.append("model " + str(d["model"]))
        svc = [str(x.get("name") if isinstance(x, dict) else x) for x in (d.get("services") or [])][:4]
        if svc:
            extra.append("services " + ", ".join(svc))
        f.add("[%s] %s%s" % (ref, " | ".join(parts), (" | " + "; ".join(extra)) if extra else ""))
    if len(devices) > len(ranked):
        f.add("(%d further devices not listed)" % (len(devices) - len(ranked)))
    f.add("")
    _availability(f, core, by_id, now)
    _paths(f, core, by_id)
    _firewall(f, core, firewall)
    _report(f, core)
    return f


def _availability(f: Facts, core, by_id: dict, now: float) -> None:
    journal = getattr(getattr(core, "reports", None), "journal", None)
    if journal is None:
        return
    try:
        evs = journal.between(now - 86400, now, ("dev_new", "dev_offline", "dev_back", "dev_gone", "src_fail", "src_ok",
                                                "wan_down", "wan_up"))
    except Exception:
        return
    if not evs:
        f.add("Availability in the last 24 hours: no changes recorded.")
        f.add("")
        return
    f.add("Availability in the last 24 hours (newest last):")
    words = {"dev_new": "new device", "dev_offline": "went offline", "dev_back": "back online", "dev_gone": "not seen for a day",
             "src_fail": "data source failing", "src_ok": "data source working again", "wan_down": "internet line down",
             "wan_up": "internet line up again"}
    for ev in evs[-12:]:
        data = ev.get("data") or {}
        what = words.get(ev.get("kind"), ev.get("kind"))
        hours = max(0, int((now - float(ev.get("t") or now)) // 3600))
        d = by_id.get(data.get("id"))
        who = ("%s [%s]" % (_title(d), f.dev(d))) if d else str(data.get("title") or data.get("name") or "")
        f.add("- %s: %s (%d hours ago)" % (what, who or "-", hours))
    f.add("")


def _paths(f: Facts, core, by_id: dict) -> None:
    fn = getattr(core, "paths_summary", None)
    if not callable(fn):
        return
    try:
        s = fn("24h")
    except Exception:
        return
    tot = s.get("totals") or {}
    if not (tot.get("tx") or tot.get("rx")):
        return
    f.add("Data paths of the last 24 hours (traffic to the internet): sent %s, received %s, %d countries%s." % (
        fmt_bytes(tot.get("tx") or 0), fmt_bytes(tot.get("rx") or 0), int(tot.get("countries") or 0),
        (", %d %% outside the EU" % round(100 * tot["outside_eu_share"])) if tot.get("outside_eu_share") is not None else ""))
    top = []
    for d in (s.get("devices") or [])[:5]:
        dev = by_id.get(d.get("id"))
        name = ("%s [%s]" % (_title(dev), f.dev(dev))) if dev else ("this host" if d.get("self") else str(d.get("title") or "?"))
        top.append("%s %s" % (name, fmt_bytes(int(d.get("tx") or 0) + int(d.get("rx") or 0))))
    if top:
        f.add("Most traffic: " + "; ".join(top))
    cc = [str(c.get("cc") or "?") for c in (s.get("countries") or [])[:6]]
    if cc:
        f.add("Main countries (codes): " + ", ".join(cc))
    for u in (s.get("unusual") or [])[:4]:
        dev = by_id.get(u.get("device"))
        name = ("%s [%s]" % (_title(dev), f.dev(dev))) if dev else "a device"
        if u.get("kind") == "new_country":
            f.add("Unusual: %s talked to a new country (%s)." % (name, u.get("cc")))
        else:
            f.add("Unusual: %s sent much more than usual." % name)
    f.add("")


def _firewall(f: Facts, core, firewall) -> None:
    try:
        live = core.live_configs() if callable(getattr(core, "live_configs", None)) else []
    except Exception:
        live = []
    for entry in (live or [])[:2]:
        st = entry.get("state") or {}
        pols = [p for p in st.get("policies") or [] if isinstance(p, dict)]
        nets = [n for n in st.get("networks") or [] if isinstance(n, dict)]
        zones = {z.get("id"): z.get("name") for z in st.get("zones") or [] if isinstance(z, dict)}
        f.add("Firewall as read from %s (read-only): %d networks, %d rules (%d enabled)." % (
            entry.get("name") or "the controller", len(nets), len(pols), sum(1 for p in pols if p.get("enabled"))))
        if nets:
            f.add("Networks: " + "; ".join("%s (zone %s)" % (n.get("name"), zones.get(n.get("zone"), n.get("zone") or "-")) for n in nets[:10]))
        for p in [p for p in pols if not p.get("builtin")][:12]:
            src, dst = p.get("src") or {}, p.get("dst") or {}
            f.add("- rule %s: %s from %s to %s%s" % (p.get("name") or "?", p.get("action"), zones.get(src.get("zone"), src.get("zone") or "any"),
                                                   zones.get(dst.get("zone"), dst.get("zone") or "any"), "" if p.get("enabled") else " (off)"))
        f.add("")
    st = None
    if firewall is not None:
        try:
            st = firewall.state()
        except Exception:
            st = None
    if st and not live:
        nets = [n for n in st.get("networks") or [] if isinstance(n, dict)]
        counts = {}
        for d in st.get("devices") or []:
            counts[d.get("network")] = counts.get(d.get("network"), 0) + 1
        f.add("Networks in the firewall editor (%s): %s" % (
            "simulation" if (st.get("controller") or {}).get("kind") == "simulation" else "controller",
            "; ".join("%s (%d devices%s)" % (n.get("name"), counts.get(n.get("id"), 0), ", management" if n.get("management") else "") for n in nets[:10])))
        f.add("")


def _report(f: Facts, core) -> None:
    reports = getattr(core, "reports", None)
    latest = getattr(reports, "latest", None)
    if not callable(latest):
        return
    try:
        rep = latest()
    except Exception:
        return
    if not rep:
        return
    f.add("Latest nightly report (%s): %d points need a look." % (rep.get("day"), int(rep.get("points") or 0)))
    for line in report_lines(rep, reports, limit=8):
        f.add("- " + line)
    f.add("")


PRIVATE_SECTIONS = ("hub", "hub_radio")    # report sections the AI sees only as their summary line (counts, no names)


def report_lines(rep: dict, reports, limit: int = 30, points_first: bool = True) -> list:
    from ..nightly.render import Renderer
    try:
        tz = reports.tz()
    except Exception:
        tz = None
    r = Renderer("en", tz, rep.get("day") or "")
    items = []
    for sec in rep.get("sections") or []:
        title = r.tr(sec.get("title") or "")
        if sec.get("id") in PRIVATE_SECTIONS:      # names of automations may name people: counts only (hub_privacy)
            items.append((2, "%s: %s" % (title, r.line(sec.get("summary") or {}))))
            continue
        for it in sec.get("items") or []:
            items.append((0 if it.get("point") else 1, "%s: %s%s" % (title, r.line(it), " (needs a look)" if it.get("point") else "")))
        if not sec.get("items"):
            items.append((2, "%s: %s" % (title, r.line(sec.get("summary") or {}))))
    if points_first:
        items.sort(key=lambda x: x[0])
    return [x[1] for x in items[:limit]]


def ask_user_text(facts: Facts, question: str, history: list) -> str:
    lines = ["Facts:", facts.text(), ""]
    if history:
        lines.append("Conversation so far:")
        for turn in history:
            lines.append("%s: %s" % ("User" if turn["role"] == "user" else "Assistant", turn["text"]))
        lines.append("")
    lines.append("Question: " + question)
    return "\n".join(lines)


def resolve_refs(text: str, facts: Facts) -> dict:
    """Links for the references in an answer; unknown references are removed from the text."""
    links, seen = [], set()

    def dev(m):
        ref = m.group(1) + m.group(2)
        hit = facts.devices.get(ref) if m.group(1) == "D" else None
        if not hit:
            return ""
        if ref not in seen:
            seen.add(ref)
            links.append({"ref": ref, "kind": "device", "id": hit["id"], "title": hit["title"], "href": "#/devices/" + hit["id"]})
        return "[%s]" % ref

    def view(m):
        v = m.group(1)
        if v not in VIEWS:
            return ""
        key = "view:" + v
        if key not in seen:
            seen.add(key)
            links.append({"ref": key, "kind": "view", "id": v, "href": "#/" + v})
        return "[view:%s]" % v
    text = REF_RX.sub(dev, str(text or ""))
    text = VIEW_RX.sub(view, text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return {"text": text, "links": links}


# ---------------------------------------------------------------------- identification help
def identify_text(core, dev: dict, now: float) -> str:
    from .manager import _device_text
    lines = [_device_text(dev, now).replace("Subject: device", "Subject: identify")]
    if dev.get("vendor"):
        lines.append("Manufacturer from the hardware address: %s" % dev["vendor"])
    elif dev.get("origin"):
        lines.append("Hardware address origin: %s" % dev["origin"])
    if dev.get("model"):
        lines.append("Model: %s" % dev["model"])
    if dev.get("firmware"):
        lines.append("Firmware: %s" % dev["firmware"])
    feats = dev.get("features") or {}
    for k in ("upnp_model", "sys_descr", "wsd_types", "netbios"):
        v = feats.get(k)
        if v:
            lines.append("%s: %s" % (k.replace("_", " "), ", ".join(map(str, v)) if isinstance(v, list) else v))
    for s in (dev.get("ssdp") or [])[:3]:
        if isinstance(s, dict):
            bits = [str(s.get(k)) for k in ("friendly_name", "model", "manufacturer") if s.get(k)]
            if bits:
                lines.append("Announces (UPnP): %s" % ", ".join(bits))
    if dev.get("reasons"):
        lines.append("Clues Lanloom already used: %s" % ", ".join(map(str, dev["reasons"][:6])))
    return "\n".join(lines)


def parse_identify(text: str, kinds) -> dict:
    obj = parse_json(text) or {}
    kind = str(obj.get("kind") or "").strip().lower()
    if kind not in kinds or kind in ("self", "internet"):
        kind = "unknown"
    what = _clip(obj.get("label") or obj.get("what"), 60)
    reason = _clip(obj.get("reason"), 400)
    if not reason:
        reason = _clip(re.sub(r"\{.*\}", "", str(text or ""), flags=re.S), 400)
    return {"kind": kind, "what": what, "reason": reason}


# ---------------------------------------------------------------------- firewall / network suggestions
def firewall_facts(st: dict, core) -> tuple:
    """Facts for the suggestion, the references and the moves the editor allows ({D: [N, ...]})."""
    from ..firewall import model
    f = Facts()
    nets = [n for n in st.get("networks") or [] if isinstance(n, dict)]
    zones = {z.get("id"): z for z in st.get("zones") or [] if isinstance(z, dict)}
    net_ref = {}
    f.add("Networks (reference: name | zone | internet | management):")
    for i, n in enumerate(nets, 1):
        ref = "N%d" % i
        net_ref[n["id"]] = ref
        f.networks[ref] = {"id": n["id"], "name": str(n.get("name") or "?")}
        z = zones.get(n.get("zone")) or {}
        f.add("[%s] %s | zone %s | internet %s | %s" % (ref, n.get("name"), z.get("name") or n.get("zone") or "-",
                                                        "yes" if z.get("internet", True) else "no", "management" if n.get("management") else "normal"))
    allow = ["%s -> %s" % (a, b) for a, b in (st.get("allow") or [])[:20] if isinstance(a, str) and isinstance(b, str)]
    if allow:
        f.add("Zones that may reach each other: " + "; ".join(allow))
    f.add("")
    f.add("Devices (reference: name | kind | network):")
    possible = {}
    for d in [d for d in st.get("devices") or [] if isinstance(d, dict)][:80]:
        ref = f.dev({"id": d["id"], "title": d.get("name") or d.get("ip") or "?"})
        f.add("[%s] %s | %s | %s" % (ref, d.get("name") or "?", _kind_word(d.get("kind")), net_ref.get(d.get("network"), "-")))
        ok = [net_ref[nid] for nid, why in model.drop_targets(st, d).items() if why is None and nid in net_ref]
        if ok:
            possible[ref] = ok
    f.add("")
    f.add("Possible moves (device: networks it may go to):")
    if possible:
        for ref, nets_ok in possible.items():
            f.add("%s: %s" % (ref, ", ".join(nets_ok)))
    else:
        f.add("none")
    return f, possible


def parse_firewall(text: str, facts: Facts, possible: dict, max_moves: int) -> dict:
    obj = parse_json(text) or {}
    out, seen = [], set()
    for m in obj.get("moves") or []:
        if not isinstance(m, dict):
            continue
        d, n = str(m.get("device") or "").strip("[] "), str(m.get("network") or "").strip("[] ")
        if d in seen or n not in possible.get(d, []):
            continue                     # only what the editor itself would allow, one move per device
        seen.add(d)
        out.append({"device": facts.devices[d]["id"], "device_title": facts.devices[d]["title"],
                    "network": facts.networks[n]["id"], "network_name": facts.networks[n]["name"],
                    "reason": _clip(m.get("reason"), 300)})
        if len(out) >= max_moves:
            break
    return {"moves": out, "summary": _clip(obj.get("summary"), 300)}


# ---------------------------------------------------------------------- hints (confirmed suggestions)
class Hints:
    """Identification suggestions by device: open -> accepted | dismissed. Only accepted ones reach the classifier."""

    def __init__(self, store, clock=time.time):
        self.store = store
        self.clock = clock
        self._cache = None          # read once, refreshed by every write (Hints is the only writer)

    def all(self) -> dict:
        if self._cache is None:
            h = self.store.get_setting("ai_hints", {}) or {}
            self._cache = h if isinstance(h, dict) else {}
        return json.loads(json.dumps(self._cache))

    def _save(self, h: dict) -> None:
        self.store.set_setting("ai_hints", h)
        self._cache = h

    def peek(self, device_id: str):
        """The hint of one device without copying the table (for the device views; do not modify)."""
        if self._cache is None:
            self.all()
        return self._cache.get(str(device_id or ""))

    def get(self, device_id: str):
        return self.all().get(str(device_id or ""))

    def put(self, device_id: str, hint: dict) -> dict:
        h = self.all()
        h[str(device_id)] = hint
        if len(h) > HINT_KEEP:
            for k in sorted(h, key=lambda k: h[k].get("t", 0))[:len(h) - HINT_KEEP]:
                h.pop(k, None)
        self._save(h)
        return hint

    def decide(self, device_id: str, action: str):
        h = self.all()
        cur = h.get(str(device_id or ""))
        if not cur:
            return None
        if action == "accept":
            if cur.get("kind") in (None, "", "unknown"):
                raise ValueError("ai.err.hint_unknown")
            cur["state"] = "accepted"
        elif action == "dismiss":
            cur["state"] = "dismissed"
        elif action == "forget":
            h.pop(str(device_id), None)
            self._save(h)
            return {"state": "none"}
        else:
            raise ValueError("ai.err.hint_action")
        cur["decided"] = self.clock()
        h[str(device_id)] = cur
        self._save(h)
        return cur

    def open_count(self, since: float = 0) -> int:
        return sum(1 for v in self.all().values() if v.get("state") == "open" and v.get("t", 0) >= since)


def apply_hint(view: dict, hint) -> dict:
    """A confirmed AI hint as weak evidence: it fills in a kind only where Lanloom has nothing better.
    A kind the user chose, and anything the classifier is at least as sure of, wins."""
    if not hint or hint.get("state") not in ("open", "accepted"):
        return view
    view["ai_hint"] = {"kind": hint.get("kind"), "what": hint.get("what") or "", "reason": hint.get("reason") or "",
                       "state": hint.get("state"), "route": hint.get("route"), "service": hint.get("label") or "",
                       "model": hint.get("model") or "", "pseudonymised": bool(hint.get("pseudonymised")), "t": hint.get("t")}
    if hint.get("state") != "accepted":
        return view
    if view.get("kind") != view.get("auto_kind"):
        return view                       # the user's own choice
    if view.get("is_self") or view.get("is_gateway"):
        return view
    if view.get("auto_kind") == "unknown" or float(view.get("confidence") or 0) < HINT_CONFIDENCE:
        view["kind"] = hint["kind"]
        view["confidence"] = HINT_CONFIDENCE
        view["certainty"] = "probable"
        view["reasons"] = list(view.get("reasons") or []) + ["ai:hint"]
    return view
