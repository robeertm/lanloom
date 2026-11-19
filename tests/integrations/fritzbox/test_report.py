"""The night report section (fixed clock): nothing without a state, changes since the last report, checks."""
import copy

from lanloom.integrations.fritzbox import model, report
from lanloom.nightly import contrib

from . import fixtures as fx
from .test_model import raw_of

NOW = 1_800_000_000.0


def state(**kw):
    return model.to_neutral(raw_of(**kw))


def stored(st):
    """What the night report keeps of a state for the next night."""
    return {k: v for k, v in st.items() if k not in contrib.FAMILY_STATE_DROP}


def keys(sec):
    return [i["key"] for i in sec["items"]]


def test_no_state_no_section_and_other_families_are_ignored():
    assert report.night_section(None, None, NOW) is None
    assert report.night_section(None, {"hosts": [], "controller": {}}, NOW) is None
    assert report.night_section(None, {"controller": {"version": "1", "class": "access_controller"}}, NOW) is None


def test_first_night_is_calm_when_nothing_is_wrong():
    sec = report.night_section(None, state(), NOW, "Home router")
    assert sec["family"] == "fritzbox" and sec["title"] == "night.home.title" and sec["name"] == "Home router"
    assert sec["items"] == [] and sec["summary"] == "night.home.summary_calm" and sec["look"] == 0
    assert sec["params"] == {"n": 0, "devices": 2, "online": 2, "hosts": 4, "changes": 0}


def test_first_night_lists_the_checks():
    st = state(wans=[{"kind": "ip", "status": "Disconnected"}], mesh=fx.mesh_doc(repeater_up=False),
               hosts=[h for h in fx.HOSTS if h[0] != fx.REPEATER])
    sec = report.night_section(None, st, NOW)
    assert keys(sec) == ["fwr.finding.wan_down", "fwr.finding.mesh_device_offline"]
    assert sec["summary"] == "night.home.summary_look" and sec["look"] == 2


def test_a_repeater_that_went_offline_is_said_once_as_a_change():
    before = stored(state())
    now = state(mesh=fx.mesh_doc(repeater_up=False), hosts=[h for h in fx.HOSTS if h[0] != fx.REPEATER])
    sec = report.night_section(before, now, NOW)
    assert keys(sec) == ["night.home.device_down"] and sec["items"][0]["level"] == "warn"
    assert sec["items"][0]["params"] == {"device": "Repeater upstairs"} and sec["params"]["changes"] == 1
    again = report.night_section(stored(now), now, NOW)
    assert keys(again) == ["fwr.finding.mesh_device_offline"], "still down tonight: the check, not a change"
    back = report.night_section(stored(now), state(), NOW)
    assert keys(back) == ["night.home.device_up"] and back["items"][0]["level"] == "info"


def test_new_and_vanished_mesh_devices_and_firmware_changes():
    before = stored(state())
    mesh = fx.mesh_doc()
    mesh["nodes"][1]["device_firmware_version"] = "7.58"
    sec = report.night_section(before, state(mesh=mesh), NOW)
    assert keys(sec) == ["night.home.firmware"]
    assert sec["items"][0]["params"] == {"device": "Repeater upstairs", "before": "7.57", "now": "7.58"}
    gone = report.night_section(before, state(mesh=None), NOW)
    assert "night.home.device_gone" in keys(gone)
    new = report.night_section(gone and stored(state(mesh=None)), state(), NOW)
    assert "night.home.device_new" in keys(new)


def test_the_box_firmware_change_is_reported():
    before = stored(state())
    raw = raw_of()
    raw["info"] = dict(raw["info"], firmware="7.60")
    sec = report.night_section(before, model.to_neutral(raw), NOW)
    assert keys(sec) == ["night.home.firmware"] and sec["items"][0]["params"]["now"] == "7.60"


def test_devices_the_router_did_not_know_are_a_note_with_the_first_names():
    before = stored(state())
    hosts = list(fx.HOSTS) + [(fx.mac(0x40 + i), "192.168.178.%d" % (100 + i), "Guest %d" % i, "802.11", 0, True) for i in range(5)]
    sec = report.night_section(before, state(hosts=hosts), NOW)
    assert keys(sec) == ["night.home.hosts_new"] and sec["items"][0]["level"] == "info"
    assert sec["items"][0]["params"] == {"n": 5, "names": "Guest 0, Guest 1, Guest 2 …"}
    assert sec["look"] == 0 and sec["summary"] == "night.home.summary_calm" and sec["params"]["changes"] == 5


def test_a_device_that_only_went_to_sleep_is_not_news():
    before = stored(state())
    hosts = [h for h in fx.HOSTS if h[0] != fx.TABLET] + [(fx.TABLET, "192.168.178.52", "Kitchen tablet", "802.11", 0, False)]
    assert report.night_section(before, state(hosts=hosts), NOW)["items"] == []


def test_the_section_is_stable_and_does_not_change_its_inputs():
    before, now = stored(state()), state(wans=[{"kind": "ip", "status": "Disconnected"}])
    b0, n0 = copy.deepcopy(before), copy.deepcopy(now)
    a = report.night_section(before, now, NOW, "x")
    assert a == report.night_section(before, now, NOW, "x") and before == b0 and now == n0


def test_a_previous_state_of_another_family_counts_as_the_first_night():
    other = {"controller": {"class": "access_controller"}, "equipment": []}
    sec = report.night_section(other, state(), NOW)
    assert sec["params"]["changes"] == 0 and sec["items"] == []
