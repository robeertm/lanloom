"""The translations are complete and in step, in every language under lanloom/i18n (a new language is just two new files).

- every language has the same keys as English, in the app file (<lang>.json) and in the board file (board.<lang>.json);
- every key carries the same placeholders in every language ({age}, plural variables, ...), no value is empty or still the key;
- plural forms: every plural text names the categories its language needs (CLDR), uses the same variable as English, and renders
  for 0, 1, 2, 5 and 21 without leftovers;
- every key the code uses exists, and every key in the files is used somewhere (so a removed screen takes its texts with it);
- a text equal to its English original is only allowed when it is listed with a reason."""
import json
import os
import re

import pytest

from lanloom import textfmt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
I18N = os.path.join(ROOT, "lanloom", "i18n")
WEB = os.path.join(ROOT, "lanloom", "web")

# Keys whose text is the same in German and English on purpose: key pattern (regular expression) -> reason. A stale entry fails the test.
SAME_AS_ENGLISH = {
    r"ai\.status\.public": "only a label and a model name around the word Cloud, which German uses as it is",
    r"integrations\.family\..+": "a product name with its standard description",
    r"livestate\.live": "the header says Live and the time; German uses the word Live as it is",
    r"notify\.(email\.placeholder|hb\.url_ph)": "an example address in an input field, not a word",
    r"zone\.net_(named|vlan|named_vlan)": "a name, a VLAN number and an address joined by dots; no word of its own",
    r"hv\.nic\.title": "a guest name and a network card name joined by a dot; no word of its own",
    r"hv\.edit\.fw_chip": "the word Firewall and a translated on/off; German uses the word Firewall as it is",
    r"api\.ref\.version_text": "the API version and the Lanloom version with the product name; no word of its own",
}

# Families of keys chosen at run time ('kind.' + x). A key counts as used when the code builds its prefix. Prefixes with two or more
# parts are accepted as they are; one-part prefixes are broad, so each is listed here with where it is built.
ONE_PART_FAMILIES = {
    "kind.": "device kinds (views.js, board icons)",
    "zone.": "board zones (views.js)",
    "reason.": "scan/source reason codes sent by the server",
    "role.": "account roles (settings)",
    "theme.": "light/dark/auto choice (settings)",
    "palette.": "colour themes (settings)",
    "certainty.": "topology certainty (detail panel)",
    "agent.": "kind of device of a session or audit entry (access.js)",
    "change.": "kind of a journal event in the Right now rail (shell.js)",
    "livestate.": "state of the live feed in the header and the live grade dialog (views.js)",
    "srcstate.": "calm state of a source on the start page (overview.js)",
}

SOURCES = ("lanloom", "updater")


def languages():
    return sorted(f[:-5] for f in os.listdir(I18N) if f.endswith(".json") and not f.startswith("board."))


def load(lang, board=False):
    with open(os.path.join(I18N, ("board.%s.json" if board else "%s.json") % lang), encoding="utf-8") as f:
        return json.load(f)


def both(lang):
    return {**load(lang), **load(lang, True)}


def test_languages_have_both_files():
    langs = languages()
    assert "en" in langs and "de" in langs
    for lang in langs:
        assert os.path.isfile(os.path.join(I18N, "board.%s.json" % lang)), "board.%s.json is missing" % lang
    boards = sorted(f[6:-5] for f in os.listdir(I18N) if f.startswith("board.") and f.endswith(".json"))
    assert boards == langs, "every language needs both files"


@pytest.mark.parametrize("board", [False, True], ids=["app", "board"])
def test_same_keys_in_every_language(board):
    en = set(load("en", board))
    assert en
    for lang in languages():
        have = set(load(lang, board))
        assert have == en, "%s: missing %s, extra %s" % (lang, sorted(en - have)[:8], sorted(have - en)[:8])


def test_app_and_board_files_do_not_overlap():
    for lang in languages():
        assert not set(load(lang)) & set(load(lang, True)), lang


