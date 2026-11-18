"""The second DNS filter family in the app: its reading is booked like the first one's (counts per client may be unknown)."""
import time
from pathlib import Path
from types import SimpleNamespace

from lanloom import core as core_mod
from lanloom import dnsfilter, mapmodel
from lanloom.integrations.adguard import model
from lanloom.store import Store

from integrations.adguard import fixtures as fx


def test_an_adguard_reading_is_booked_and_shown():
    c = core_mod.Core(SimpleNamespace(data_dir=Path("/nonexistent/lanloom-test"), probe=True, demo=False), Store(":memory:"), None)
    c.ingest({"hosts": [{"ip": "192.0.2.10", "mac": "02:4c:4c:00:00:10", "hostnames": ["laptop"], "ports": [], "mdns": [], "ssdp": [],
                         "sources": ["arp"]}]}, None)
    c._refresh_index()
    iid = c.save_integration("adguard", "Filter", {"url": "http://192.0.2.2:3000", "username": "u"}, {"password": "x"})["id"]
    now = time.time()
    st = model.to_neutral(fx.DATA, now)
    c.on_event("int:%d" % iid, iid, {"type": "dns", "source": "family", "family": "adguard", "t": now, "state": st})
    assert sum(r[1] for r in c.dns_log.hours(iid, 24, now)) == 2400
    v = dnsfilter.filter_view(c, iid, now)
    assert v["totals"]["blocked"] == 615 and v["upstream_ok"] is True and v["health"] == "ok"
    assert v["top_clients"][0]["b"] is None and v["top_clients"][0]["device"]
    mine = mapmodel.component_detail(c, v["top_clients"][0]["device"])["dns_client"]["filters"][0]
    assert mine["q"] == 1500 and mine["blocked_share"] is None
    assert "DNS" in {b for comp in mapmodel.build(c, "overview")["components"] if comp["id"] == "dns:%d" % iid for b in comp["badges"]}
