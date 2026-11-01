"""Board strings: English and German have the same keys, built-in defaults match English, every key in use exists."""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
I18N = os.path.join(ROOT, "lanloom", "i18n")
BOARD = os.path.join(ROOT, "lanloom", "web", "board")


def load(lang):
    with open(os.path.join(I18N, f"board.{lang}.json"), encoding="utf-8") as f:
        return json.load(f)


def board_js():
    with open(os.path.join(BOARD, "board.js"), encoding="utf-8") as f:
        return f.read()


def test_same_keys_and_prefix():
    en, de = load("en"), load("de")
    assert set(en) == set(de)
    assert all(k.startswith("board.") for k in en)
    assert all(isinstance(v, str) and v.strip() for v in list(en.values()) + list(de.values()))
    # placeholders must survive translation
    for k in en:
        assert set(re.findall(r"\{(\w+)\}", en[k])) == set(re.findall(r"\{(\w+)\}", de[k])), k


def test_builtin_defaults_match_english():
    src = board_js()
    block = src.split("/* strings:begin */", 1)[1].split("/* strings:end */", 1)[0]
    body = block[block.index("{"): block.rindex("}") + 1]
    assert json.loads(body) == load("en")


def test_every_used_key_exists():
    en = load("en")
    src = board_js()
    used = {k for k in re.findall(r"t\('(board\.[a-zA-Z.]+)'", src) if not k.endswith('.')}   # prefixes are checked below
    assert used, "no keys found"
    assert used <= set(en), used - set(en)
    with open(os.path.join(BOARD, "icons.js"), encoding="utf-8") as f:
        kinds = re.findall(r"^\s{4}(\w+): '", f.read(), re.M)
    for prefix, values in (("board.kind.", kinds),
                           ("board.grade.", ["realtime", "near", "periodic", "sluggish", "interrupted", "unknown"]),
                           ("board.status.", ["up", "down", "unknown"]),
                           ("board.trace.", ["uplink", "lan", "service", "flow", "integration"])):
        for v in values:
            assert prefix + v in en, prefix + v


def test_german_uses_real_umlauts():
    de = load("de")
    text = " ".join(de.values())
    assert "ä" in text and "ö" in text and "ü" in text
    for wrong in ("Traege", "Geraet", "groesse", "fuellen", "Groesse"):
        assert wrong not in text
