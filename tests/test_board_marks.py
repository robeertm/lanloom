"""End and port marks: pure logic under Node, the host interface is documented, the theme uses only own variables with Lanloom
fallbacks, and the core books peers beyond the LAN and the target ports of flows for them (never guessed)."""
import os
import re
import time

from _board_node import run_node

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOARD = os.path.join(ROOT, "lanloom", "web", "board")


def read(*p):
    with open(os.path.join(ROOT, "lanloom", "web", *p), encoding="utf-8") as f:
        return f.read()


def test_bundle_name_changes_only_with_a_lasting_lead():
    n = run_node("marks_test.js")["name"]
    assert n["firstSimilar"] == "alpha" and n["firstSimilarSwapped"] == "alpha"    # similar weight → fixed order by name
    assert n["firstClear"] == "beta"                                               # clearly heavier → the heavier one
    assert n["alternating"] == ["alpha"]                                           # flips each second, never 1.5× for 8 s
    assert n["peak"] == ["alpha"] * 3                                              # 5 s peak: stays
    assert n["sustained"] == ["alpha", "alpha", "beta", "beta"]                    # ≥ 8 s with 1.5× lead: changes
    assert n["back"] == ["beta", "beta"]
    assert n["order"] == ["beta", "x", "alpha"] and n["gone"] == "x"


def test_placement_takes_the_first_free_spot_and_shortens():
    p = run_node("marks_test.js")["place"]
    assert p["height"] == 18
    assert p["a"] == [100, 140, False]       # first candidate blocked → second
    assert p["b"] == [100, 180]
    assert p["c"] == [True, True]            # full text fits inside the picture
    assert p["d"] == ["→ 1", True]           # at the edge only the short form fits
    assert p["e"] is None                    # everything blocked → no label
    assert all(s.endswith("…") for s in p["steps"]) and len(set(p["steps"])) == 3


def test_labels_are_shortened_at_a_word_boundary():
    out = run_node("marks_test.js")
    c = out["place"]["cut"]
    assert c["hyphen"].startswith("↔ 9 peers: living") and c["hyphen"].endswith("…")
    shown = c["hyphen"][len("↔ 9 peers: "):-1]
    assert c["peer"].startswith(shown) and c["peer"][len(shown)] == "-"                     # the next character is a separator
    assert c["spaced"].startswith("→ garden") and c["spaced"].endswith("…") and c["spaced"][-2] not in " ·-,"
    assert c["oneWord"] == "↔ 9 peers …"                                                    # no word fits: only the lead-in stays
    assert c["oneWordSingle"] == "→ …"
    assert c["fits"] == "→ nas"
    assert c["stepsOneWord"] == [] and c["stepsOneWordArrow"] == []                         # a bare "…" or "→ …" is no step
    s = out["shorten"]
    assert s["long"].endswith("…") and "storage · port 12 ⇄ eth0, seen in the last minute".startswith(s["long"][:-1])
    assert s["long"][:-1][-1] not in " ·-," and s["fits"] == "port 12" and s["oneWord"] == "…"


def test_equal_edge_targets_become_one_label():
    b = run_node("marks_test.js")["bundle"]
    same = b["same"]
    assert same["done"] == ["a", "b", "c"] and len(same["specs"]) == 1
    s = same["specs"][0]
    assert s["bundle"] == 3 and s["text"].endswith("3× cloud") and s["sources"] == ["a", "b", "c"] and s["bus"] >= 2
    rel = b["related"]["specs"]
    assert len(rel) == 1 and rel[0]["entries"] == ["cloud", "dns"] and rel[0]["text"].endswith("…")   # main target among the other's targets
    assert b["far"]["specs"] == [] and b["different"]["specs"] == []


def test_detail_levels():
    lod = run_node("marks_test.js")["lod"]
    assert lod["endFont"] == [11, 11, 12, 13, 13]
    assert lod["logical"] == [False, True, True]
    assert lod["network"] == [False, True, True]
    assert lod["touch"] is False                 # touch: only on tap
    assert lod["portFont"] == [10, 10, 11, 11.5, 11.5]


def test_texts_from_neutral_facts_and_nothing_guessed():
    out = run_node("marks_test.js")
    f = out["facts"]
    assert f["service"]["short"] == ":1883 MQTT" and "seen" in f["service"]["long"] and "listed :1883 :8883" in f["service"]["title"]
    assert f["serviceTwo"]["short"] == ":445 SMB · :22 SSH"
    assert f["uplink"]["short"] == "3 ⇄ SFP+ 9" and f["uplink"]["kind"] == "sfp"
    assert f["wired"]["short"] == "4" and f["radio"]["short"] == "5 GHz" and f["radioStd"]["short"] == "802.11ax"
    assert f["serviceNone"] is None and f["uplinkNone"] is None and f["wiredUnknown"] is None and f["chipNone"] == ""
    assert "SFP+ 10 ⇄ eth0" in f["chip"]
    assert [e and e["s"] for e in out["exit"]] == ["b", "r", None]
    assert out["arrow"] == ["→", "←", "↔", "→"]


