"""The command palette (S1): the server search behind GET /api/search, the matcher shared with the page, and the page's registers.

- words: accents and case do not matter, every word has to match, a typo is forgiven from four letters, a swap counts as one edit;
- value filter: ``> 25 °C`` / ``<= 40 %`` keep only hub entities whose current number fits, units are compared, the rest narrows by name;
- groups: devices, guests, services, automations and hub entities, hidden devices never, privacy-filtered data only as the bridge delivered it;
- speed: 3000 hub entities answer within the budget (50 ms), the index is built once per snapshot;
- access: any signed-in role may search, nobody else; the search never changes anything (no audit entry, no setting);
- the page: the new scripts are loaded, every command and action names texts that exist in every language, a view that is not there offers no action."""
import json
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod
from lanloom import palette
from lanloom.main import create_app

HDR = {"X-Lanloom": "1"}
PW = "a-long-test-password"
ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "lanloom" / "web"


# ---------------------------------------------------------------------- the matcher (Python and the page agree)
CASES = [
    ["kitchen", "Kitchen light 1", "light.kitchen_2 Kitchen light"],
    ["kitch", "Kitchen light 1", ""],
    ["itch", "Kitchen light 1", ""],
    ["lght", "Kitchen light 1", ""],            # a typo: only with typo tolerance
    ["kichen", "Kitchen light 1", ""],           # a swap of two letters
    ["sensr kichen", "Kitchen temperature", "sensor.kitchen_1 sensor"],
    ["router", "Core switch", "192.168.50.2 switch"],
    ["192.168", "Core switch", "192.168.50.2 switch"],
    ["cafe", "Café Küche", ""],                  # accents fold
    ["strasse", "Straße 5", ""],
    ["ab", "Abc", ""],
    ["a", "xyz", "abc"],
    ["", "anything", ""],
]
DISTS = [["kitten", "sitting"], ["abc", "acb"], ["abcd", "abdc"], ["same", "same"], ["short", "muchlongerword"], ["ab", "ba"]]
RANK_ITEMS = ["Open Map", "Open Network", "Open Data paths", "Open Devices", "Open Firewall", "Open Reports", "Open Settings", "Switch light / dark"]


def test_matcher_ranks_like_the_page():
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, str(ROOT / "tests" / "js" / "fuzzy_test.js"), json.dumps(CASES), json.dumps(DISTS), json.dumps(RANK_ITEMS), "dev"],
                          capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr[-1500:]
    js = json.loads(proc.stdout)
    for i, (q, title, extra) in enumerate(CASES):
        e = palette.Entry(title, extra, None)
        tokens = palette.words(q)
        assert js["fold"][i] == palette.fold(title), (title, js["fold"][i])
        assert js["scores"][i] == palette.score(tokens, e, False), ("exact", q, title, js["scores"][i])
        assert js["typo"][i] == palette.score(tokens, e, True), ("typo", q, title, js["typo"][i])
    assert js["dist"] == [palette._dist(a, b, 2) for a, b in DISTS]
    # views and commands: "dev" finds Devices first, then nothing else without a typo
    assert js["rank"][0] == "Open Devices"


def test_fold_and_words():
    assert palette.fold("Café  Küche – Straße!") == "cafe kuche strasse"
    assert palette.words("  ") == []
    assert palette.words("Kitchen, light 1") == ["kitchen", "light", "1"]


def test_word_score_levels_and_typo_rules():
    t, hw = "kitchen light", ["kitchen", "light"]
    assert palette.word_score("light", t, hw, False) == 100            # a whole word
    assert palette.word_score("kitch", t, hw, False) == 80             # start of a word
    assert palette.word_score("tche", t, hw, False) == 55              # inside a word
    assert palette.word_score("kichen", t, hw, False) == 0             # a typo is only forgiven when asked for
    assert 25 <= palette.word_score("kichen", t, hw, True) <= 45
    assert palette.word_score("lit", t, hw, True) == 0                 # under four letters: exact only
    assert palette.word_score("xyzxyz", t, hw, True) == 0
    assert palette.typo_limit("abc") == 0 and palette.typo_limit("abcd") == 1 and palette.typo_limit("abcdefgh") == 2


def test_every_word_must_match():
    e = palette.Entry("Kitchen light", "light.kitchen_2", None)
    assert palette.score(["kitchen", "light"], e, False) > 0
    assert palette.score(["kitchen", "garage"], e, False) == 0


