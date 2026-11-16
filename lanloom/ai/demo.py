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
    m = re.search(r"^Subject: (device|finding|rule)$", user, re.M)
    subject = m.group(1) if m else "device"
    name = re.search(r"^(?:Name|Note|Rule): (.+)$", user, re.M)
    kind = re.search(r"^Kind: (.+)$", user, re.M)
    text = _text(lang, "ai.demo.answer." + subject)
    return text.replace("{name}", name.group(1).strip() if name else "?").replace("{kind}", kind.group(1).strip() if kind else "?")
