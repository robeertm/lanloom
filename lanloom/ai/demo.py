"""Sample answers for demo mode: no network, no model, the same shape as a real answer.
The texts are translations (``ai.demo.answer.*`` in ``lanloom/i18n``)."""
import json
import re
from pathlib import Path

I18N = Path(__file__).resolve().parent.parent / "i18n"


def _text(lang: str, key: str) -> str:
    try:
        return json.loads((I18N / ("%s.json" % lang)).read_text(encoding="utf-8")).get(key) or key
    except (OSError, ValueError):
        return key


def answer(system: str, user: str) -> str:
    m = re.search(r"Answer in (\w+)", system)
    lang = "de" if m and m.group(1) == "German" else "en"
    if "OK and nothing else" in user:
        return "OK"
    if "Answer the user's question" in system:
        return _ask(lang, user)
    if "opening paragraph of a network's nightly report" in system:
        m = re.search(r"^Points that need a look: (\d+)$", user, re.M)
        n = int(m.group(1)) if m else 0
        from ..textfmt import fill
        return fill(_text(lang, "ai.demo.answer.report_calm" if not n else "ai.demo.answer.report"), {"n": n}, lang)
    if "help identify one device" in system:
        return _identify(lang, user)
    if "suggest which devices could move" in system:
        return _firewall(lang, user)
    m = re.search(r"^Subject: (device|finding|rule)$", user, re.M)
    subject = m.group(1) if m else "device"
    name = re.search(r"^(?:Name|Note|Rule): (.+)$", user, re.M)
    kind = re.search(r"^Kind: (.+)$", user, re.M)
    text = _text(lang, "ai.demo.answer." + subject)
    return text.replace("{name}", name.group(1).strip() if name else "?").replace("{kind}", kind.group(1).strip() if kind else "?")


def _ask(lang: str, user: str) -> str:
    q = (re.search(r"^Question: (.+)$", user, re.M) or [None, ""])[1].lower()
    rows = re.findall(r"^\[(D\d+)\] ([^|]+?) \| ([^|]+?) \| [^|]+ \| (\w+)", user, re.M)
    offline = [r for r in rows if r[3] == "down"]
    key = "ai.demo.answer.ask"
    pick = rows[0] if rows else None
    if offline and any(w in q for w in ("offline", "down", "weg", "aus", "erreichbar", "missing")):
        key, pick = "ai.demo.answer.ask_offline", offline[0]
    elif any(w in q for w in ("traffic", "daten", "internet", "upload", "country", "land", "länder")):
        key = "ai.demo.answer.ask_paths"
    text = _text(lang, key)
    if pick:
        text = text.replace("{name}", "%s [%s]" % (pick[1].strip(), pick[0]))
    return text.replace("{n}", str(len(rows)))


DEMO_KINDS = (("554", "camera"), ("1883", "broker"), ("8123", "hub"), ("631", "printer"), ("9100", "printer"),
              ("8009", "media"), ("445", "nas"), ("22/", "server"))


def _identify(lang: str, user: str) -> str:
    kind = "iot"
    for clue, k in DEMO_KINDS:
        if clue in user:
            kind = k
            break
    reason = _text(lang, "ai.demo.answer.identify").replace("{kind}", _text(lang, "kind." + kind))
    return json.dumps({"kind": kind, "label": _text(lang, "ai.demo.answer.identify_label.%s" % kind), "reason": reason})


def _firewall(lang: str, user: str) -> str:
    kinds = dict(re.findall(r"^\[(D\d+)\] [^|]+ \| ([^|]+?) \|", user, re.M))
    nets = dict(re.findall(r"^\[(N\d+)\] ([^|]+?) \|", user, re.M))
    moves = []
    for ref, targets in re.findall(r"^(D\d+): (.+)$", user, re.M):
        k = kinds.get(ref, "").lower()
        if any(w in k for w in ("speaker", "tv", "plug")):
            choices = [t.strip() for t in targets.split(",")]
            target = next((t for t in choices if any(w in nets.get(t, "").lower() for w in ("iot", "camera", "smart", "things"))), choices[0])
            moves.append({"device": ref, "network": target, "reason": _text(lang, "ai.demo.answer.firewall_reason")})
        if len(moves) >= 2:
            break
    return json.dumps({"moves": moves, "summary": _text(lang, "ai.demo.answer.firewall" if moves else "ai.demo.answer.firewall_none")})
