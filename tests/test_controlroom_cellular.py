"""Mobile-router card (lanloom/controlroom/cellular.py): signal table, bands, technology, data volume, reserve line, admin data, hub readings."""
import json

import pytest

from crhelp import T0, FakeCore, ent, gw_entry, hub_entry
from lanloom.controlroom import cellular


# ---------------------------------------------------------------------------------------------------- the signal table
@pytest.mark.parametrize("measure,value,grade", [
    ("rsrp", -79.9, 0), ("rsrp", -80, 0), ("rsrp", -80.1, 1), ("rsrp", -90, 1), ("rsrp", -90.1, 2), ("rsrp", -100, 2), ("rsrp", -100.1, 3),
    ("rsrp", -110, 3), ("rsrp", -110.1, 4), ("rsrp", -140, 4),
    ("rsrq", -10, 0), ("rsrq", -10.5, 1), ("rsrq", -15, 1), ("rsrq", -20, 2), ("rsrq", -25, 3), ("rsrq", -25.1, 4),
    ("sinr", 20, 0), ("sinr", 19.9, 1), ("sinr", 13, 1), ("sinr", 12.9, 2), ("sinr", 0, 2), ("sinr", -0.1, 3), ("sinr", -5, 3), ("sinr", -5.1, 4),
    ("rssi", -65, 0), ("rssi", -75, 1), ("rssi", -85, 2), ("rssi", -95, 3), ("rssi", -95.5, 4),
    ("rsrp", "-95", 2),
])
def test_grade_limits_are_exact(measure, value, grade):
    assert cellular.grade_of(measure, value) == grade


@pytest.mark.parametrize("measure,value", [("rsrp", None), ("rsrp", ""), ("rsrp", "weak"), ("rsrp", float("nan")), ("rsrp", float("inf")),
                                           ("snr", -50), ("tech", 3), ("rsrp", [1])])
def test_grade_unknown_for_junk(measure, value):
    assert cellular.grade_of(measure, value) is None


def test_quality_is_the_worst_measure():
    q = cellular.quality({"rsrp": -75, "rsrq": -9, "sinr": -8})
    assert q["worst"] == "sinr" and q["grade"] == "bad" and q["level"] == "danger"
    assert q["measures"] == {"rsrp": "excellent", "rsrq": "excellent", "sinr": "bad"}
    q = cellular.quality({"rsrp": -105, "sinr": 15})
    assert q["grade"] == "poor" and q["level"] == "warn" and q["worst"] == "rsrp"
    q = cellular.quality({"rsrp": -95, "sinr": 5})
    assert q["grade"] == "fair" and q["level"] == "ok"
    assert cellular.quality({"rsrp": -70, "sinr": 25})["grade"] == "excellent"


def test_quality_rssi_only_counts_without_the_other_measures():
    assert cellular.quality({"rssi": -100})["grade"] == "bad"
    assert cellular.quality({"rssi": -100, "rsrp": -70})["measures"] == {"rsrp": "excellent"}
    q = cellular.quality({})
    assert q == {"grade": None, "level": "unknown", "worst": None, "measures": {}}
    assert cellular.quality({"rsrp": "n/a"})["level"] == "unknown"


# ---------------------------------------------------------------------------------------------------- bands and technology
def test_band_list_with_lte_and_nr():
    bands = cellular.parse_bands("B20+B1+B3+n78")
    assert [b["band"] for b in bands] == ["B20", "B1", "B3", "n78"]
    assert [b["kind"] for b in bands] == ["LTE", "LTE", "LTE", "NR"]
    assert bands[0] == {"band": "B20", "kind": "LTE", "duplex": "FDD", "mhz": 800, "role": "coverage"}
    assert bands[3]["mhz"] == 3500 and bands[3]["duplex"] == "TDD" and bands[3]["role"] == "capacity"


def test_band_list_various_spellings_and_unknown_bands():
    assert [b["band"] for b in cellular.parse_bands("b7, N28 / B 3")] == ["B7", "n28", "B3"]
    assert [b["band"] for b in cellular.parse_bands("LTE BAND 3")] == ["B3"]
    b = cellular.parse_bands("B99")[0]
    assert b["band"] == "B99" and b["mhz"] is None and b["duplex"] is None and b["role"] is None          # unknown band: named, not described
    assert cellular.parse_bands("B01")[0]["band"] == "B1"