def test_host_interface_documented():
    for name in ("endmarks.js", "portmarks.js"):
        src = read("board", name)
        assert "API = 1" in src and "const API = 1;" in src, name
        for fn in ("host.el", "host.ready()", "host.traces()", "host.view()", "host.keepClear(rect)", "host.t(key, params)"):
            assert fn in src, (name, fn)


def test_marks_never_cover_kept_clear_titles():
    em, pm, board = read("board", "endmarks.js"), read("board", "portmarks.js"), read("board", "board.js")
    assert "placeFree(variants, cand, [clear, ...lists], extra)" in em       # every placement, also the last fallbacks
    assert "free(rp, [clear, hard, tiles, taken])" in em                     # the emergency search on a leash as well
    assert "clear = host.keepClear ? host.keepClear(er) : []" in pm
    assert "keepClear: er =>" in board


def test_theme_variables_fall_back_to_lanloom_tokens():
    tokens = set(re.findall(r"(--ll-[\w-]+)\s*:", read("css", "tokens.css")))
    for name, own in (("endmarks.css", "endmark"), ("portmarks.css", "portmark")):
        css = read("board", name)
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", css), name                     # colours only from variables
        for var, fallback in re.findall(r"var\((--ll-%s-[\w-]+),\s*(?:var\((--[\w-]+)\)|[\d.]+)" % own, css):
            assert not fallback or fallback in tokens, (name, var, fallback)
        themed = re.findall(r"var\((--ll-%s-[\w-]+)(,?)" % own, css)
        assert themed and all(comma for _, comma in themed), name                            # every theme variable has a fallback
        other = {v for v in re.findall(r"var\((--[\w-]+)", css) if not v.startswith(("--ll-%s-" % own, "--ll-em-", "--ll-pm-"))}
        assert other <= tokens, (name, other - tokens)


def test_core_books_outside_peers_and_seen_ports(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    from lanloom import config as config_mod
    from lanloom.core import Core, _is_outside, flow_trace
    from lanloom.store import Store
    cfg = config_mod.load()
    core = Core(cfg, Store(os.path.join(str(tmp_path), "t.db")), None)
    core.self_ip, core.gateway_ip = "192.168.50.11", "192.168.50.1"
    core.ip_to_device = {"192.168.50.11": "self-dev", "192.168.50.1": "gw", "192.168.50.10": "nas"}
    now = time.time()
    core._book_flows([
        {"peer_ip": "192.168.50.10", "peer_port": 445, "tx": 9000, "rx": 100},
        {"peer_ip": "192.168.50.10", "peer_port": 445, "tx": 9000, "rx": 100},
        {"peer_ip": "192.168.50.10", "peer_port": 22, "tx": 10, "rx": 10},
        {"peer_ip": "203.0.113.20", "peer_port": 443, "tx": 1000, "rx": 5000},      # identifier: documentation address
        {"peer_ip": "198.51.100.7", "peer_port": 443, "tx": 10, "rx": 20},          # identifier: documentation address
        {"peer_ip": "192.168.50.77", "peer_port": 80, "tx": 10, "rx": 10},          # unknown LAN host: no outside peer
        {"peer_ip": "fe80::1", "peer_port": 80, "tx": 10, "rx": 10},
    ], now)
    peers = core.outside_list(now)
    assert [p["id"] for p in peers] == ["203.0.113.20", "198.51.100.7"]
    assert peers[0]["out"] == 1000 and peers[0]["in"] == 5000 and peers[0]["weight"] == 6000
    assert core.seen_ports(now) == {"nas": [445, 22]}
    frame = core.tick(now + 1)
    assert frame["peers"]["internet"][0]["id"] == "203.0.113.20"
    assert frame["ports"] == {flow_trace("self-dev", "nas"): [445, 22]}
    # decay and expiry: after the keep time nothing is left
    assert core.outside_list(now + 601) == [] and core.seen_ports(now + 601) == {}
    assert not core.tick(now + 602).get("peers")
    assert _is_outside("8.8.8.8") and not _is_outside("10.1.2.3") and not _is_outside("fd00::1") and not _is_outside("127.0.0.1")
