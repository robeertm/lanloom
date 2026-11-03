"""Update notice and update requests on the app side (release feed, retry after errors, request file)."""
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lanloom import updates as U
from updater import engine as E


class MemStore:
    def __init__(self):
        self.settings = {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


def make(tmp_path, feed="https://feed.example/releases?per_page=30"):
    cfg = SimpleNamespace(data_dir=tmp_path, update_check=True, demo=False, release_feed=feed, image_repo="img")
    return U.Updates(cfg, MemStore())


def with_sidecar(up):
    up.dir.mkdir(parents=True, exist_ok=True)
    (up.dir / "sidecar.json").write_text(json.dumps({"version": "1", "t": time.time()}))
    return up


def write_status(up, **status):
    up.dir.mkdir(parents=True, exist_ok=True)
    (up.dir / "status.json").write_text(json.dumps(status))


# ------------------------------------------------------------------ request file
def test_terminal_states_match_the_engine():
    assert U.RUN_TERMINAL == E.TERMINAL


def test_concurrent_requests_never_fail_with_an_os_error(tmp_path):
    up = with_sidecar(make(tmp_path))
    results, errors = [], []
    start = threading.Barrier(40)

    def one():
        start.wait()
        try:
            results.append(up.request("1.2.3"))
        except ValueError as exc:
            results.append(str(exc))
        except Exception as exc:     # the bug: a shared temp file -> FileNotFoundError -> HTTP 500
            errors.append(repr(exc))

    threads = [threading.Thread(target=one) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    accepted = [r for r in results if isinstance(r, dict)]
    assert len(accepted) == 1 and results.count("update.err.busy") == 39
    req = json.loads((up.dir / "request.json").read_text())
    assert req["nonce"] == accepted[0]["nonce"]
    assert [p.name for p in up.dir.iterdir() if p.name.endswith(".tmp")] == []


def test_waiting_request_is_busy(tmp_path):
    up = with_sidecar(make(tmp_path))
    up.request("1.2.3")
    with pytest.raises(ValueError, match="update.err.busy"):
        up.request("1.2.3")


@pytest.mark.parametrize("state", ["pulling", "selftest", "probe", "backup", "swap", "health"])
def test_running_update_is_busy(tmp_path, state):
    up = with_sidecar(make(tmp_path))
    write_status(up, state=state, t=time.time() - 60)
    with pytest.raises(ValueError, match="update.err.busy"):
        up.request("1.2.3")


@pytest.mark.parametrize("state", ["idle", "done", "rolled_back", "failed"])
def test_terminal_state_accepts_a_request(tmp_path, state):
    up = with_sidecar(make(tmp_path))
    write_status(up, state=state, t=time.time() - 5)
    assert up.request("1.2.3")["version"] == "1.2.3"


def test_stuck_run_older_than_the_busy_window_does_not_block_forever(tmp_path):
    up = with_sidecar(make(tmp_path))
    write_status(up, state="swap", t=time.time() - U.BUSY_FOR - 5)
    assert up.request("1.2.3")["version"] == "1.2.3"


def test_busy_key_is_translated_and_known():
    root = Path(U.__file__).resolve().parent / "i18n"
    for lang in ("en", "de"):
        data = json.loads((root / f"{lang}.json").read_text(encoding="utf-8"))
        assert data.get("update.err.busy"), lang
        for key in E.MESSAGE_KEYS:
            assert data.get(key), (lang, key)
    assert "update.err.busy" in E.MESSAGE_KEYS


# ------------------------------------------------------------------ release feed
def release(tag, published, draft=False, prerelease=False, body="notes"):
    return {"tag_name": tag, "published_at": published, "draft": draft, "prerelease": prerelease, "body": body,
            "html_url": "https://releases.example/" + tag}


def test_highest_stable_version_wins_not_the_newest_date():
    # a backport published after the feature release must not hide it
    feed = [release("v1.0.5", "2026-03-02T00:00:00Z"), release("v1.2.0-rc.1", "2026-03-03T00:00:00Z", prerelease=True),
            release("v2.0.0", "2026-03-04T00:00:00Z", draft=True), release("v1.1.0", "2026-02-01T00:00:00Z"),
            release("not-a-version", "2026-03-05T00:00:00Z"), release("v1.0.4", "2026-01-01T00:00:00Z")]
    got = U.pick_release(feed)
    assert got["version"] == "1.1.0" and got["url"] == "https://releases.example/v1.1.0"


def test_pick_release_rejects_feeds_without_a_stable_release():
    with pytest.raises(ValueError):
        U.pick_release([release("v3.0.0", "x", draft=True), release("v3.0.0-beta", "x", prerelease=True)])
    with pytest.raises(ValueError):
        U.pick_release({"tag_name": "v1.0.0"})     # an object instead of a list


def test_pick_release_drops_non_https_links():
    assert U.pick_release([dict(release("1.0.0", "x"), html_url="http://plain.example")])["url"] == ""


def test_default_feed_lists_releases_instead_of_latest(monkeypatch):
    from lanloom import config as config_mod
    monkeypatch.delenv("LANLOOM_RELEASE_FEED", raising=False)
    feed = config_mod.load().release_feed
    assert feed.endswith("/releases?per_page=30") and "/latest" not in feed


def test_fetch_reads_the_list_feed(tmp_path, monkeypatch):
    up = make(tmp_path)
    body = json.dumps([release("v1.0.9", "2026-05-01T00:00:00Z"), release("v1.3.0", "2026-04-01T00:00:00Z")]).encode()

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            assert n == 512 * 1024      # the response stays bounded
            return body

    seen = {}
    monkeypatch.setattr(U.urllib.request, "urlopen", lambda req, timeout, context: seen.setdefault("url", req.full_url) and Resp())
    assert up._fetch()["version"] == "1.3.0"
    assert seen["url"] == "https://feed.example/releases?per_page=30"


# ------------------------------------------------------------------ retry after a network error
class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def run(coro):
    import asyncio
    return asyncio.run(coro)


def test_network_error_is_retried_after_15_minutes_not_6_hours(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(U.time, "time", clock)
    up = make(tmp_path)
    calls = []

    def failing():
        calls.append(clock.t)
        raise OSError("dns not ready")

    monkeypatch.setattr(up, "_fetch", failing)
    assert run(up.check())["error"] == "OSError"
    clock.t += 14 * 60
    run(up.check())
    assert len(calls) == 1                      # no hammering right after an error
    clock.t += 2 * 60
    monkeypatch.setattr(up, "_fetch", lambda: calls.append(clock.t) or {"version": "9.0.0", "notes": "", "url": ""})
    state = run(up.check())
    assert len(calls) == 2 and state["error"] is None and state["latest"] == "9.0.0"
    clock.t += U.CHECK_EVERY - 60
    run(up.check())
    assert len(calls) == 2                      # a successful check still waits the full interval
    clock.t += 120
    run(up.check())
    assert len(calls) == 3


def test_selftest_prints_the_line_the_updater_requires(capsys):
    from lanloom import __version__
    from lanloom.main import selftest
    assert selftest() == 0
    out = [ln.strip() for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert out[-1] == "selftest: ok %s" % __version__
