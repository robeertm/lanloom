"""Radio classification of hub registry entries (the hub's wire-protocol identifiers live only in this file) and the
snapshot fields built from them: radio, role, address hints, entity class and "last seen" flag."""
import pytest

from lanloom.sources import hub as m
from lanloom.sources import hub_radio as hr


# ---------------------------------------------------------------------- classify
def test_mesh_device_by_its_first_identifier():
    assert hr.classify({"identifiers": [["zha", "00:12:4b:00:aa:bb:cc:01"]]}, []) == ("mesh154", "device")


def test_mesh_coordinator_by_model_or_name():
    dev = {"identifiers": [["zha", "00:12:4b:00:aa:bb:cc:02"]], "model": "Sample Coordinator v2"}
    assert hr.classify(dev, []) == ("mesh154", "coordinator")
    dev = {"identifiers": [["zha", "00:12:4b:00:aa:bb:cc:03"]], "name": "Main coordinator stick"}
    assert hr.classify(dev, []) == ("mesh154", "coordinator")
    assert hr.classify({"identifiers": [["deconz", "00:12:4b:00:aa:bb:cc:04"]], "model": "Plug"}, []) == ("mesh154", "device")


def test_mesh_bridge_over_the_message_broker():
    assert hr.classify({"identifiers": [["mqtt", "zigbee2mqtt_0x00158d0001"]]}, []) == ("mesh154", "device")
    assert hr.classify({"identifiers": [["mqtt", "zigbee2mqtt_bridge_0x00158d0002"]]}, []) == ("mesh154", "coordinator")
    # another broker device is not a radio device
    assert hr.classify({"identifiers": [["mqtt", "sample_node_1"]]}, []) == (None, None)


def test_subghz_node_one_is_the_controller():
    assert hr.classify({"identifiers": [["zwave_js", "3967855007-1"]]}, []) == ("subghz", "coordinator")
    assert hr.classify({"identifiers": [["zwave_js", "3967855007-5"]]}, []) == ("subghz", "device")
    assert hr.classify({"identifiers": [["zwave_js", "3967855007-15"]]}, []) == ("subghz", "device")


def test_ip_standard_thread_and_ble_by_domain():
    assert hr.classify({"identifiers": [["matter", "deviceid_1234"]]}, []) == ("ipstd", "device")
    assert hr.classify({"identifiers": [["otbr", "x1"]]}, []) == ("thread", "border_router")
    assert hr.classify({"identifiers": [["sample_other", "x"]]}, ["otbr"]) == ("thread", "border_router")
    assert hr.classify({"identifiers": [["sample_other", "x"]]}, ["bluetooth"]) == ("ble", "receiver")


def test_domain_of_the_config_entry_decides_when_identifiers_are_unknown():
    assert hr.classify({"identifiers": [["sample_hub", "n1"]]}, ["zha"]) == ("mesh154", "device")
    assert hr.classify({"identifiers": []}, ["matter"]) == ("ipstd", "device")


def test_connection_types():
    dev = {"identifiers": [["sample_hub", "n1"]], "connections": [["bluetooth", "AA:BB:CC:00:11:22"]]}
    assert hr.classify(dev, ["sample_hub"]) == ("ble", "device")
    dev = {"identifiers": [["sample_hub", "n2"]], "connections": [["zigbee", "00:12:4b:00:aa:bb:cc:05"]]}
    assert hr.classify(dev, []) == ("mesh154", "device")
    # a plain wireless-LAN device: no radio at all
    dev = {"identifiers": [["sample_hub", "n3"]], "connections": [["mac", "aa:bb:cc:00:11:33"]]}
    assert hr.classify(dev, ["sample_hub"]) == (None, None)


@pytest.mark.parametrize("garbage", [None, 5, "x", [], {}, {"identifiers": "zha"}, {"identifiers": [["zha"]]},
                                     {"identifiers": [[1, 2]], "connections": [None, 3, ["bluetooth"]]},
                                     {"identifiers": [["zha", None]], "model": 7}])
def test_garbage_is_safe(garbage):
    kind, role = hr.classify(garbage, None)
    assert (kind, role) in ((None, None), ("mesh154", "device"))


def test_pairs_are_capped_and_values_stringified():
    many = [["sample_hub", str(i)] for i in range(40)] + [["zha", "late"]]
    assert hr.classify({"identifiers": many}, []) == (None, None)      # beyond the first 16 nothing is read
    assert hr.classify({"identifiers": [["zha", 12345]]}, []) == ("mesh154", "device")


# ---------------------------------------------------------------------- snapshot fields
def _snap(devices, entities=(), states=None, entries=()):
    return m.build_snapshot(states or {}, list(devices), list(entities), [], list(entries))


def _dev(did, **kw):
    return dict({"id": did, "name": did, "identifiers": [["sample_hub", did]], "connections": [], "config_entries": []}, **kw)


