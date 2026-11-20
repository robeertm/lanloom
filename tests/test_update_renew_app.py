"""The app side of the updater's self-renewal: what the update page and the nightly report make of the heartbeat and of
renew.json (lanloom/updates.py, lanloom/nightly/contrib.py)."""
import json
import time
from types import SimpleNamespace

from lanloom import __version__
from lanloom import updates as U
from updater import engine as E

CUR = tuple(int(x) for x in __version__.split("."))
OLDER = "%d.%d.%d" % (CUR[0], max(CUR[1] - 1, 0), 0) if CUR[1] else "0.9.0"


class MemStore:
    def __init__(self):
        self.settings = {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


def make(tmp_path, demo=False, addon=False, repo=U.OFFICIAL_REPO):
    cfg = SimpleNamespace(data_dir=tmp_path, update_check=True, demo=demo, release_feed="https://feed.example/r",
                          image_repo=repo, addon=addon)
    return U.Updates(cfg, MemStore())


def heartbeat(up, version, features=("signature", "previous", "renew")):
    up.dir.mkdir(parents=True, exist_ok=True)
    (up.dir / "sidecar.json").write_text(json.dumps({"version": version, "t": time.time(), "features": list(features)}))


def renew(up, state, frm=OLDER, to=__version__, reason=None, t=None):
    up.dir.mkdir(parents=True, exist_ok=True)
    rec = {"state": state, "from": frm, "to": to, "t": time.time() if t is None else t}
    if reason:
        rec["reason"] = reason
    (up.dir / "renew.json").write_text(json.dumps(rec))


def test_states_match_the_engine():
    assert U.RENEW_STATES == E.RENEW_STATES


def test_a_current_sidecar_that_renews_itself(tmp_path):
    up = make(tmp_path)
    heartbeat(up, __version__)
    st = up.state()
    assert st["sidecar_renews"] is True and st["sidecar_hint"] is None and st["renew"] is None
    renew(up, "done")
    st = up.state()
    assert st["renew"]["state"] == "done" and st["renew"]["to"] == __version__ and st["sidecar_hint"] is None


def test_a_sidecar_from_before_self_renewal_is_renewed_once_by_hand(tmp_path):
    up = make(tmp_path)
    heartbeat(up, OLDER, features=("signature", "previous"))
    st = up.state()
    assert st["sidecar_renews"] is False and st["sidecar_hint"] == "renew_once"
    assert st["signature"]["supported"] is True             # its own hint (signature) does not apply here


def test_a_failed_renewal_shows_the_command_calmly(tmp_path):
    up = make(tmp_path)
    heartbeat(up, OLDER)
    renew(up, "rolled_back", reason="update.renew.err.no_heartbeat")
    st = up.state()
    assert st["sidecar_hint"] == "renew_failed" and st["renew"]["reason"] == "update.renew.err.no_heartbeat"
    renew(up, "failed", reason="update.renew.err.restore")
    assert up.state()["sidecar_hint"] == "renew_failed"
    # a record about an older target is history: after the next update it no longer counts
    renew(up, "rolled_back", to=OLDER, reason="update.renew.err.no_heartbeat")
    assert up.state()["sidecar_hint"] is None


def test_a_skipped_renewal_keeps_the_manual_way(tmp_path):
    up = make(tmp_path)
    heartbeat(up, OLDER)
    renew(up, "skipped", reason="update.renew.skip.unofficial")
    assert up.state()["sidecar_hint"] == "renew_manual"


def test_renewing_is_shown_and_goes_stale(tmp_path):
    up = make(tmp_path)
    heartbeat(up, OLDER)
    renew(up, "renewing")
    assert up.state()["renew"]["state"] == "renewing"
    renew(up, "renewing", t=time.time() - U.RENEWING_FOR - 5)
    assert up.state()["renew"] is None


def test_renew_record_is_cleaned(tmp_path):
    up = make(tmp_path)
    heartbeat(up, __version__)
    up.dir.mkdir(parents=True, exist_ok=True)
    (up.dir / "renew.json").write_text(json.dumps({"state": "done", "from": "<b>", "to": "x", "reason": "<script>", "t": "no"}))
    rec = up.state()["renew"]
    assert rec == {"state": "done", "t": 0.0, "from": None, "to": None, "reason": None}
    (up.dir / "renew.json").write_text(json.dumps({"state": "weird"}))
    assert up.state()["renew"] is None
    (up.dir / "renew.json").write_text("not json")
    assert up.state()["renew"] is None


def test_no_sidecar_no_hint(tmp_path):
    up = make(tmp_path)
    renew(up, "rolled_back", reason="update.renew.err.no_heartbeat")
    st = up.state()
    assert st["sidecar"] is False and st["sidecar_renews"] is False and st["sidecar_hint"] is None


def test_addon_mode_is_untouched(tmp_path):
    up = make(tmp_path, addon=True)
    heartbeat(up, OLDER)
    renew(up, "rolled_back", reason="update.renew.err.no_heartbeat")
    st = up.state()
    assert st["mode"] == "addon" and st["sidecar"] is False
    assert st["sidecar_renews"] is False and st["renew"] is None and st["sidecar_hint"] is None


def test_demo_shows_a_renewed_updater(tmp_path):
    up = make(tmp_path, demo=True)
    st = up.state()
    assert st["sidecar_renews"] is True and st["sidecar_hint"] is None
    if st["previous"]:
        assert st["renew"]["state"] == "done" and st["renew"]["to"] == __version__


def test_nightly_report_mentions_only_a_failed_renewal():
    from lanloom.nightly import contrib as C
    now = time.time()

    class Core:
        @staticmethod
        def source_list():
            return []

    def ctx(rec):
        upd = {"renew": rec, "status": {}}
        return SimpleNamespace(update=upd, since=now - 86400, until=now + 1, events=lambda kind: [], core=Core(),
                               data_dir=None)

    def items(rec):
        sec = C._lanloom(ctx(rec))
        return [i for i in sec["items"] if i["id"].startswith("renewfail:")]

    got = items({"state": "rolled_back", "from": OLDER, "to": __version__, "t": now - 60,
                 "reason": "update.renew.err.no_heartbeat"})
    assert len(got) == 1 and got[0]["key"] == "report.self.renew_failed" and got[0]["point"] is True
    assert got[0]["vars"]["reason_key"] == "update.renew.err.no_heartbeat" and got[0]["level"] == "info"
    assert items({"state": "failed", "from": OLDER, "to": __version__, "t": now - 60})[0]["level"] == "warn"
    assert items({"state": "done", "from": OLDER, "to": __version__, "t": now - 60}) == []
    assert items({"state": "skipped", "from": OLDER, "to": __version__, "t": now - 60}) == []
    assert items({"state": "rolled_back", "from": OLDER, "to": __version__, "t": now - 3 * 86400}) == []


def test_ui_uses_the_hint_and_the_note():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "lanloom" / "web" / "js" / "views.js").read_text(encoding="utf-8")
    assert "'update.sidecar_hint.' + u.sidecar_hint" in js and "update.renew.done_at" in js
    assert "settings.sidecar.renews" in js and "update.renew.after" in js