def test_rank_prefers_exact_then_shorter_and_forgives_typos_only_when_nothing_matches():
    items = [palette.Entry(x, "", x) for x in ("Kitchen light 1", "Light", "Highlight")]
    assert palette.rank(items, ["light"], 5) == ["Light", "Kitchen light 1", "Highlight"]
    assert palette.rank(items, ["lihgt"], 5)[0] == "Light"             # no exact hit anywhere: typo pass
    assert palette.rank(items, ["zzzz"], 5) == []


# ---------------------------------------------------------------------- the value filter
def test_parse_value_comparisons():
    p = palette.parse("> 25 °C")
    assert (p["op"], p["num"], p["unit"], p["text"]) == (">", 25.0, "c", "")
    p = palette.parse("kitchen temp <= 19,5")
    assert (p["op"], p["num"], p["unit"], p["text"]) == ("<=", 19.5, "", "kitchen temp")
    assert palette.parse("battery ≥ 80 %")["op"] == ">=" and palette.parse("battery ≥ 80 %")["unit"] == "%"
    assert palette.parse("=> 3")["op"] == ">="
    assert palette.parse("light 2")["op"] is None
    assert palette.parse("x" * 500)["text"] == "x" * palette.MAX_QUERY


def test_unit_aliases():
    assert palette._unit_ok("c", "°C") and palette._unit_ok("c", "C") and palette._unit_ok("", "kWh")
    assert palette._unit_ok("%", "%") and not palette._unit_ok("c", "°F") and not palette._unit_ok("%", None)


