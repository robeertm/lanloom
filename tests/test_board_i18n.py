"""Board strings: English and German have the same keys, built-in defaults match English, every key in use exists.
The end and port marks (endmarks.js, portmarks.js) keep their strings in the same files under their own prefixes."""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
I18N = os.path.join(ROOT, "lanloom", "i18n")
BOARD = os.path.join(ROOT, "lanloom", "web", "board")


def load(lang):
    with open(os.path.join(I18N, f"board.{lang}.json"), encoding="utf-8") as f:
        return json.load(f)


def board_js(name="board.js"):
    with open(os.path.join(BOARD, name), encoding="utf-8") as f:
        return f.read()


MODULES = {"board.": "board.js", "endmarks.": "endmarks.js", "portmarks.": "portmarks.js"}


def builtin(name):
    block = board_js(name).split("/* strings:begin */", 1)[1].split("/* strings:end */", 1)[0]
    return json.loads(block[block.index("{"): block.rindex("}") + 1])


def test_same_keys_and_prefix():
    en, de = load("en"), load("de")
    assert set(en) == set(de)
    assert all(k.startswith(tuple(MODULES)) for k in en)
    assert all(isinstance(v, str) and v.strip() for v in list(en.values()) + list(de.values()))
    # placeholders must survive translation
    for k in en:
        assert set(re.findall(r"\{(\w+)\}", en[k])) == set(re.findall(r"\{(\w+)\}", de[k])), k


def test_builtin_defaults_match_english():
    en = load("en")
    for prefix, name in MODULES.items():
        assert builtin(name) == {k: v for k, v in en.items() if k.startswith(prefix)}, name


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
    # end and port marks: every literal key in the source exists, band keys complete
    for name in ("endmarks.js", "portmarks.js"):
        keys = {k for k in re.findall(r"'((?:endmarks|portmarks)\.[a-zA-Z0-9.]+)'", board_js(name)) if not k.endswith(".")}   # prefixes below
        assert keys and keys <= set(en), keys - set(en)
    for band in ("2.4ghz", "5ghz", "6ghz", "11n", "11ac", "11ax", "11be"):
        assert "portmarks.band." + band in en


def test_german_uses_real_umlauts():
    de = load("de")
    text = " ".join(de.values())
    assert "ä" in text and "ö" in text and "ü" in text
    for wrong in ("Traege", "Geraet", "groesse", "fuellen", "Groesse"):
        assert wrong not in text
