"""Write load (B9): nothing is written that is already stored, periodic writers use one transaction and only changed
rows, "last seen" reaches the database at most every five minutes, and stopping writes everything that is pending.

Counted with ``Store.changes()`` (rows changed through the connection), the same counter the views use as their cache key.
"""
import pytest
from fastapi.testclient import TestClient

from lanloom import agents as agents_mod
from lanloom import config as config_mod
from lanloom.main import create_app
from lanloom.store import Store


# ---------------------------------------------------------------------- settings
def test_same_value_writes_nothing(tmp_path):
    st = Store(str(tmp_path / "a.db"))
    assert st.set_setting("k", {"a": 1, "b": [1, 2]}) is True
    before = st.changes()
    for _ in range(100):
        assert st.set_setting("k", {"a": 1, "b": [1, 2]}) is False
    assert st.changes() == before and st.skipped_writes == 100
    assert st.set_setting("k", {"a": 2}) is True                   # a real change is written at once
    assert st.changes() == before + 1 and st.get_setting("k") == {"a": 2}
    st.close()


def test_a_read_value_is_known_as_stored(tmp_path):
    path = str(tmp_path / "a.db")
    st = Store(path)
    st.set_setting("k", [1, 2, 3])
    st.close()
    st = Store(path)                                                # after a restart nothing is known ...
    assert st.get_setting("k") == [1, 2, 3]                        # ... until the value was read once
    before = st.changes()
    assert st.set_setting("k", [1, 2, 3]) is False and st.changes() == before
    st.close()


def test_direct_writes_to_the_table_drop_the_checksums(tmp_path):
    st = Store(str(tmp_path / "a.db"))
    st.set_setting("k", 1)
    st.execute("DELETE FROM settings WHERE key='k'")
    assert st.set_setting("k", 1) is True and st.get_setting("k") == 1
    st.execute("UPDATE settings SET value='5' WHERE key='k'")
    assert st.set_setting("k", 1) is True and st.get_setting("k") == 1
    st.close()


def test_a_rolled_back_transaction_drops_the_checksums(tmp_path):
    st = Store(str(tmp_path / "a.db"))
    st.set_setting("k", 1)
    with pytest.raises(RuntimeError):
        with st.transaction():
            st.set_setting("k", 2)
            raise RuntimeError("stop")
    assert st.get_setting("k") == 1
    assert st.set_setting("k", 2) is True and st.get_setting("k") == 2
    st.close()


def test_a_second_connection_is_noticed(tmp_path):
    path = str(tmp_path / "a.db")
    a, b = Store(path), Store(path)
    a.set_setting("k", 1)
    b.set_setting("k", 9)                                           # another connection on the same file
    assert a.set_setting("k", 1) is True and b.get_setting("k") == 1
    a.close()
    b.close()


# ---------------------------------------------------------------------- host agents
@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("LANLOOM_DATA", str(tmp_path))
    monkeypatch.setenv("LANLOOM_UPDATE_CHECK", "0")
    application = create_app(config_mod.load())
    with TestClient(application, base_url="http://lanloom.test") as c:
        c.st = application.state.ll
        c.tmp = tmp_path
        yield c


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def _report(t, load=0.5, tx=10, failed=()):
    return {"t": t, "interval": 10, "flows": [],
            "host": {"name": "web-host", "ips": ["192.168.50.40"], "macs": ["02:4c:4c:00:00:40"], "cpus": 2},
            "system": {"load": [load, 0.4, 0.3], "mem_total": 1000, "mem_avail": 400, "disks": [{"mount": "/", "total": 100, "free": 5}]},
            "units": {"available": True, "failed": [{"name": n, "since": 1, "result": "exit-code"} for n in failed], "timers": []},
            "containers": {"available": True, "runtime": "docker", "items": [
                {"id": "a" * 12, "name": "web", "image": "example/web:1", "state": "running", "created": 1, "health": "healthy",
                 "tx": tx, "rx": 2 * tx}]}}


def _hub_with_agent(app, t0):
    hub = app.st.core.agents
    hub.clock = Clock(t0)
    app.st.core.store.execute("INSERT INTO agent_hosts(id, kind, name, created) VALUES('ag1', 'agent', 'web host', ?)", (t0,))
    hub.reload()
    return hub


