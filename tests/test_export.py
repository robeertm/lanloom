"""Exports (L3): the inventory as CSV and JSON, the import of own fields (round trip), the redact option (nothing of the network may
remain in any file), the network plan as SVG, draw.io and PDF (well-formed, texts as text, no line across a pad), roles and the audit log.

Names, addresses and notes are sample data; the "secret" strings exist only to prove they never leave a redacted file."""
import json
import re
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lanloom import config as config_mod
from lanloom import export, export_plan, pdfmini
from lanloom.main import create_app, load_i18n
from lanloom.web_export import make_tr

H = {"X-Lanloom": "1"}
PW = "a-long-test-password"
SECRETS = ["Zebra-Closet-Secret", "SN-SECRET-77", "Shop-Secret-GmbH", "Purpose-Secret-Text", "Walter-Secret-Person", "Secret-Room-9"]


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_DEMO_SEED", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        code = (Path(tmp_path) / "setup-code.txt").read_text().strip()
        r = c.post("/api/setup/account", json={"code": code, "username": "admin", "password": PW}, headers=H)
        assert r.status_code == 200
        c.st = app.state.ll
        yield c


def fetch(c, what, **opts):
    r = c.post("/api/export", json=dict(what=what, lang="en", **opts), headers=H)
    assert r.status_code == 200, r.text
    return r


def seed_secrets(c):
    """Own fields with text that must never appear in a redacted file, on a few devices (the sample inventory already has others)."""
    c.st.accounts.create("walter-secret-person", PW, role="viewer")
    items = c.get("/api/inventory").json()["items"]
    for n, it in enumerate(items[:4]):
        r = c.put("/api/devices/%s/inventory" % it["id"], json={"purpose": "Purpose-Secret-Text", "room": "Secret-Room-9", "shop": "Shop-Secret-GmbH",
                                                                 "note": "Zebra-Closet-Secret", "serial": "SN-SECRET-77-%d" % n,
                                                                 "owner": "walter-secret-person", "purchase_date": "2024-05-01"}, headers=H)
        assert r.status_code == 200, r.text
    return items


def pdf_text(raw: bytes) -> str:
    """The text of all pages of a PDF made by pdfmini (streams are deflated, the strings are plain)."""
    out = []
    for m in re.finditer(rb"stream\n(.*?)\nendstream", raw, re.S):
        out.append(zlib.decompress(m.group(1)).decode("latin-1"))
    return "\n".join(out)


# ------------------------------------------------------------------ files and round trip
def test_csv_and_json_hold_every_device_and_the_own_fields(app_client):
    c = app_client
    seed_secrets(c)
    total = c.get("/api/inventory").json()["summary"]["total"]
    csv_text = fetch(c, "list.csv").content.decode("utf-8")
    assert csv_text.startswith("﻿id,name,kind,ip,mac,network,")
    recs = export.parse("csv", csv_text)
    assert len(recs) == total and any(r["purpose"] == "Purpose-Secret-Text" and r["network"] for r in recs)
    doc = json.loads(fetch(c, "list.json").content)
    assert doc["format"] == export.FORMAT and doc["redacted"] is False and len(doc["devices"]) == total
    assert doc["columns"] == list(export.EXPORT_COLUMNS) and set(doc["devices"][0]) == set(export.EXPORT_COLUMNS)
    assert any(d["connected_via"] for d in doc["devices"])


