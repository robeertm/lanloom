"""Update channels (stable/preview), holding a version, going back, and "What's new?" (app side)."""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from lanloom import __version__
from lanloom import config as config_mod
from lanloom import updates as U
from lanloom import whatsnew as W
from lanloom.main import create_app

HDR = {"X-Lanloom": "1"}
PW = "a-long-test-password"


class MemStore:
    def __init__(self):
        self.settings = {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


def bump(v, minor=0, patch=0):
    a, b, c = (int(x) for x in v.split("."))
    return "%d.%d.%d" % (a, b + minor, c + patch)


CUR = __version__
NEXT = bump(CUR, minor=1)
PREV = bump(CUR, patch=-1) if int(CUR.split(".")[2]) else bump(CUR, minor=-1)


def make(tmp_path, repo="img", demo=False):
    cfg = SimpleNamespace(data_dir=tmp_path, update_check=True, demo=demo, release_feed="https://feed.example/r",
                          image_repo=repo)
    return U.Updates(cfg, MemStore())


def sidecar(up, **extra):
    up.dir.mkdir(parents=True, exist_ok=True)
    (up.dir / "sidecar.json").write_text(json.dumps(dict({"version": CUR, "t": time.time()}, **extra)))
    return up


def cache(up, stable, preview=None):
    up.store.set_setting("update_cache", {"checked": time.time(), "latest": stable, "notes": "stable notes",
                                          "url": "https://example.org/s", "error": None, "preview": preview})
    return up


def backup(up, frm, to, name="20261101-000000"):
    d = Path(up.cfg.data_dir) / "backups" / name
    d.mkdir(parents=True)
    (d / "backup.json").write_text(json.dumps({"from": frm, "to": to, "t": 1.0}))


# ------------------------------------------------------------------ release feed
FEED = [
    {"tag_name": "v1.40.0", "prerelease": False, "html_url": "https://example.org/140"},
    {"tag_name": "v1.41.0", "prerelease": True, "html_url": "https://example.org/141", "body": "preview notes"},
    {"tag_name": "edge", "prerelease": True},
    {"tag_name": "v1.42.0-rc1", "prerelease": True},
    {"tag_name": "v1.43.0", "prerelease": True, "draft": True},
]


def test_preview_is_the_newest_pre_release_with_a_plain_version():
    assert U.pick_release(FEED)["version"] == "1.40.0"
    p = U.pick_preview(FEED, "1.40.0")
    assert p["version"] == "1.41.0" and p["notes"] == "preview notes"


def test_preview_older_than_stable_or_missing_is_none():
    assert U.pick_preview(FEED, "1.41.0") is None
    assert U.pick_preview([{"tag_name": "v1.0.0", "prerelease": False}], "1.0.0") is None
    assert U.pick_preview([{"tag_name": "edge", "prerelease": True}, {"tag_name": "latest", "prerelease": True}], None) is None
    assert U.pick_preview("nonsense", None) is None


def test_stable_channel_never_offers_the_preview(tmp_path):
    up = cache(make(tmp_path), CUR, {"version": NEXT, "notes": "p", "url": "https://example.org/p"})
    s = up.state()
    assert s["channel"] == "stable" and s["available"] is False and s["latest"] == CUR
    assert s["preview"]["version"] == NEXT and s["preview_none"] is False


def test_preview_channel_offers_a_newer_pre_release(tmp_path):
    up = cache(make(tmp_path), CUR, {"version": NEXT, "notes": "p", "url": "https://example.org/p"})
    up.settings({"channel": "preview"})
    s = up.state()
    assert s["channel"] == "preview" and s["available"] and s["latest"] == NEXT and s["notes"] == "p"


def test_preview_channel_without_pre_release_says_so_and_offers_stable(tmp_path):
    up = cache(make(tmp_path), NEXT, None)
    up.settings({"channel": "preview"})
    s = up.state()
    assert s["preview_none"] is True and s["latest"] == NEXT and s["available"]


def test_cached_preview_that_is_not_newer_than_stable_is_ignored(tmp_path):
    up = cache(make(tmp_path), NEXT, {"version": CUR, "notes": "old"})
    up.settings({"channel": "preview"})
    assert up.state()["preview"] is None and up.state()["latest"] == NEXT


def test_unknown_channel_is_refused(tmp_path):
    up = make(tmp_path)
    with pytest.raises(ValueError, match="update.err.channel"):
        up.settings({"channel": "edge"})
    up.store.set_setting("update_channel", "edge")         # a broken stored value falls back to stable
    assert up.channel() == "stable"


# ------------------------------------------------------------------ hold
def test_hold_hides_the_notice_and_refuses_an_update(tmp_path):
    up = sidecar(cache(make(tmp_path), NEXT))
    s = up.settings({"hold": True})
    assert s["held"] == CUR and s["available"] is False and s["held_newer"] == NEXT
    with pytest.raises(ValueError, match="update.err.held"):
        up.request(NEXT)
    s = up.settings({"hold": False})
    assert s["held"] is None and s["available"] is True
    assert up.request(NEXT)["version"] == NEXT


# ------------------------------------------------------------------ previous version and going back
def test_previous_from_the_heartbeat_or_from_the_backup(tmp_path):
    up = make(tmp_path)
    assert up.previous() is None
    backup(up, PREV, CUR)
    assert up.previous()["version"] == PREV and up.previous()["when"] == 1.0
    sidecar(up, previous=PREV, features=["signature", "previous"])
    assert up.previous()["version"] == PREV
    sidecar(up, previous=NEXT)                          # never "back" to something newer
    assert up.previous()["version"] == PREV


def test_backup_for_another_update_is_not_a_rollback_target(tmp_path):
    up = make(tmp_path)
    backup(up, PREV, NEXT)
    assert up.previous() is None


def test_rollback_writes_a_marked_request_and_holds_the_target(tmp_path):
    up = sidecar(make(tmp_path), previous=PREV)
    s = up.state()
    assert s["previous"]["version"] == PREV and s["rollback_ok"]
    with pytest.raises(ValueError, match="update.err.rollback_mismatch"):
        up.rollback(NEXT)
    req = up.rollback(PREV)
    on_disk = json.loads((up.dir / "request.json").read_text())
    assert on_disk["rollback"] is True and on_disk["version"] == PREV == req["version"]
    assert up.held() == PREV


def test_rollback_without_previous_or_sidecar(tmp_path):
    up = make(tmp_path)
    with pytest.raises(ValueError, match="update.err.no_previous"):
        up.rollback(PREV)
    backup(up, PREV, CUR)
    assert up.state()["rollback_ok"] is False
    with pytest.raises(ValueError, match="update.err.no_sidecar"):
        up.rollback(PREV)


def test_no_rollback_of_the_official_image_to_a_version_before_signing(tmp_path):
    up = sidecar(make(tmp_path, repo=U.OFFICIAL_REPO), previous="1.28.0")
    s = up.state()
    assert s["rollback_unsigned"] is True and s["rollback_ok"] is False
    with pytest.raises(ValueError, match="update.err.rollback_unsigned"):
        up.rollback("1.28.0")


def test_signature_support_comes_from_the_heartbeat(tmp_path):
    up = sidecar(make(tmp_path, repo=U.OFFICIAL_REPO))
    assert up.state()["signature"] == {"supported": False, "enforced": True}
    sidecar(up, features=["signature", "previous"])
    assert up.state()["signature"] == {"supported": True, "enforced": True}


def test_official_repo_names_match():
    from updater import sigverify
    assert U.OFFICIAL_REPO == sigverify.OFFICIAL_REPO


# ------------------------------------------------------------------ what's new
SAMPLE = """# Changelog

## [Unreleased]
- not yet

## [9.9.9] - 2099-01-01
### Added
- from the future

## [1.2.0] – 2026-11-01
### Added
- **Bold** thing with a [link](https://example.org)
  continued on the next line
### Fixed
- a fix

## [1.1.0] - 2026-10-01
Plain line without a section.
"""


def test_changelog_parsing(tmp_path):
    entries = W.parse(SAMPLE)
    assert [e["version"] for e in entries] == ["9.9.9", "1.2.0", "1.1.0"]
    e = entries[1]
    assert e["date"] == "2026-11-01"
    assert e["sections"][0] == {"title": "Added", "items": ["**Bold** thing with a link continued on the next line"]}
    assert e["sections"][1]["title"] == "Fixed"
    assert entries[2]["sections"] == [{"title": "", "items": ["Plain line without a section."]}]


def test_view_marks_new_versions_and_leaves_out_future_ones(tmp_path, monkeypatch):
    p = tmp_path / "CHANGELOG.md"
    p.write_text(SAMPLE)
    monkeypatch.setattr(W, "__version__", "1.2.0")
    v = W.view("1.1.0", path=p)
    assert [e["version"] for e in v["entries"]] == ["1.2.0", "1.1.0"]
    assert v["unseen"] is True and v["entries"][0]["new"] and v["entries"][0]["current"] and not v["entries"][1]["new"]
    assert W.view("1.2.0", path=p)["unseen"] is False
    assert W.view("1.1.0", limit=1, path=p)["more"] is True


def test_the_shipped_changelog_names_the_running_version():
    assert W.entries(), "CHANGELOG.md must ship with the app"
    assert W.entries()[0]["version"] == __version__


# ------------------------------------------------------------------ API
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_DEMO", "0")
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    app = create_app(config_mod.load())
    with TestClient(app, base_url="http://lanloom.test") as c:
        c.data_dir = tmp_path
        c.st = app.state.ll
        code = (Path(tmp_path) / "setup-code.txt").read_text().strip()
        r = c.post("/api/setup/account", json={"code": code, "username": "admin", "password": PW}, headers=HDR)
        assert r.status_code == 200, r.text
        yield c


def test_settings_route_changes_channel_and_hold_and_is_audited(client):
    r = client.put("/api/update/settings", json={"channel": "preview", "hold": True}, headers=HDR)
    assert r.status_code == 200, r.text
    assert r.json()["channel"] == "preview" and r.json()["held"] == __version__
    assert client.put("/api/update/settings", json={"channel": "nightly"}, headers=HDR).json()["error"] == "update.err.channel"
    actions = [row["action"] for row in client.st.store.query("SELECT action FROM audit")]
    assert "update.settings" in actions


def test_rollback_route_asks_for_a_fresh_confirmation(client):
    up = client.st.updates
    up.dir.mkdir(parents=True, exist_ok=True)
    (up.dir / "sidecar.json").write_text(json.dumps({"version": CUR, "t": time.time(), "previous": PREV}))
    client.st.store.execute("UPDATE sessions SET stepup_until=0")
    r = client.post("/api/update/rollback", json={"version": PREV}, headers=HDR)
    assert r.status_code == 403 and r.json()["error"] == "auth.err.stepup"
    assert client.post("/api/account/stepup", json={"kind": "password", "password": PW}, headers=HDR).json()["ok"]
    r = client.post("/api/update/rollback", json={"version": PREV}, headers=HDR)
    assert r.status_code == 200, r.text
    assert json.loads((up.dir / "request.json").read_text())["rollback"] is True
    assert client.get("/api/update").json()["held"] == PREV


def test_whatsnew_route_and_seen_mark(client):
    v = client.get("/api/whatsnew").json()
    assert v["current"] == __version__ and v["entries"][0]["version"] == __version__
    assert v["unseen"] is False                       # a first look starts at the running version
    uid = client.st.accounts.by_name("admin")["id"]
    client.st.accounts.set_prefs(uid, {"whatsnew_seen": "1.0.0"})
    assert client.get("/api/whatsnew").json()["unseen"] is True
    r = client.post("/api/whatsnew/seen", json={"version": "99.0.0"}, headers=HDR).json()
    assert r == {"seen": __version__, "unseen": False}    # never ahead of what runs
    assert client.post("/api/whatsnew/seen", json={"version": "x"}, headers=HDR).json()["error"] == "update.err.invalid_version"


def test_the_image_build_context_keeps_the_changelog():
    root = Path(__file__).resolve().parent.parent
    lines = [ln.strip() for ln in (root / ".dockerignore").read_text().splitlines()]
    assert "!CHANGELOG.md" in lines and lines.index("!CHANGELOG.md") > lines.index("*.md")
    assert "CHANGELOG.md" in (root / "Dockerfile").read_text()
