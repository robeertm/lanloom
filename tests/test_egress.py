"""Telemetry-free as a checked rule (SECURITY.md, docs/SECURITY-MODEL.md).

Every place in the shipped code that can open a connection, and every fixed public host, is listed in
security/egress.json with a category and a purpose. The bait tests prove the checker still fires on a new
connection or a new host, so it cannot go quiet by accident."""
import importlib.util
import json
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("egress_check", ROOT / "scripts" / "egress_check.py")
egress = importlib.util.module_from_spec(spec)
spec.loader.exec_module(egress)


def test_every_outbound_place_and_host_is_listed():
    res = egress.check(ROOT)
    assert not egress.report(res), egress.report(res)
    assert len(res["sites"]) >= 20                      # the scanner still finds the known places


def test_only_the_update_check_is_a_connection_of_its_own():
    """Fixed hosts the code connects to: the release feed, presets that are used only once set up, and the one source of
    the optional city data and of the optional exchange point lists of the network routes (both switched off by default)."""
    allow = json.loads((ROOT / "security" / "egress.json").read_text())
    own = [h["host"] for h in allow["hosts"] if h["use"] == "connect" and h["category"] == "update-check"]
    assert own == ["api.github.com"]
    optional = [h["host"] for h in allow["hosts"] if h["use"] == "connect" and h["category"] == "optional-download"]
    assert optional == ["download.db-ip.com", "www.peeringdb.com"]
    for h in allow["hosts"]:
        assert h["use"] == "link" or h["category"] in ("update-check", "configured-ai", "configured-channel", "optional-download"), h
    for s in allow["sites"]:
        assert s["category"] in egress.CATEGORIES, s


BAIT = {
    "lanloom/beacon.py": (
        "import urllib.request as u\n"
        "from socket import create_connection as cc\n"
        "import subprocess, asyncio\n"
        "def report():\n"
        "    u.urlopen('https://collector.metrics-cloud.net/v1')\n"
        "class Pinger:\n"
        "    def run(self):\n"
        "        cc(('203.0.113.9', 443))\n"
        "        subprocess.run(['curl', '-s', 'x'])\n"
        "    async def go(self, loop):\n"
        "        await loop.create_connection(lambda: None, 'x', 1)\n"
        "        await asyncio.open_connection('x', 1)\n"
    ),
    "lanloom/web/js/beacon.js": "fetch('https://beacon.tracker-host.io/hit');\n",
}


def bait_tree(tmp_path):
    for rel, text in BAIT.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (tmp_path / "updater").mkdir()
    (tmp_path / "security").mkdir()
    (tmp_path / "security" / "egress.json").write_text(json.dumps({"sites": [], "hosts": []}))
    return tmp_path


def test_bait_new_connections_and_hosts_are_red(tmp_path):
    res = egress.check(bait_tree(tmp_path))
    found = {k for k, _ in res["missing_sites"]}
    assert "lanloom/beacon.py::report::http" in found
    assert "lanloom/beacon.py::Pinger.run::socket" in found
    assert "lanloom/beacon.py::Pinger.run::net-tool" in found
    assert "lanloom/beacon.py::Pinger.go::socket" in found
    hosts = {h for h, _ in res["missing_hosts"]}
    assert hosts == {"collector.metrics-cloud.net", "beacon.tracker-host.io"}
    assert egress.main(["--root", str(tmp_path)]) == 1


def test_bait_listed_entries_turn_green_and_stale_ones_red(tmp_path):
    root = bait_tree(tmp_path)
    res = egress.check(root)
    allow = {"sites": [{"site": k, "category": "lan-scan", "purpose": "bait"} for k, _ in res["missing_sites"]],
             "hosts": [{"host": h, "use": "connect", "category": "configured-channel", "purpose": "bait"} for h, _ in res["missing_hosts"]]}
    allow["sites"].append({"site": "lanloom/gone.py::f::http", "category": "lan-scan", "purpose": "removed code"})
    (root / "security" / "egress.json").write_text(json.dumps(allow))
    res = egress.check(root)
    assert not res["missing_sites"] and not res["missing_hosts"]
    assert res["stale_sites"] == ["lanloom/gone.py::f::http"]
    allow["sites"].pop()
    allow["sites"][0]["category"] = "analytics"                       # unknown category
    (root / "security" / "egress.json").write_text(json.dumps(allow))
    assert egress.check(root)["bad_entries"]


def test_documentation_hosts_are_not_connections(tmp_path):
    root = bait_tree(tmp_path)
    (root / "lanloom" / "beacon.py").write_text("A = 'https://router.example.com/' + 'http://nas.local:5000' + 'http://x.invalid'\n")
    (root / "lanloom" / "web" / "js" / "beacon.js").write_text("// see https://docs.example.org\n")
    res = egress.check(root)
    assert not res["missing_hosts"] and not res["missing_sites"]


# ------------------------------------------------------------------ no telemetry library in the image
TELEMETRY_WORDS = re.compile(r"telemetry|analytics|tracing|exporter|sdk|apm|crash|metrics", re.I)
# the web framework needs the OpenTelemetry *API*, which does nothing without an SDK and an exporter
ALLOWED = {"opentelemetry-api"}


def test_no_telemetry_library_is_installed_in_the_image():
    names = []
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(re.split(r"[\[=<>!~ ]", line, 1)[0].lower().replace("_", "-"))
    assert len(names) > 10
    bad = [n for n in names if TELEMETRY_WORDS.search(n) and n not in ALLOWED]
    assert not bad, "telemetry/monitoring package in the image: %s" % bad


def test_the_framework_telemetry_hooks_are_off(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "1")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    monkeypatch.setenv("FASTAPI_OTEL_AUTO_CONFIGURE", "true")        # must not switch anything on
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.metrics-cloud.net")
    from lanloom import config as config_mod
    from lanloom.main import NO_TELEMETRY, create_app
    app = create_app(config_mod.load())
    conf = getattr(app, "_telemetry", None)
    if conf is None:
        pytest.skip("framework without built-in telemetry hooks")
    for key in ("tracing", "metrics", "logs", "operation_spans", "auto_configure"):
        assert conf[key] is False, key
    assert set(NO_TELEMETRY) <= set(conf)
    assert importlib.util.find_spec("opentelemetry.sdk") is None      # no SDK, so nothing could be exported
    assert os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"]                   # set, and still ignored