def test_no_empty_or_unchanged_values():
    for lang in languages():
        for k, v in both(lang).items():
            assert isinstance(v, str) and v.strip(), "%s %s is empty" % (lang, k)
            assert v != k, "%s %s is still the key" % (lang, k)
            assert v == v.strip(), "%s %s has leading or trailing blanks" % (lang, k)


def test_placeholders_match_english():
    en = both("en")
    for lang in languages():
        have = both(lang)
        for k, v in en.items():
            assert textfmt.placeholders(v) == textfmt.placeholders(have[k]), "%s %s: %s vs %s" % (
                lang, k, sorted(textfmt.placeholders(v)), sorted(textfmt.placeholders(have[k])))


def test_plural_forms_complete_and_rendering():
    en = both("en")
    for lang in languages():
        need = set(textfmt.PLURAL_CATEGORIES.get(lang, ("one", "other")))
        have = both(lang)
        for k, v in have.items():
            forms = textfmt.constructs(v)
            for var, branch in forms:
                cats = {c for c in branch if not c.startswith("=")}
                assert need <= cats, "%s %s: plural of {%s} lacks %s" % (lang, k, var, sorted(need - cats))
                assert cats <= {"zero", "one", "two", "few", "many", "other"}, "%s %s: unknown category %s" % (lang, k, sorted(cats))
                assert all(b.strip() for b in branch.values()), "%s %s: empty plural form" % (lang, k)
            if forms:
                names = textfmt.placeholders(v)
                for n in (0, 1, 2, 5, 21):
                    out = textfmt.fill(v, {name: n for name in names}, lang)
                    assert "{" not in out and "}" not in out and "#" not in out, "%s %s with %d: %r" % (lang, k, n, out)


# Per language (O6): keys whose text equals the English original on purpose in that language only: key pattern -> reason.
# Short, so a translation that was simply left in English cannot hide here: every entry names why the words are the same.
_NAMES = "a name, a number, an address or a symbol line without a word of its own"
SAME_IN_LANGUAGE = {
    "fr": {
        r"routes\.method\.udp": "name of the measuring method (Paris traceroute) with the protocol in brackets",
        r"agents\.snippet\.run": "a shell command",
        r"auto\.dur\.min|notify\.shared\.hold_n|storage\.keep\.minutes": "minute/minutes is written the same way in French",
        r"backup\.files\.version": "Version is the same French word",
        r"devices\.filter\.net_vlan|devices\.tag\.wireless|ws\.chip\.rename|inc\.ch\.version": _NAMES,
        r"fw\.plain\.(apps|ports)|fw\.pol\.ports|tree\.port|hv\.meta\.cluster|hv\.trunk|nc\.net\.zone|wizard\.networks\.iface|zf\.chip\.net_zone|zf\.chip\.zone_rename|users\.sites\.n|pf\.clients|endmarks\.sources|inc\.cal\.maint": "the French word is written like the English one (port, zone, cluster, trunk, interface, site, client, source ...)",
        r"hv\.col\.path|users\.sessions|zf\.rule\.active|fail\.why\.radio|inc\.heal\.maintenance": "short phrase that reads the same in French",
        r"nc\.proto\.both|portmarks\.mesh\.subghz": "protocol / radio technology name",
    },
    "es": {
        r"agents\.snippet\.run": "a shell command",
        r"ai\.status\.local|svc\.state\.error": "Local / Error is the same Spanish word",
        r"api\.report\.ingest|devices\.filter\.net_vlan|devices\.tag\.wireless|ws\.chip\.rename|inc\.ch\.(version|firmware)|inc\.before\.firmware": _NAMES,
        r"detail\.snmp": "device class with a protocol name in brackets",
        r"routes\.method\.udp": "name of the measuring method (Paris traceroute) with the protocol in brackets",
        r"fw\.sec\.wpa2?_?(wpa3_)?personal|fw\.sec\.wpa3_personal": "standard protocol label; personal is the same Spanish word",
        r"nc\.proto\.both|portmarks\.mesh\.subghz": "protocol / radio technology name",
        r"users\.tfa\.passkeys": "the loan word passkey is used the same way in every passkey text",
    },
    "nl": {
        r"a11y\.key\.shift_tab": "names of two keys",
        r"a11y\.key\.pgdn": "the name of the key as printed on Dutch keyboards",
        r"agents\.snippet\.run": "a shell command",
        r"account\.title|api\.report\.ingest|devices\.filter\.net_vlan|devices\.tag\.wireless|ws\.chip\.rename|inc\.ch\.(version|firmware)|inc\.before\.firmware|zf\.chip\.(net_zone|zone_rename)": _NAMES,
        r"auto\.ex\.c\.(open|state|states)|notify\.msg\.(dev_offline|incident)|ov\.note\.dev_offline\.sub_unknown": "the Dutch sentence is built like the English one (is open, is offline, incident)",
        r"auto\.runs\.hour_tip|fw\.pol\.match\.app|pf\.clients|running\.lead|endmarks\.peers|users\.tfa\.passkeys": "the Dutch loan word is the same as the English one (run, app, client, host, container, peer, passkey)",
        r"detail\.snmp|hv\.meta\.cluster|hv\.scope\.node|hv\.trunk|nc\.net\.zone|pf\.duplex\.half|pf\.f\.poe|radio\.anchors\.thread|radio\.role\.border_router": "technical term that Dutch writes like the English one",
        r"nc\.proto\.both|portmarks\.mesh\.subghz": "protocol / radio technology name",
    },
    "it": {
        r"routes\.method\.udp": "name of the measuring method (Paris traceroute) with the protocol in brackets",
        r"agents\.snippet\.run": "a shell command",
        r"account\.title|api\.report\.ingest|devices\.filter\.net_vlan|devices\.tag\.wireless|ws\.chip\.rename": _NAMES,
        r"detail\.snmp": "device class with a protocol name in brackets",
        r"portmarks\.mesh\.subghz": "radio technology name",
    },
}