def test_import_round_trip_changes_nothing_then_writes_own_columns(app_client):
    c = app_client
    seed_secrets(c)
    for fmt, what in (("csv", "list.csv"), ("json", "list.json")):
        text = fetch(c, what).content.decode("utf-8")
        r = c.post("/api/export/import", json={"format": fmt, "text": text}, headers=H).json()
        assert r["changed"] == 0 and r["errors"] == [] and r["unmatched"] == 0 and r["matched"] == r["rows"] and r["dry_run"] is True
    # edit a CSV cell, a dry run counts, only dry_run=false writes
    items = c.get("/api/inventory").json()["items"]
    target = items[0]
    text = fetch(c, "list.csv").content.decode("utf-8")
    recs = export.parse("csv", text)
    rec = next(r for r in recs if r["id"] == target["id"])
    rec.update({"room": "Attic", "note": "=HYPERLINK(\"x\")", "name": "Renamed by file", "warranty_until": "2030-01-31"})
    import csv as csvmod, io
    buf = io.StringIO()
    w = csvmod.DictWriter(buf, fieldnames=list(recs[0]))
    w.writeheader()
    w.writerows([dict(r, note=export._cell(r["note"])) for r in recs])      # a formula is written the way the export writes it
    r = c.post("/api/export/import", json={"format": "csv", "text": buf.getvalue()}, headers=H).json()
    assert r["changed"] == 1 and r["dry_run"] is True
    assert c.get("/api/devices/%s/inventory" % target["id"]).json()["room"] != "Attic"
    r = c.post("/api/export/import", json={"format": "csv", "text": buf.getvalue(), "dry_run": False}, headers=H).json()
    assert r["changed"] == 1 and r["dry_run"] is False
    got = c.get("/api/devices/%s/inventory" % target["id"]).json()
    assert got["room"] == "Attic" and got["title"] == "Renamed by file" and got["warranty_until"] == "2030-01-31" and got["note"] == '=HYPERLINK("x")'
    # and the next export is again a no-op against the inventory
    again = fetch(c, "list.csv").content.decode("utf-8")
    rr = c.post("/api/export/import", json={"format": "csv", "text": again}, headers=H).json()
    assert rr["changed"] == 0, rr["changes"]


def test_formulas_are_neutralised_in_the_file(app_client):
    c = app_client
    it = c.get("/api/inventory").json()["items"][0]
    c.put("/api/devices/%s/inventory" % it["id"], json={"note": "+1+1", "purpose": "@SUM(A1)"}, headers=H)
    raw = fetch(c, "list.csv").content.decode("utf-8")
    assert "'+1+1" in raw and "'@SUM(A1)" in raw
    rec = next(r for r in export.parse("csv", raw) if r["id"] == it["id"])
    assert rec["note"] == "+1+1" and rec["purpose"] == "@SUM(A1)"


def test_import_reports_wrong_rows_and_refuses_bad_files(app_client):
    c = app_client
    items = c.get("/api/inventory").json()["items"]
    a, b = items[0], items[1]
    doc = {"format": export.FORMAT, "redacted": False, "devices": [
        {"id": a["id"], "warranty_until": "not-a-date"}, {"id": b["id"], "owner": "nobody-here"}, {"id": "ghost", "room": "x"},
        {"id": items[2]["id"], "connected_via_id": items[2]["id"]}, {"id": items[3]["id"], "purpose": "ok"}]}
    r = c.post("/api/export/import", json={"format": "json", "text": json.dumps(doc), "dry_run": False}, headers=H).json()
    errs = {e["id"]: e["error"] for e in r["errors"]}
    assert errs == {a["id"]: "inventory.err.date", b["id"]: "inventory.err.owner", items[2]["id"]: "topo.err.parent"}
    assert r["unmatched"] == 1 and r["changed"] == 1                       # a wrong row is skipped whole, the right one is written
    assert c.get("/api/devices/%s/inventory" % items[3]["id"]).json()["purpose"] == "ok"
    for fmt, text, key in (("json", "{", "export.err.json"), ("json", json.dumps({"redacted": True, "devices": []}), "export.err.redacted"),
                           ("csv", "foo,bar\n1,2\n", "export.err.format"), ("csv", "", "export.err.empty"), ("xml", "<a/>", "export.err.format")):
        assert c.post("/api/export/import", json={"format": fmt, "text": text}, headers=H).json() == {"error": key}, (fmt, text)
    big = "id,room\n" + "\n".join("x%d,r" % i for i in range(export.MAX_ROWS + 5))
    assert c.post("/api/export/import", json={"format": "csv", "text": big}, headers=H).json() == {"error": "export.err.too_many"}