@pytest.mark.parametrize("junk", [None, "", "no bands here", "Bandwidth 20 MHz", "B1234", "XB3", "banana 4", 123, "bn5", "B-3"])
def test_band_list_junk_gives_nothing(junk):
    assert cellular.parse_bands(junk) == []


def test_band_list_duplicates_and_upper_limit():
    assert [b["band"] for b in cellular.parse_bands("B3+b3+B3, n78 N78")] == ["B3", "n78"]
    many = "+".join("B%d" % i for i in range(1, 30))
    assert len(cellular.parse_bands(many)) == 8


@pytest.mark.parametrize("text,tech", [
    ("5G SA", "5G SA"), ("NR SA", "5G SA"), ("5G NSA", "5G NSA"), ("NR-NSA", "5G NSA"), ("nsa", "5G NSA"), ("5G", "5G"), ("NR", "5G"), ("5G+", "5G"),
    ("LTE", "LTE"), ("LTE-A", "LTE"), ("4G", "LTE"), ("LTE CA", "LTE"), ("WCDMA", "3G"), ("HSPA+", "3G"), ("UMTS", "3G"), ("3G", "3G"),
    ("EDGE", "2G"), ("GSM", "2G"), ("2G", "2G"), ("", ""), (None, ""), ("Satellite", "Satellite"), ("x" * 40, "x" * 16),
])
def test_technology(text, tech):
    assert cellular.tech_of(text) == tech


# ---------------------------------------------------------------------------------------------------- the card
def adapter_core(cells, lines=None, age=30):
    core = FakeCore()
    core.configs = [gw_entry(None, age, cellular=cells)]
    return core


def base(**kw):
    d = {"id": "c1", "line": "", "name": "Router", "tech": "LTE", "bands": "B3+B20", "carrier": "Sample Mobile", "rsrp": -95, "rsrq": -12, "sinr": 8,
         "rssi": -70, "data_used": 10e9, "data_limit": 100e9, "cell_id": "0x1A2B", "tac": "4711", "location": "Sample hill"}
    d.update(kw)
    return d


def card_of(**kw):
    core = adapter_core([base(**kw)])
    out = cellular.cards(core, T0)
    assert len(out) == 1
    return out[0]


def test_card_fields_and_age():
    c = card_of()
    assert c["name"] == "Router" and c["carrier"] == "Sample Mobile" and c["tech"] == "LTE" and c["source"] == "adapter"
    assert [b["band"] for b in c["bands"]] == ["B3", "B20"]
    assert c["signal"] == {"rsrp": -95.0, "rsrq": -12.0, "sinr": 8.0, "rssi": -70.0}
    assert c["quality"]["grade"] == "fair" and c["level"] == "ok"
    assert c["age_s"] == 30.0 and c["fresh"] == "fresh"


def test_card_with_bands_already_a_list_of_dicts():
    c = card_of(bands=[{"band": "B1", "kind": "LTE", "duplex": "FDD", "mhz": 2100, "role": "capacity"}])
    assert c["bands"][0]["band"] == "B1"


@pytest.mark.parametrize("used,limit,pct,level", [
    (10e9, 100e9, 10.0, "ok"), (79.9e9, 100e9, 79.9, "ok"), (80e9, 100e9, 80.0, "warn"), (99.9e9, 100e9, 99.9, "warn"),
    (100e9, 100e9, 100.0, "danger"), (150e9, 100e9, 150.0, "danger"), (10e9, None, None, "ok"), (10e9, 0, None, "ok"), (None, 100e9, None, "unknown"),
])
def test_data_volume_levels(used, limit, pct, level):
    c = card_of(data_used=used, data_limit=limit)
    assert c["data"]["pct"] == pct and c["data"]["level"] == level


def test_a_reserve_line_that_is_idle_is_never_worse_than_warn():
    lines = [{"id": "L", "active": False}]
    core = adapter_core([base(line="L", rsrp=-120, sinr=-9)])
    c = cellular.cards(core, T0, lines=lines)[0]
    assert c["quality"]["level"] == "danger" and c["level"] == "warn" and c["active"] is False
    lines = [{"id": "L", "active": True}]
    c = cellular.cards(core, T0, lines=lines)[0]
    assert c["level"] == "danger" and c["active"] is True