def test_a_quiet_agent_writes_little_over_an_hour(app):
    t0 = 1_800_000_000.0
    hub = _hub_with_agent(app, t0)
    store = app.st.core.store
    states, seen_w, facts_w = [], [], []
    hub.ingest_report("ag1", _report(t0 - 10), t0 - 10, t0 - 10)     # first report: facts and device are written once
    store._db.set_trace_callback(lambda s: (states.append(s) if "SET state=" in s else None,
                                            seen_w.append(s) if "SET last_seen=" in s else None,
                                            facts_w.append(s) if "SET facts=" in s else None))
    for i in range(360):                                            # one hour, a report every 10 s, a save every minute
        t = t0 + 10 * i
        hub.clock.t = t
        hub.ingest_report("ag1", _report(t, load=0.3 + (i % 7) / 10, tx=10 * i), t, t)
        hub.touch("ag1", t, "1.0.0")
        if i % 6 == 5:
            hub.save()
    store._db.set_trace_callback(None)
    # the state moved only in live numbers: at most every five minutes (plus the first one)
    assert len(states) <= 13, len(states)
    # "last seen": first contact, then at most every five minutes
    assert len(seen_w) <= 13, len(seen_w)
    # the exact time lives in memory and is what the views read
    assert hub._row("ag1")["last_seen"] == t0 + 3590
    assert hub.entries()[0]["online"] is True
    assert not facts_w                                              # the host facts did not change


def test_a_real_change_is_written_at_the_next_save(app):
    t0 = 1_800_000_000.0
    hub = _hub_with_agent(app, t0)
    store = app.st.core.store
    hub.ingest_report("ag1", _report(t0), t0, t0)
    hub.save()
    hub.clock.t = t0 + 60
    hub.ingest_report("ag1", _report(t0 + 60, failed=("backup.service",)), t0 + 60, t0 + 60)
    before = store.changes()
    assert hub.save() >= 1 and store.changes() > before
    raw = store.one("SELECT state FROM agent_hosts WHERE id='ag1'")["state"]
    assert "backup.service" in raw
    # nothing new: nothing written
    before = store.changes()
    assert hub.save() == 0 and store.changes() == before


def test_facts_and_device_are_written_only_when_they_change(app):
    t0 = 1_800_000_000.0
    hub = _hub_with_agent(app, t0)
    store = app.st.core.store
    seen = []
    store._db.set_trace_callback(lambda s: seen.append(s) if s.lstrip().startswith("UPDATE agent_hosts SET facts") else None)
    for i in range(30):
        hub.ingest_report("ag1", _report(t0 + 10 * i), t0 + 10 * i, t0 + 10 * i)
    store._db.set_trace_callback(None)
    assert len(seen) == 1
    rep = _report(t0 + 400)
    rep["host"]["cpus"] = 4
    store._db.set_trace_callback(lambda s: seen.append(s) if s.lstrip().startswith("UPDATE agent_hosts SET facts") else None)
    hub.ingest_report("ag1", rep, t0 + 400, t0 + 400)
    store._db.set_trace_callback(None)
    assert len(seen) == 2


def test_stopping_writes_everything_pending_and_a_restart_loses_nothing(app):
    t0 = 1_800_000_000.0
    hub = _hub_with_agent(app, t0)
    store = app.st.core.store
    hub.ingest_report("ag1", _report(t0), t0, t0)
    hub.touch("ag1", t0, "1.0.0")
    hub.save()
    t1 = t0 + 70
    hub.clock.t = t1
    hub.ingest_report("ag1", _report(t1, load=2.5), t1, t1)
    hub.touch("ag1", t1)
    assert store.one("SELECT last_seen FROM agent_hosts WHERE id='ag1'")["last_seen"] == t0   # still the stored one
    hub.save(force=True)
    row = store.one("SELECT last_seen, state FROM agent_hosts WHERE id='ag1'")
    assert row["last_seen"] == t1 and '"load":[2.5' in row["state"]
    # a fresh hub on the same store (a restart) sees the same
    fresh = agents_mod.AgentHub(app.st.core, clock=Clock(t1))
    assert fresh._row("ag1")["last_seen"] == t1 and fresh.live["ag1"]["system"]["load"][0] == 2.5


def test_core_stop_saves_agents_and_energy(app):
    core = app.st.core
    calls = []
    real_save, real_flush = core.agents.save, core.energy.flush
    core.agents.save = lambda force=False: calls.append(("agents", force)) or real_save(force)
    core.energy.flush = lambda now=None: calls.append(("energy", None)) or real_flush(now)
    import asyncio
    asyncio.run(core.stop())
    assert ("agents", True) in calls and ("energy", None) in calls


def test_energy_flush_writes_the_unsaved_day(tmp_path):
    from lanloom.energy import DAYS_SETTING

    class Core:
        pass

    core = Core()
    core.store = Store(str(tmp_path / "a.db"))
    from lanloom.energy import EnergyBook
    book = EnergyBook.__new__(EnergyBook)
    import threading
    book.core, book._lock, book._days, book._saved_t, book.clock = core, threading.Lock(), {"2026-11-23": {"kwh": 1.5}}, 0.0, lambda: 1000.0
    book.flush()
    assert core.store.get_setting(DAYS_SETTING) == {"2026-11-23": {"kwh": 1.5}}
    before = core.store.changes()
    book.flush()                                                     # unchanged: nothing written
    assert core.store.changes() == before