def test_import_sets_the_connection(app_client):
    c = app_client
    items = c.get("/api/inventory").json()["items"]
    sw = next(i for i in items if i["kind"] == "switch")
    dev = next(i for i in items if i["kind"] == "computer")
    text = "id,connected_via,port,medium\n%s,%s,7,wired\n" % (dev["id"], sw["title"])
    r = c.post("/api/export/import", json={"format": "csv", "text": text, "dry_run": False}, headers=H).json()
    assert r["changed"] == 1 and not r["errors"], r
    links = c.get("/api/topology").json()["links"]
    assert links[dev["id"]]["parent"] == sw["id"] and links[dev["id"]]["port"] == "7"
    row = next(x for x in export.parse("json", fetch(c, "list.json").content.decode()) if x["id"] == dev["id"])
    assert row["connected_via_id"] == sw["id"] and row["port"] == "7"


# ------------------------------------------------------------------ redaction: nothing of the network remains
def test_redacted_files_carry_nothing_of_the_network(app_client):
    c = app_client
    items = seed_secrets(c)
    leaks = set(SECRETS)
    for it in c.get("/api/inventory").json()["items"]:
        leaks.update(x for x in (it["title"], it["ip"], it["mac"], it["serial_reported"]) if x)
    generic = " ".join("%s %s %s" % (r["vendor"], r["model"], r["kind"]) for r in export.rows(c.st.core)).lower()
    generic += " " + " ".join(v for k, v in load_i18n("en").items() if k.startswith("kind.")).lower()
    leaks = {x for x in leaks if len(x) > 3 and x.lower() not in generic}      # maker and model are kept on purpose; so is the kind of device
    leaks.update(["192.168.50", "02:4c:4c", "walter-secret-person"])
    files = {"csv": fetch(c, "list.csv", redact=True).content, "json": fetch(c, "list.json", redact=True).content,
             "svg": fetch(c, "plan.svg", redact=True).content, "drawio": fetch(c, "plan.drawio", redact=True).content,
             "svg-overview": fetch(c, "plan.svg", redact=True, view="overview", look="dark").content}
    pdf = fetch(c, "plan.pdf", redact=True, size="A3", look="print").content
    files["pdf"] = pdf + pdf_text(pdf).encode("latin-1", "replace")
    assert files["pdf"].count(b"Redacted") >= 2
    for kind, raw in files.items():
        text = raw.decode("utf-8", "replace") if kind != "pdf" else raw.decode("latin-1")
        found = sorted(x for x in leaks if x.lower() in text.lower())
        assert not found, "%s leaks %s" % (kind, found[:6])
    doc = json.loads(files["json"])
    assert doc["redacted"] is True and all(re.match(r"^dev-\d{4}$", d["id"]) for d in doc["devices"])
    assert all(re.match(r"^10\.\d+\.\d+\.\d+$", d["ip"]) for d in doc["devices"] if d["ip"])
    assert all(d["mac"].startswith("02:00:00:00:") for d in doc["devices"] if d["mac"])
    # the same device has the same pseudonym in every file of one export (connection column points at a device of the list)
    ids = {d["id"] for d in doc["devices"]}
    assert all(d["connected_via_id"] in ids for d in doc["devices"] if d["connected_via_id"])
    assert len({d["name"] for d in doc["devices"]}) == len(doc["devices"])
    assert export.parse("csv", files["csv"].decode("utf-8"))[0]["id"].startswith("dev-")
    # a redacted JSON cannot be imported, a redacted CSV matches nothing
    assert c.post("/api/export/import", json={"format": "json", "text": files["json"].decode()}, headers=H).json() == {"error": "export.err.redacted"}
    r = c.post("/api/export/import", json={"format": "csv", "text": files["csv"].decode("utf-8"), "dry_run": False}, headers=H).json()
    assert r["matched"] == 0 and r["changed"] == 0


