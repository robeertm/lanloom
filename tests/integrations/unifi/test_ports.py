"""The read half of the controller family for the switch front view: ports with errors, radios with clients, the port and wireless name
of each client, the ports of the light poll, and the size of the intrusion detection's suppression list."""
import json

import pytest

from lanloom.integrations.unifi import model

from . import fixtures as fx


def raw_with_counters():
    raw = fx.classic()
    sw = raw["device"]["data"][1]
    sw["port_table"][0].update(rx_errors=4, tx_errors=1, rx_dropped=2, full_duplex=True)
    raw["device"]["data"][2]["radio_table"] = [{"radio": "ng", "channel": 6}, {"radio": "na", "channel": 36}, {"radio": "xx", "channel": 1}]
    raw["device"]["data"][2]["radio_table_stats"] = [{"radio": "ng", "channel": 6, "num_sta": 3}, {"radio": "na", "channel": 36, "user-num_sta": 5}]
    return raw


def test_ports_carry_errors_and_duplex_and_radios_carry_clients():
    st = model.to_neutral(raw_with_counters())
    p2 = next(p for p in st["ports"] if p["idx"] == 2)
    assert (p2["rx_errors"], p2["tx_errors"], p2["rx_dropped"], p2["duplex"]) == (4, 1, 2, "full")
    ap = next(e for e in st["equipment"] if e["name"] == "AP hall")
    assert ap["radios"] == [{"band": "2.4ghz", "channel": 6, "clients": 3}, {"band": "5ghz", "channel": 36, "clients": 5}]
    assert next(e for e in st["equipment"] if e["name"] == "Gateway")["radios"] == []


def test_clients_know_their_port_or_wireless_name():
    st = model.to_neutral(fx.classic())
    office = next(d for d in st["devices"] if d["name"] == "Office PC")
    phone = next(d for d in st["devices"] if d["name"] == "Phone")
    assert office["port"] == 2 and office["ssid"] is None
    assert phone["port"] is None and phone["ssid"] == "Home" and phone["band"] == "5ghz"


def test_the_light_poll_carries_ports_and_equipment_with_neutral_ids():
    ev = model.topology_event(raw_with_counters(), 1.0)
    st = model.to_neutral(raw_with_counters())
    assert {p["id"] for p in ev["ports"]} == {p["id"] for p in st["ports"]}
    assert {e["id"] for e in ev["equipment"]} == {e["id"] for e in st["equipment"]}
    assert all(set(p) == {"id", "device", "idx", "link", "is_uplink", "rx_bytes", "tx_bytes", "rx_errors", "tx_errors"} for p in ev["ports"])
    sw = next(e["id"] for e in ev["equipment"] if e["ip"] == "192.168.50.2")
    assert {(p["device"], p["idx"]) for p in ev["ports"] if p["is_uplink"]} == {(sw, 8)}   # the switch's own uplink port (#21)
    assert "ports" not in model.topology_event({"sta": fx.classic()["sta"]}, 1.0)          # no device list, no ports


def test_the_intrusion_detection_list_is_only_counted():
    raw = fx.classic()
    raw["ips"] = {"meta": {"rc": "ok"}, "data": [{"key": "ips", "mode": "ips", "suppression": {"alerts": [{"gid": 1, "id": n, "tracking": [{"direction": "src", "value": "marker-%d" % n}]} for n in range(30)],
                                                                                           "whitelist": [{"direction": "src", "value": "marker-trusted"}]}}]}
    st = model.to_neutral(raw)
    assert st["ids"] == {"suppressions": 31, "enabled": True}
    assert "marker-" not in json.dumps(st)
    assert "ids" not in model.to_neutral(fx.classic())


def test_the_reader_may_ask_for_the_ids_setting_and_nothing_wider():
    from lanloom.integrations import transport
    from lanloom.integrations.unifi import read
    read.ALLOW.check("GET", "/api/s/default/get/setting/ips", None)
    read.ALLOW.check("GET", "/proxy/network/api/s/default/get/setting/ips", None)
    for path in ("/api/s/default/get/setting", "/api/s/default/get/setting/mgmt", "/api/s/default/rest/setting", "/api/s/default/get/setting/ips/x"):
        with pytest.raises(transport.Forbidden):
            read.ALLOW.check("GET", path, None)