def test_text_equal_to_english_is_listed():
    en = both("en")
    same_by_lang = {}
    for lang in languages():
        if lang == "en":
            continue
        for k, v in both(lang).items():
            if v == en[k] and len(re.findall(r"[^\W\d_]{3,}", v)) >= 2:
                same_by_lang.setdefault(lang, set()).add(k)       # two or more real words and identical: untranslated, or a name that needs a reason
    same = set().union(*same_by_lang.values()) if same_by_lang else set()
    for lang, keys in same_by_lang.items():
        own = SAME_IN_LANGUAGE.get(lang, {})
        unlisted = {k for k in keys if not any(re.fullmatch(p, k) for p in SAME_AS_ENGLISH) and not any(re.fullmatch(p, k) for p in own)}
        assert not unlisted, "%s: text equal to the English original (translate it or list its key pattern in SAME_AS_ENGLISH / SAME_IN_LANGUAGE with a reason): %s" % (lang, sorted(unlisted)[:20])
    stale = [pat for pat in SAME_AS_ENGLISH if not any(re.fullmatch(pat, k) for k in same)]
    assert not stale, "stale entries in SAME_AS_ENGLISH: %s" % stale
    for lang, table in SAME_IN_LANGUAGE.items():
        stale = [pat for pat in table if not any(re.fullmatch(pat, k) for k in same_by_lang.get(lang, ()))]
        assert not stale, "stale entries in SAME_IN_LANGUAGE[%s]: %s" % (lang, stale)
        assert lang in languages(), lang


# ------------------------------------------------------------------ keys in use
def read_sources():
    """(path, text) of every file that can name a key: web js, board js, html, python."""
    out = []
    for sub in ("js", "board"):
        d = os.path.join(WEB, sub)
        out += [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".js")]
    out.append(os.path.join(WEB, "index.html"))
    for top in SOURCES:
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, top)):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "web", "i18n")]
            out += [os.path.join(dirpath, f) for f in sorted(filenames) if f.endswith(".py")]
    texts = []
    for p in out:
        with open(p, encoding="utf-8") as f:
            texts.append((p, f.read()))
    return texts


def strip_builtin_strings(path, text):
    a, b = text.find("/* strings:begin */"), text.find("/* strings:end */")
    return text if a < 0 or b < 0 else text[:a] + text[b:]