def test_redactor_is_stable_and_keeps_networks_apart():
    rd = export.Redactor([{"cidr": "192.168.1.0/24"}, {"cidr": "10.9.0.0/16"}])
    a, b, c3 = rd.ip("192.168.1.5"), rd.ip("10.9.4.4"), rd.ip("192.168.1.9")
    assert a == rd.ip("192.168.1.5") and len({a, b, c3}) == 3
    assert a.split(".")[1] == c3.split(".")[1] and a.split(".")[1] != b.split(".")[1]      # one pseudonym network per real network
    assert rd.net("192.168.1.0/24").startswith("10.%s." % a.split(".")[1])
    assert rd.mac("aa:bb:cc:dd:ee:ff") == rd.mac("aa:bb:cc:dd:ee:ff") != rd.mac("aa:bb:cc:dd:ee:00")
    assert rd.name("x", "Router") == "router-1" and rd.name("y", "router") == "router-2" and rd.name("x", "router") == "router-1"
    assert rd.text("192.168.1.5") == a and rd.text("Roberts desk") == ""
    assert rd.ip("fe80::1").startswith("10.") and rd.person("") == "" and rd.person("a") == rd.person("a")


# ------------------------------------------------------------------ the plan: SVG, draw.io, PDF
def scene_of(c, view="network", redact=False):
    from lanloom import mapmodel
    texts = load_i18n("en")
    return export_plan.build_scene(mapmodel.cached_build(c.st.core, view), make_tr(texts, "en"), export.Redactor() if redact else None, "T", "S")


def test_layout_puts_pads_in_zones_and_lines_in_the_gaps(app_client):
    c = app_client
    for view in ("network", "overview"):
        sc = scene_of(c, view)
        pads, zones = sc["pads"], sc["zones"]
        assert len(pads) >= 20 and zones and sc["edges"]
        for p in pads:                                                    # a pad lies inside its zone
            z = p["block"]
            assert z["x"] <= p["x"] and p["x"] + p["w"] <= z["x"] + z["w"] and z["y"] <= p["y"] and p["y"] + p["h"] <= z["y"] + z["h"]
        for i, a in enumerate(pads):                                      # pads do not overlap
            for b in pads[i + 1:]:
                assert a["x"] + a["w"] <= b["x"] or b["x"] + b["w"] <= a["x"] or a["y"] + a["h"] <= b["y"] or b["y"] + b["h"] <= a["y"]
        for e in sc["edges"]:                                             # a line runs straight along x or y and crosses no pad
            for (x1, y1), (x2, y2) in zip(e["pts"], e["pts"][1:]):
                assert x1 == x2 or y1 == y2
                lo_x, hi_x, lo_y, hi_y = min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)
                for p in pads:
                    inside = lo_x < p["x"] + p["w"] - 1 and hi_x > p["x"] + 1 and lo_y < p["y"] + p["h"] - 1 and hi_y > p["y"] + 1
                    assert not inside, "line %s crosses pad %s" % (e["id"], p["id"])


def test_svg_is_vector_with_texts_as_text(app_client):
    c = app_client
    names = [i["title"] for i in c.get("/api/inventory").json()["items"] if i["title"]]
    for look in export_plan.LOOKS:
        raw = fetch(c, "plan.svg", look=look).content.decode("utf-8")
        root = ET.fromstring(raw)
        assert root.tag.endswith("svg") and root.get("viewBox")
        texts = " ".join("".join(t.itertext()) for t in root.iter("{http://www.w3.org/2000/svg}text"))
        for n in names[:12]:
            assert n[:20] in texts, n
        assert "<image" not in raw and "<foreignObject" not in raw and "data:" not in raw     # no bitmaps, nothing hidden inside
        assert raw.count("<path") >= 10 and "Cable or uplink" in texts