def test_snapshot_sets_radio_and_role_from_raw_registry_entries():
    snap = _snap([
        _dev("d1", identifiers=[["zha", "00:12:4b:00:aa:bb:cc:10"]], model="Radio Coordinator"),
        _dev("d2", identifiers=[["zha", "00:12:4b:00:aa:bb:cc:11"]], via_device_id="d1"),
        _dev("d3", identifiers=[["sample_hub", "x"]], config_entries=["e1"]),
        _dev("d4", identifiers=[["sample_hub", "y"]]),
    ], entries=[{"entry_id": "e1", "domain": "bluetooth"}])
    by = {d["id"]: d for d in snap["devices"]}
    assert (by["d1"]["radio"], by["d1"]["role"]) == ("mesh154", "coordinator")
    assert (by["d2"]["radio"], by["d2"]["role"], by["d2"]["via"]) == ("mesh154", "device", "d1")
    assert (by["d3"]["radio"], by["d3"]["role"], by["d3"]["domains"]) == ("ble", "receiver", ["bluetooth"])
    assert (by["d4"]["radio"], by["d4"]["role"]) == (None, None)


def test_snapshot_never_carries_a_raw_identifier():
    snap = _snap([_dev("d1", identifiers=[["zha", "00:12:4b:00:aa:bb:cc:12"]])])
    assert "00:12:4b:00:aa:bb:cc:12" not in repr(snap) and "identifiers" not in snap["devices"][0]


@pytest.mark.parametrize("url, ip, host", [
    ("http://192.168.1.20/", "192.168.1.20", None),
    ("http://192.168.1.20:8080/config", "192.168.1.20", None),
    ("192.168.1.21", "192.168.1.21", None),
    ("http://plug-1.local:80", None, "plug-1.local"),
    ("https://Plug-2.Example.Test./x", None, "plug-2.example.test"),
    ("javascript:alert(1)", None, None),
    ("ftp://192.168.1.22/", None, None),
    ("javascript://alert(1)", None, None),
    ("http://", None, None),
    ("", None, None),
    (None, None, None),
    (12, None, None),
    ("http://bad host/", None, None),
    ("http://" + "a" * 600, None, None),
])
def test_url_hints(url, ip, host):
    assert m._url_hints(url) == (ip, host)


def test_snapshot_hints_from_configuration_url():
    snap = _snap([_dev("d1", configuration_url="http://192.168.1.20/"), _dev("d2", configuration_url="http://plug-1.local:80"),
                  _dev("d3", configuration_url="not a url!"), _dev("d4")])
    by = {d["id"]: d for d in snap["devices"]}
    assert (by["d1"]["ip_hint"], by["d1"]["host_hint"]) == ("192.168.1.20", None)
    assert (by["d2"]["ip_hint"], by["d2"]["host_hint"]) == (None, "plug-1.local")
    assert (by["d3"]["ip_hint"], by["d3"]["host_hint"]) == (None, None)
    assert (by["d4"]["ip_hint"], by["d4"]["host_hint"]) == (None, None)


@pytest.mark.parametrize("eid, tkey, dc, name, want", [
    ("sensor.lamp_x", "last_seen", None, None, True),
    ("sensor.lamp_last_seen", None, None, None, True),
    ("sensor.lamp_ts", None, "timestamp", "Lamp Last seen", True),
    ("sensor.lamp_ts", None, "timestamp", "Lamp boot time", False),
    ("sensor.lamp_ts", None, None, "Lamp last seen", False),
    ("sensor.lamp", None, None, None, False),
])
def test_last_seen_entity_rule(eid, tkey, dc, name, want):
    assert m._last_seen_entity(eid, tkey, dc, name) is want


def test_snapshot_entity_fields():
    states = {"sensor.lamp_battery": ["80", "%", "Lamp battery", "battery"],
              "sensor.lamp_last_seen": ["2026-11-16T07:00:00+00:00", None, "Lamp last seen", "timestamp"],
              "light.lamp": ["on", None, "Lamp", None]}
    ents = [{"entity_id": "sensor.lamp_battery", "device_id": "d1", "entity_category": "diagnostic", "platform": "sample_hub"},
            {"entity_id": "sensor.lamp_last_seen", "device_id": "d1", "entity_category": "config"},
            {"entity_id": "light.lamp", "device_id": "d1", "entity_category": "other", "translation_key": None},
            {"entity_id": "sensor.lamp_seen_by_key", "device_id": "d1", "translation_key": "last_seen"}]
    snap = _snap([_dev("d1", area_id="hall")], ents, states)
    by = {e["entity_id"]: e for e in snap["entities"]}
    b = by["sensor.lamp_battery"]
    assert (b["dc"], b["cat"], b["platform"], b["ls"], b["unit"], b["area"]) == ("battery", "diagnostic", "sample_hub", False, "%", "hall")
    ls = by["sensor.lamp_last_seen"]
    assert (ls["ls"], ls["cat"], ls["dc"]) == (True, "config", "timestamp")
    assert by["light.lamp"]["cat"] is None and by["light.lamp"]["platform"] is None and by["light.lamp"]["ls"] is False
    assert by["sensor.lamp_seen_by_key"]["ls"] is True


def test_snapshot_entities_without_registry_entry_get_the_flag_too():
    snap = _snap([], [], {"sensor.thing_last_seen": ["unknown", None, None, None], "sensor.plain": ["1", None, None, None]})
    by = {e["entity_id"]: e for e in snap["entities"]}
    assert by["sensor.thing_last_seen"]["ls"] is True and by["sensor.plain"]["ls"] is False