HOLE = r"[A-Za-z0-9_\-.]+"


def literals_and_prefixes(namespaces):
    """(literals, prefixes, patterns): key-shaped string literals, key prefixes built at run time ('kind.' + x), and regexes for keys
    built with a hole in the middle ('integ.' + kind + '.title', 'source.%s.timeout', `a.${x}.b`)."""
    lits, prefixes, patterns = {}, {}, []
    quoted_js = re.compile(r"""(?<![\w.])(['"`])([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_\-]+)*\.?)\1""")
    quoted_py = re.compile(r"""(?<![\w.`])(['"])([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_\-]+)*\.?)\1""")
    prefix_open = re.compile(r"""(['"`])([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_\-]+)*\.)(?:\1|\$\{|%s|\{)""")
    concat = re.compile(r"""(['"`])([A-Za-z][\w]*(?:\.[\w\-]+)*\.)\1\s*\+\s*[^'"`+;]{1,80}?\s*\+\s*(['"`])(\.[\w.\-]+)\3""")
    holed = re.compile(r"""(['"`])([A-Za-z][\w]*\.[^'"`\n]*?(?:%s|\$\{[^}]*\}|\{[^}]*\})[^'"`\n]*)\1""")
    for path, text in read_sources():
        text = strip_builtin_strings(path, text)
        quoted = quoted_js if path.endswith((".js", ".html")) else quoted_py
        for m in quoted.finditer(text):
            lits.setdefault(m.group(2), set()).add(os.path.basename(path))
        for m in prefix_open.finditer(text):
            if m.group(2).split(".")[0] in namespaces:
                prefixes.setdefault(m.group(2), set()).add(os.path.basename(path))
        for m in concat.finditer(text):
            if m.group(2).split(".")[0] in namespaces:
                patterns.append(re.compile("^" + re.escape(m.group(2)) + HOLE + re.escape(m.group(4)) + "$"))
        for m in holed.finditer(text):
            body = m.group(2)
            if body.split(".")[0] in namespaces:
                pieces = re.split(r"%s|\$\{[^}]*\}|\{[^}]*\}", body)
                patterns.append(re.compile("^" + HOLE.join(re.escape(x) for x in pieces) + "$"))
    return lits, prefixes, patterns


def all_keys():
    return set(both("en"))


def test_every_key_the_code_names_exists():
    keys = all_keys()
    namespaces = {k.split(".")[0] for k in keys}
    lits, _, _ = literals_and_prefixes(namespaces)
    # string literals shaped like a key of a known namespace but not a key: file names, step identifiers. Anything else is a missing
    # translation.
    NOT_KEYS = {
        "request.json": "file name of the updater hand-over (updater/engine.py)",
        "catalog.json": "file name of the integration catalog (integrations/catalog.py)",
        "setup.sh": "file name of an installer script",
        "setup.ps1": "file name of an installer script",
        "zone.create": "firewall step identifier (firewall/steps.py), not a text",
        "zone.update": "firewall step identifier (firewall/steps.py), not a text",
        "zone.delete": "firewall step identifier (firewall/steps.py), not a text",
        "zone.update.name": "capability key of a firewall step and field (integrations/capabilities.py), not a text",
        "backup.lan": "host name of a sample device (demo.py)",
        "backup.json": "file name of the updater's record of an update (updater/engine.py)",
        "storage.local": "host name of a sample device (demo.py)",
        "notify.family_phones": "action name in a sample automation of the demo hub (demo_hub.py)",
        "notify.notify": "action name in a sample automation of the demo hub (demo_hub.py)",
        "counters.json": "file name of the executor's signature counters (firewall/runner.py)",
        "device.mfr": "variable name of a Network UPS Tools server (sources/nut.py), not a text",
        "device.model": "variable name of a Network UPS Tools server (sources/nut.py), not a text",
    }
    # audit log action names ("auth.login", "users.invite" ...) are identifiers stored in the log; their labels are audit.action.<name>
    from lanloom import access
    NOT_KEYS.update({a: "audit action name (access.AUDIT_ACTIONS)" for a in access.AUDIT_ACTIONS})
    # webhook event ids ("device.new", "report.ready" ...) are part of the public API; their labels are webhooks.ev.<id>
    from lanloom import webhooks
    NOT_KEYS.update({e: "webhook event id (webhooks.EVENTS)" for e in webhooks.EVENTS})
    missing = sorted(l for l in lits if "." in l and not l.endswith(".") and l.split(".")[0] in namespaces and l not in keys and l not in NOT_KEYS)
    assert not missing, "the code names keys that no language file has: %s" % missing[:40]
    # t('literal') calls specifically must resolve, whatever the namespace
    called = set()
    for path, text in read_sources():
        if path.endswith(".js"):
            called |= set(re.findall(r"""\bt\(\s*'([^']+)'""", strip_builtin_strings(path, text)))
    called = {c for c in called if not c.endswith(".")}
    assert called <= keys, sorted(called - keys)[:40]