def test_an_active_flag_of_the_card_itself_is_kept_over_the_line():
    core = adapter_core([base(line="L", rsrp=-120, active=False)])
    c = cellular.cards(core, T0, lines=[{"id": "L", "active": True}])[0]
    assert c["active"] is False and c["level"] == "warn"


def test_the_card_on_the_active_line_comes_first():
    core = adapter_core([base(id="a", name="Alpha", line="A"), base(id="b", name="Zulu", line="B")])
    out = cellular.cards(core, T0, lines=[{"id": "A", "active": False}, {"id": "B", "active": True}])
    assert [c["name"] for c in out] == ["Zulu", "Alpha"]


def test_cards_ignore_broken_entries_and_survive_a_failing_core():
    core = adapter_core([{"id": 5, "name": "x"}, "junk", {"name": "no id"}, base()])
    assert len(cellular.cards(core, T0)) == 1

    class Broken(FakeCore):
        def live_configs(self, *a, **k):
            raise RuntimeError("boom")
    assert cellular.cards(Broken(), T0) == []


def test_text_fields_are_clipped():
    c = card_of(name="n" * 200, carrier="c" * 200)
    assert len(c["name"]) == 64 and len(c["carrier"]) == 48


# ---------------------------------------------------------------------------------------------------- admin data
def test_cell_id_tac_and_position_are_for_admins_only():
    core = adapter_core([base()])
    plain = cellular.cards(core, T0, admin=False)[0]
    assert "admin" not in plain
    text = json.dumps(plain)
    for secret in ("0x1A2B", "4711", "Sample hill"):
        assert secret not in text
    adm = cellular.cards(core, T0, admin=True)[0]
    assert adm["admin"] == {"cell_id": "0x1A2B", "tac": "4711", "location": "Sample hill"}


def test_admin_block_only_has_what_the_router_told():
    core = adapter_core([base(cell_id=None, tac="", location="x" * 100)])
    adm = cellular.cards(core, T0, admin=True)[0]
    assert list(adm["admin"]) == ["location"] and len(adm["admin"]["location"]) == 64


# ---------------------------------------------------------------------------------------------------- readings from a hub
def hub_core(entities, states, devices=None, t=T0 - 20):
    core = FakeCore()
    core.hub = {1: hub_entry(entities, states, devices or [{"id": "dev1", "name": "LTE router"}], t=t)}
    return core


def lte_entities(device="dev1", prefix="lte"):
    return [ent("sensor.%s_rsrp" % prefix, device, "RSRP", "dBm"), ent("sensor.%s_sinr" % prefix, device, "SINR", "dB"),
            ent("sensor.%s_operator" % prefix, device, "Operator"), ent("sensor.%s_data_used" % prefix, device, "Data used", "GB"),
            ent("sensor.%s_network" % prefix, device, "Network type"), ent("sensor.%s_band" % prefix, device, "Band"),
            ent("sensor.%s_cell" % prefix, device, "Cell ID")]


def lte_states(prefix="lte", t=T0 - 20):
    return {"sensor.%s_rsrp" % prefix: ("-97", t), "sensor.%s_sinr" % prefix: ("7,5", t), "sensor.%s_operator" % prefix: ("Sample Mobile", t),
            "sensor.%s_data_used" % prefix: ("38.4", t), "sensor.%s_network" % prefix: ("5G NSA", t), "sensor.%s_band" % prefix: ("B20+n78", t),
            "sensor.%s_cell" % prefix: ("0xCAFE", t)}


def test_hub_entities_make_one_card():
    core = hub_core(lte_entities(), lte_states())
    c = cellular.cards(core, T0)[0]
    assert c["source"] == "hub" and c["name"] == "LTE router" and c["id"] == "hub:1:dev1"
    assert c["signal"]["rsrp"] == -97.0 and c["signal"]["sinr"] == 7.5 and c["carrier"] == "Sample Mobile"
    assert c["tech"] == "5G NSA" and [b["band"] for b in c["bands"]] == ["B20", "n78"]
    assert c["data"]["used"] == 38.4e9 and c["data"]["pct"] is None and c["data"]["level"] == "ok"
    assert c["age_s"] == 20.0 and c["quality"]["grade"] == "fair"
    assert "admin" not in c and "0xCAFE" not in json.dumps(c)
    assert cellular.cards(core, T0, admin=True)[0]["admin"] == {"cell_id": "0xCAFE"}