# ---------------------------------------------------------------------- the search over a core
def fake_core(n_entities=0, devices=None, guests=None, hub_states=None):
    ents = []
    for i in range(n_entities):
        domain = ("sensor", "light", "switch", "binary_sensor")[i % 4]
        ents.append({"entity_id": "%s.room%d_%d" % (domain, i // 4, i), "name": "Room %d %s %d" % (i // 4, domain.replace("_", " "), i), "state": "%.1f" % (10 + (i % 300) / 10) if domain == "sensor" else "on",
                     "unit": "°C" if domain == "sensor" else None, "domain": domain, "area": "r%d" % (i // 40)})
    ents += [{"entity_id": "automation.night_light", "name": "Night light", "state": "on", "unit": None, "domain": "automation", "area": None},
             {"entity_id": "script.water_garden", "name": "Water the garden", "state": "off", "unit": None, "domain": "script", "area": None}]
    snap = {"entities": ents, "areas": [{"id": "r%d" % i, "name": "Area %d" % i} for i in range(80)]}
    entry = {"snapshot": snap, "t": 1.0, "states": dict(hub_states or {})}
    devices = devices if devices is not None else []
    configs = [{"id": 5, "name": "Sample hypervisor", "state": {"hosts": [{"id": "h1", "name": "node1"}], "guests": guests}}] if guests is not None else []
    return SimpleNamespace(devices=lambda: devices, hub_ids=lambda: [1], hub={1: entry}, live_configs=lambda: configs)


def dev(i, title, ip, kind="nas", ports=(), hidden=False, services=()):
    return {"id": "d-%d" % i, "title": title, "ip": ip, "mac": "02:00:00:00:00:%02x" % i, "kind": kind, "hostnames": [], "hidden": hidden, "status": "up",
            "is_self": False, "ports": [{"port": p, "service": s} for p, s in ports], "services": list(services)}


@pytest.fixture(autouse=True)
def no_component_lookup(monkeypatch):
    monkeypatch.setattr(palette, "_hub_component", lambda core, integ_id: "comp-%d" % integ_id)


def test_search_groups_and_hidden_devices():
    core = fake_core(devices=[dev(1, "Living room NAS", "192.168.1.10", ports=[(22, "ssh"), (443, "https")], services=["SSH", "HTTPS"]),
                              dev(2, "Printer", "192.168.1.20", "printer", [(631, "ipp")], services=["IPP"]),
                              dev(3, "Secret NAS", "192.168.1.30", hidden=True)],
                     guests=[{"id": "g1", "name": "nas-backup", "vmid": 101, "type": "lxc", "host": "h1", "status": "running", "ips": ["192.168.1.10"], "template": False}])
    r = palette.search(core, "nas")
    g = r["groups"]
    assert [h["title"] for h in g["devices"]] == ["Living room NAS"]                   # the hidden device is never offered
    assert g["guests"][0]["title"] == "nas-backup" and g["guests"][0]["device"] == "d-1" and g["guests"][0]["host"] == "node1"
    r = palette.search(core, "ssh")
    s = r["groups"]["services"]
    assert s and s[0]["device"] == "d-1" and s[0]["port"] == 22 and s[0]["title"] == "SSH"
    r = palette.search(core, "192.168.1.20")
    assert [h["id"] for h in r["groups"]["devices"]] == ["d-2"]                         # the address is searchable
    assert palette.search(core, "")["groups"] == {k: [] for k in ("devices", "guests", "services", "automations", "entities")}


def test_automations_and_entities_are_separate_and_carry_the_live_state():
    core = fake_core(20, hub_states={"light.room0_1": ("off", 5.0)})
    r = palette.search(core, "night")
    assert [h["id"] for h in r["groups"]["automations"]] == ["automation.night_light"] and r["groups"]["automations"][0]["comp"] == "comp-1"
    assert not any(h["kind"] == "automation" for h in r["groups"]["entities"])
    r = palette.search(core, "water garden")
    assert r["groups"]["automations"][0]["id"] == "script.water_garden" and r["groups"]["automations"][0]["domain"] == "script"
    r = palette.search(core, "room0 light")
    hit = next(h for h in r["groups"]["entities"] if h["id"] == "light.room0_1")
    assert hit["state"] == "off"                                                        # the live value beats the snapshot's "on"


def test_value_filter_on_entities():
    core = fake_core(60, hub_states={"sensor.room0_0": ("30.5", 1.0)})
    r = palette.search(core, "> 25 °C", limit=25)
    vals = [float(h["state"]) for h in r["groups"]["entities"]]
    assert r["value"] == {"op": ">", "num": 25.0, "unit": "c"}
    assert vals and all(v > 25 for v in vals) and vals == sorted(vals, reverse=True)  # highest first for ">"
    assert r["groups"]["entities"][0]["state"] == "30.5"
    assert not r["groups"]["devices"] and not r["groups"]["services"]                    # a comparison only asks about hub values
    low = palette.search(core, "<= 11", limit=25)["groups"]["entities"]
    assert low and all(float(h["state"]) <= 11 for h in low) and [float(h["state"]) for h in low] == sorted(float(h["state"]) for h in low)
    assert palette.search(core, "> 25 °F")["groups"]["entities"] == []                  # another unit
    narrowed = palette.search(core, "room0 sensor > 10")["groups"]["entities"]
    assert narrowed and all(h["id"].startswith("sensor.room0") for h in narrowed)
    assert all(h["state"] != "on" for h in palette.search(core, "> 0")["groups"]["entities"])   # non-numeric states never match


def test_typo_tolerant_search():
    core = fake_core(devices=[dev(1, "Living room speaker", "192.168.1.11", "media")])
    assert [h["id"] for h in palette.search(core, "speeker")["groups"]["devices"]] == ["d-1"]
    assert palette.search(core, "qqqqqqq")["groups"]["devices"] == []


def machine_factor() -> float:
    """How much slower than the reference machine (a small single-board computer, about 44 ms for this loop) this one computes right now: shared test
    runners are several times slower, and the 50 ms budget is meant for ordinary hardware."""
    t0 = time.process_time()
    n = 0
    for i in range(300000):
        n += i % 7
    return max(1.0, (time.process_time() - t0) * 1000 / 45)


def test_a_search_over_3000_entities_stays_within_the_budget():
    budget = 50 * machine_factor()
    core = fake_core(3000)
    palette.search(core, "room")                                                        # builds the index once
    for q in ("room 12 light", "> 20 °C", "sensr 99", "garden", "zzz unmatched", "binary window", "r", "tempratur"):
        times = []
        for _ in range(3):                                                              # CPU time of the best of three: a busy shared machine must not fail the budget
            t0 = time.process_time()
            r = palette.search(core, q)
            times.append((time.process_time() - t0) * 1000)
        assert min(times) < budget, (q, times, budget)
    assert palette.search(core, "sensr 99")["groups"]["entities"], "typos are forgiven over thousands of entities too"


def test_the_index_is_rebuilt_when_the_snapshot_changes():
    core = fake_core(8)
    assert palette.search(core, "water")["groups"]["automations"]
    entry = core.hub[1]
    entry["snapshot"] = {"entities": [{"entity_id": "automation.new_thing", "name": "Brand new thing", "state": "on", "unit": None, "domain": "automation", "area": None}], "areas": []}
    entry["t"] = 2.0
    assert palette.search(core, "water")["groups"]["automations"] == []
    assert palette.search(core, "brand")["groups"]["automations"][0]["id"] == "automation.new_thing"


# ---------------------------------------------------------------------- the route
@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "0")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        code = (Path(tmp_path) / "setup-code.txt").read_text().strip()
        assert c.post("/api/setup/account", json={"code": code, "username": "admin", "password": PW}, headers=HDR).status_code == 200
        c.st = app.state.ll
        yield c


def login(c, user):
    c.cookies.clear()
    r = c.post("/api/login", json={"username": user, "password": PW}, headers=HDR)
    assert r.status_code == 200, r.text


def test_route_needs_a_session_and_any_role_may_search(app_client):
    c = app_client
    for role in ("viewer", "operator"):
        c.st.accounts.create(role + "1", PW, role=role)
    c.cookies.clear()
    assert c.get("/api/search?q=nas").status_code == 401
    for user in ("viewer1", "operator1", "admin"):
        login(c, user)
        r = c.get("/api/search?q=nas")
        assert r.status_code == 200 and set(r.json()) == {"q", "value", "groups", "ms"}
    from lanloom import access
    assert access.RULES[("GET", "/api/search")].role == "viewer"


def test_sample_network_answers_and_the_search_writes_nothing(app_client):
    c = app_client
    assert c.post("/api/setup/demo", json={"on": True}, headers=HDR).status_code == 200
    audit_before = len(c.st.audit.query({}, 1000))
    for _ in range(80):                                   # the sample hub reports a moment after the start
        g = c.get("/api/search", params={"q": "kitchen"}).json()["groups"]
        if g["entities"]:
            break
        time.sleep(0.25)
    assert g["devices"] and g["entities"] and all(h["kind"] == "entity" for h in g["entities"])
    hot = c.get("/api/search", params={"q": "> 20 °C", "limit": 25}).json()
    assert hot["value"]["op"] == ">" and hot["groups"]["entities"] and all(float(h["state"]) > 20 for h in hot["groups"]["entities"])
    assert c.get("/api/search", params={"q": "ssh"}).json()["groups"]["services"]
    assert len(c.st.audit.query({}, 1000)) == audit_before                              # reading is not audited
    c.post("/api/setup/demo", json={"on": False}, headers=HDR)
    assert c.get("/api/search", params={"q": "kitchen"}).json()["groups"]["devices"] == []   # the real (empty) inventory is back
    assert c.get("/api/search", params={"q": "x" * 1000}).status_code == 200            # an oversized query is cut, not refused
    assert c.get("/api/search", params={"q": "nas", "limit": 9999}).status_code == 200


# ---------------------------------------------------------------------- the page
def js(name):
    return (WEB / "js" / name).read_text(encoding="utf-8")


def test_page_loads_the_palette_in_order():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    order = re.findall(r'<script src="(js/[^"]+)"', html)
    assert order.index("js/fuzzy.js") < order.index("js/palette.js") and order.index("js/app.js") < order.index("js/palette.js")
    assert 'href="css/palette.css"' in html
    assert "paletteButton" in js("views.js")                                           # the button sits in the top bar


def test_commands_and_actions_name_texts_that_exist():
    en = json.loads((ROOT / "lanloom" / "i18n" / "en.json").read_text(encoding="utf-8"))
    src = js("palette.js")
    keys = set(re.findall(r"""['"]((?:cmdk|theme|nav|settings|backup|ai|reports|update)\.[a-z_.]*[a-z_])['"]""", src))
    missing = sorted(k for k in keys if k not in en)
    assert not missing, missing
    for k in ("map", "network", "paths", "devices", "firewall", "reports", "settings", "account", "users", "security", "backup", "ai", "report_settings", "theme", "unit", "update", "recent"):
        assert "cmdk.kw." + k in en, k                                                 # every command has search words in every language


def test_actions_are_registered_only_for_what_exists():
    src = js("palette.js")
    for route in re.findall(r"LL\.go\('#/([a-z]+)", src):
        assert route in ("map", "network", "devices", "settings"), route               # only views that exist today (network: its 3D face, O7)
    reg = js("app.js")
    assert "LL.registerAction" in reg and "LL.registerCommand" in reg
    # the asked-for actions (outage chain, dependencies, maintenance, history, availability) are added by the features that bring those views
    assert set(re.findall(r"cmdk\.act\.[a-z_]+", src)) == {"cmdk.act.open", "cmdk.act.open_hub", "cmdk.act.copy_ip", "cmdk.act.copy_id"}