def test_no_unused_keys():
    keys = all_keys()
    namespaces = {k.split(".")[0] for k in keys}
    lits, prefixes, patterns = literals_and_prefixes(namespaces)
    for p in list(prefixes):
        if p.count(".") == 1 and p not in ONE_PART_FAMILIES:
            # a one-part prefix like "ai." only counts when listed; otherwise it would cover a whole namespace
            prefixes = {q: v for q, v in prefixes.items() if q != p}
    dynamic = {p for p in prefixes if p.count(".") >= 2 or p in ONE_PART_FAMILIES}
    unused = sorted(k for k in keys if k not in lits and not any(k.startswith(p) for p in dynamic) and not any(r.match(k) for r in patterns))
    assert not unused, "keys in the language files that no code uses (remove them or use them): %s" % unused


def test_one_part_families_are_real():
    keys = all_keys()
    namespaces = {k.split(".")[0] for k in keys}
    _, prefixes, _ = literals_and_prefixes(namespaces)
    for fam in ONE_PART_FAMILIES:
        assert fam in prefixes, "%s is not built anywhere in the code any more" % fam
        assert any(k.startswith(fam) for k in keys), fam


def test_server_selftest_checks_all_languages():
    """`python -m lanloom selftest` compares the key sets of every language (the container runs it before an update swaps in)."""
    from lanloom import main
    assert main.i18n_languages() == languages()


def test_every_place_that_lists_languages_agrees_with_the_files():
    """Adding a language file must not need a hunt through the code: the lists in the code are checked against the files."""
    langs = languages()
    with open(os.path.join(WEB, "js", "app.js"), encoding="utf-8") as f:
        app = f.read()
    listed = re.search(r"LL\.LANGUAGES = \[(.*?)\];", app, re.S).group(1)
    assert sorted(re.findall(r"\['(\w+)', '[^']+'\]", listed)) == langs, "LL.LANGUAGES in js/app.js"
    locales = re.search(r"const LOCALES = \{(.*?)\};", app, re.S).group(1)
    assert sorted(re.findall(r"(\w+): '[\w-]+'", locales)) == langs, "LOCALES in js/app.js"
    from lanloom import main
    from lanloom.ai import manager
    assert list(main.LANGS) == langs
    assert sorted(manager.LANG_NAMES) == langs, "LANG_NAMES in ai/manager.py (the language the AI answers in)"
    from lanloom.nightly import render
    assert all(render.WEEKDAYS.get(l) for l in langs), "weekday names for the delivered report (nightly/render.py)"


def test_last_seen_label_says_it_is_the_last_scan_that_found_the_device():
    # lanloom#18: "Last scan" read like the time of the latest scan and looked frozen for a device the scans miss while the
    # presence row below it was fresh; the stored time is the last scan that found the device
    labels = {}
    for lang in ("en", "de", "es", "fr", "it", "nl"):
        with open(os.path.join(I18N, "%s.json" % lang), encoding="utf-8") as f:
            labels[lang] = json.load(f)["device.last_seen"]
    assert labels["en"] == "Last found by a scan"
    assert labels["de"] != "Letzte Suche" and labels["nl"] != "Laatste scan"