def test_hub_data_volume_units_and_limit():
    ents = lte_entities() + [ent("sensor.lte_limit", "dev1", "Data limit", "GB")]
    st = lte_states()
    st["sensor.lte_limit"] = ("100", T0 - 20)
    c = cellular.cards(hub_core(ents, st), T0)[0]
    assert c["data"]["limit"] == 100e9 and c["data"]["pct"] == 38.4
    ents = [ent("sensor.lte_rsrp", "dev1", "RSRP", "dBm"), ent("sensor.lte_used", "dev1", "Data used", "MiB")]
    c = cellular.cards(hub_core(ents, {"sensor.lte_rsrp": ("-90", T0), "sensor.lte_used": ("2", T0)}), T0)[0]
    assert c["data"]["used"] == 2 * 1024 ** 2


def test_hub_unavailable_values_are_skipped():
    states = lte_states()
    states["sensor.lte_sinr"] = ("unavailable", T0)
    states["sensor.lte_operator"] = ("unknown", T0)
    c = cellular.cards(hub_core(lte_entities(), states), T0)[0]
    assert c["signal"]["sinr"] is None and c["carrier"] == ""


def test_hub_two_devices_make_two_cards():
    ents = lte_entities("dev1", "a") + lte_entities("dev2", "b")
    states = dict(lte_states("a"), **lte_states("b"))
    devices = [{"id": "dev1", "name": "Router A"}, {"id": "dev2", "name": "Router B"}]
    core = hub_core(ents, states, devices)
    assert sorted(c["name"] for c in cellular.cards(core, T0)) == ["Router A", "Router B"]


def test_a_router_without_signal_values_is_no_card():
    ents = [ent("sensor.r_operator", "dev1", "Operator"), ent("sensor.r_used", "dev1", "Data used", "GB"), ent("sensor.r_uptime", "dev1", "Uptime")]
    states = {"sensor.r_operator": ("Sample Mobile", T0), "sensor.r_used": ("3", T0), "sensor.r_uptime": ("100", T0)}
    assert cellular.cards(hub_core(ents, states), T0) == []
    assert cellular.cards(FakeCore(), T0) == []


def test_an_rssi_or_snr_alone_is_no_mobile_router():
    """Every Wi-Fi sensor has an RSSI and every DSL line an SNR margin: neither makes a mobile-router card on its own."""
    wifi = hub_core([ent("sensor.plug_rssi", "dev1", "RSSI", "dBm"), ent("sensor.plug_signal", "dev1", "Signal strength", "dBm")],
                    {"sensor.plug_rssi": ("-60", T0), "sensor.plug_signal": ("-60", T0)})
    assert cellular.cards(wifi, T0) == []
    dsl = hub_core([ent("sensor.dsl_snr", "dev1", "SNR margin", "dB")], {"sensor.dsl_snr": ("12", T0)})
    assert cellular.cards(dsl, T0) == []
    both = hub_core([ent("sensor.m_rssi", "dev1", "RSSI", "dBm"), ent("sensor.m_net", "dev1", "Network type")],
                    {"sensor.m_rssi": ("-70", T0), "sensor.m_net": ("LTE", T0)})
    assert len(cellular.cards(both, T0)) == 1


def test_entities_of_other_domains_and_without_device_are_ignored():
    ents = [ent("binary_sensor.x_rsrp", "dev1", "RSRP"), ent("sensor.y_rsrp", None, "RSRP")]
    assert cellular.cards(hub_core(ents, {"binary_sensor.x_rsrp": ("-80", T0), "sensor.y_rsrp": ("-80", T0)}), T0) == []


def test_hub_card_is_matched_to_a_network_device():
    core = hub_core(lte_entities(), lte_states())
    core.hub_by = {"1:dev1": "d-test-7"}
    assert cellular.cards(core, T0)[0]["device"] == "d-test-7"


def test_adapter_and_hub_cards_side_by_side():
    core = hub_core(lte_entities(), lte_states())
    core.configs = [gw_entry(None, 10, cellular=[base(name="Alpha")])]
    out = cellular.cards(core, T0)
    assert sorted(c["source"] for c in out) == ["adapter", "hub"]