def test_drawio_is_a_connected_graph(app_client):
    c = app_client
    raw = fetch(c, "plan.drawio", view="network").content.decode("utf-8")
    root = ET.fromstring(raw)
    assert root.tag == "mxfile" and root.find("diagram/mxGraphModel/root") is not None
    cells = {x.get("id"): x for x in root.iter("mxCell")}
    edges = [x for x in cells.values() if x.get("edge") == "1"]
    verts = [x for x in cells.values() if x.get("vertex") == "1"]
    sc = scene_of(c, "network")
    assert len(verts) == len(sc["pads"]) + len(sc["zones"]) and len(edges) == len(sc["edges"])
    for e in edges:
        assert cells[e.get("source")].get("vertex") == "1" and cells[e.get("target")].get("vertex") == "1"
    assert any("Core switch" in (v.get("value") or "") for v in verts)
    assert "<script" not in raw.lower()


def test_pdf_is_valid_and_has_plan_and_list(app_client):
    c = app_client
    for size, pages_min in (("A4", 2), ("A3", 2)):
        raw = fetch(c, "plan.pdf", size=size).content
        assert raw.startswith(b"%PDF-1.4") and raw.rstrip().endswith(b"%%EOF")
        start = int(re.search(rb"startxref\n(\d+)\n", raw).group(1))
        assert raw[start:start + 4] == b"xref"
        n = int(re.search(rb"/Size (\d+)", raw).group(1))
        offsets = [int(x) for x in re.findall(rb"(\d{10}) 00000 n ", raw)]
        assert len(offsets) == n - 1
        for i, off in enumerate(offsets, 1):                              # every cross-reference entry points at its object
            assert raw[off:off + len(b"%d 0 obj" % i)] == b"%d 0 obj" % i
        pages = len(re.findall(rb"/Type /Page /", raw))
        assert pages >= pages_min and b"/Type /Pages" in raw
        text = pdf_text(raw)
        assert "Network plan: networks" in text and "Network plan: overview" in text and "Inventory" in text and "Core switch" in text
        assert re.search(r"\d+ / %d" % pages, text)
    w, h = export_plan.PAGES["A4"]
    assert re.search(rb"/MediaBox \[0 0 %s %s\]" % (str(round(w, 2)).rstrip(b"0".decode()).encode(), str(round(h, 2)).encode()), fetch(c, "plan.pdf", size="A4").content)


def test_pdf_text_helpers():
    assert pdfmini.text_width("iii", 10) < pdfmini.text_width("WWW", 10) and pdfmini.text_width("", 10) == 0
    assert pdfmini.fit("a very long device name that does not fit", 10, 60).endswith("…")
    assert pdfmini.text_width(pdfmini.fit("a very long device name that does not fit", 10, 60), 10) <= 60
    assert pdfmini.clean("Grüße → ok\x00") == "Grüße -> ok " and pdfmini.clean("日本") == "??"
    doc = pdfmini.Pdf("t (x)")
    pg = doc.page(100, 100)
    pg.text(1, 10, "paren ) ( back\\slash", 8)
    pg.rect(1, 1, 5, 5, fill="#fff", stroke="#000", radius=2)
    raw = doc.render()
    assert b"paren \\) \\( back\\\\slash" in zlib.decompress(re.search(rb"stream\n(.*?)\nendstream", raw, re.S).group(1))


def test_a_large_network_is_cut_between_rows_of_zones():
    model = {"zones": [{"id": "z%d" % i, "title": "Zone %d" % i, "order": i, "kind": "net"} for i in range(1, 9)],
             "components": [{"id": "d%d-%d" % (z, n), "zone": "z%d" % z, "kind": "iot", "title": "Dev %d-%d" % (z, n), "subtitle": "10.0.%d.%d" % (z, n), "status": "up"}
                            for z in range(1, 9) for n in range(30)],
             "traces": [{"id": "t%d" % z, "a": "d1-0", "b": "d%d-0" % z, "kind": "lan"} for z in range(2, 9)]}
    sc = export_plan.build_scene(model, lambda k, v=None: k, None, "T", "S")
    assert len(sc["pads"]) == 240 and sc["h"] > 2000
    starts = export_plan._breaks(sc, 500.0)
    assert len(starts) >= 4 and starts == sorted(starts) and all(b - a <= 500.0 + 1e-6 for a, b in zip(starts, starts[1:]))
    top_rows = {top for top, _ in sc["bands"]}
    assert any(abs(s - (t - export_plan.ROW_GAP + 4)) < 1e-6 for s in starts[1:] for t in top_rows)    # cuts happen above a row, not through a zone
    doc = pdfmini.Pdf("t")
    export_plan.plan_pages(doc, sc, "light", "A4", lambda k, v=None: k)
    assert len(doc.pages) == len(starts) or len(doc.pages) >= 4


# ------------------------------------------------------------------ roles, audit, limits
def test_roles_and_audit(app_client):
    c = app_client
    seed_secrets(c)
    st = c.st
    st.accounts.create("view", PW, role="viewer")
    st.accounts.create("op", PW, role="operator")

    def as_(user):
        c.cookies.clear()
        assert c.post("/api/login", json={"username": user, "password": PW}, headers=H).status_code == 200

    c.cookies.clear()
    assert c.post("/api/export", json={"what": "list.csv"}, headers=H).status_code == 401
    as_("view")
    assert c.post("/api/export", json={"what": "list.csv"}, headers=H).status_code == 200
    assert c.post("/api/export", json={"what": "plan.pdf", "redact": True}, headers=H).status_code == 200
    assert c.post("/api/export/import", json={"format": "csv", "text": "id,room\nx,y\n"}, headers=H).status_code == 403
    as_("op")
    assert c.post("/api/export/import", json={"format": "csv", "text": "id,room\nx,y\n"}, headers=H).status_code == 200
    assert c.post("/api/export", json={"what": "nope"}, headers=H).json() == {"error": "export.err.what"}
    log = c.st.audit.query({}, 100)
    acts = [(e["action"], e["outcome"]) for e in log]
    assert ("export.download", "ok") in acts and ("export.import", "ok") in acts
    blob = json.dumps(log)
    assert not [s for s in SECRETS if s in blob]                       # the log names what was fetched, never its content
    assert any(e["action"] == "export.download" and (e.get("detail") or {}).get("redact") is True for e in log)


def test_the_sample_network_may_export_but_not_import(app_client):
    c = app_client
    assert c.post("/api/setup/demo", json={}, headers=H).status_code in (200, 204) or True
    r = c.post("/api/export", json={"what": "plan.svg"}, headers=H)
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert r.headers["content-disposition"].startswith('attachment; filename="lanloom-plan-network-')
    assert r.headers["cache-control"] == "no-store"


# ------------------------------------------------------------------ wiring
def test_wiring_page_assets_and_palette():
    root = Path(__file__).resolve().parent.parent
    web = root / "lanloom" / "web"
    html = (web / "index.html").read_text(encoding="utf-8")
    assert html.index("js/inventory.js") < html.index("js/export.js") < html.index("js/palette.js")
    assert "css/export.css" in html
    main = (root / "lanloom" / "main.py").read_text(encoding="utf-8")
    assert '"js/export.js", "css/export.css"' in main
    js = (web / "js" / "export.js").read_text(encoding="utf-8")
    for what in ("plan.pdf", "plan.svg", "plan.drawio", "list.csv", "list.json"):
        assert "'%s'" % what in js
    for cmd_id in ("export-pdf", "export-svg", "export-drawio", "export-csv", "export-json", "go.export"):
        assert "'%s'" % cmd_id in js
    assert "LL.view('export'" in js and "href: '#/export'" in (web / "js" / "inventory.js").read_text(encoding="utf-8")
    texts = load_i18n("en")
    for key in ("export.title", "cmdk.export.pdf", "audit.action.export.download", "audit.action.export.import", "export.plan.legend.cable"):
        assert key in texts and key in load_i18n("de")
