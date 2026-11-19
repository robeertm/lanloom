"""NUT source (lanloom/sources/nut.py): only the four read commands go out, replies are bounded, junk is dropped.

The server is a small asyncio server on 127.0.0.1 that writes down every line it receives."""
import asyncio

import pytest

from lanloom.sources import _common, nut
from lanloom.sources._common import SourceError
from lanloom.sources.nut import Nut

DEFAULT_VARS = {"ups.status": "OL CHRG", "battery.charge": "97", "battery.charge.low": "20", "battery.runtime": "1900", "ups.load": "31",
                "input.voltage": "231.4", "output.voltage": "230.0", "battery.voltage": "27,3", "ups.temperature": "29", "ups.realpower": "212",
                "ups.test.result": "Done and passed", "ups.model": "UPS-1500", "ups.serial": "SECRET-SERIAL-1", "device.serial": "SECRET-2",
                "driver.parameter.port": "/dev/ttyUSB0", "ups.firmware": "1.2"}


class FakeNut:
    """Scripted NUT server: ``units`` {name: (description, {var: value})}; ``replies`` overrides the answer to one command."""

    def __init__(self, units=None, replies=None):
        self.units = units if units is not None else {"office": ("Rack UPS", dict(DEFAULT_VARS))}
        self.replies = replies or {}
        self.received = []
        self.connections = 0
        self.server = None
        self.port = None

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    def answer(self, cmd):
        if cmd in self.replies:
            return self.replies[cmd]
        if cmd == "LIST UPS":
            body = "".join('UPS %s "%s"\n' % (n, d) for n, (d, _v) in self.units.items())
            return "BEGIN LIST UPS\n%sEND LIST UPS\n" % body
        if cmd.startswith("LIST VAR "):
            name = cmd[9:]
            if name not in self.units:
                return "ERR UNKNOWN-UPS\n"
            body = "".join('VAR %s %s "%s"\n' % (name, k, v) for k, v in self.units[name][1].items())
            return "BEGIN LIST VAR %s\n%sEND LIST VAR %s\n" % (name, body, name)
        if cmd == "VERSION":
            return "Network UPS Tools upsd 2.8.0\n"
        if cmd == "LOGOUT":
            return "OK Goodbye\n"
        return "ERR UNKNOWN-COMMAND\n"

    async def _handle(self, reader, writer):
        self.connections += 1
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                cmd = raw.decode("utf-8", "replace").rstrip("\r\n")
                self.received.append(cmd)
                reply = self.answer(cmd)
                hang_up = isinstance(reply, tuple)             # (text, True): say it and close the connection
                writer.write((reply[0] if hang_up else reply).encode())
                await writer.drain()
                if cmd == "LOGOUT" or hang_up:
                    break
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()


def go(coro):
    return asyncio.run(coro)


def make(srv, **kw):
    return Nut("127.0.0.1", srv.port, **kw)


# ---------------------------------------------------------------------------------------------------- what is sent
ALLOWED_RX = nut._ALLOWED


def test_only_the_four_read_commands_pass_the_allow_list():
    for ok in (b"LIST UPS\n", b"LIST VAR office\n", b"LIST VAR ups@host.1_x-y\n", b"VERSION\n", b"LOGOUT\n"):
        assert ALLOWED_RX.match(ok), ok
    for bad in (b"USERNAME admin\n", b"PASSWORD secret\n", b"LOGIN office\n", b"INSTCMD office beeper.off\n", b"SET VAR office ups.delay 1\n",
                b"FSD office\n", b"STARTTLS\n", b"LIST CMD office\n", b"LIST RW office\n", b"LIST CLIENT office\n", b"GET VAR office ups.status\n",
                b"LIST VAR \n", b"LIST VAR bad name\n", b"LIST VAR " + b"a" * 65 + b"\n", b"LIST UPS\r\n", b"LIST UPS", b"LIST UPS\nINSTCMD x y\n",
                b"LIST VAR office\nLOGOUT\n", b"list ups\n", b"LIST VAR o;ffice\n", b" LIST UPS\n", b""):
        assert not ALLOWED_RX.match(bad), bad


def test_a_command_outside_the_list_is_refused_before_anything_is_written():
    class W:
        data = b""

        def write(self, d):
            W.data += d

        async def drain(self):
            pass

    conn = nut._Conn(None, W())
    for cmd in ("INSTCMD office test.battery.start", "USERNAME x", "LIST VAR a b", "SET VAR a b c"):
        with pytest.raises(SourceError) as e:
            go(conn.ask(cmd, "x"))
        assert e.value.key == "source.nut.protocol"
    assert W.data == b""


def test_a_session_sends_nothing_but_the_allowed_lines():
    async def run():
        async with FakeNut({"office": ("Rack UPS", dict(DEFAULT_VARS)), "lab": ("Lab", {"ups.status": "OB"})}) as srv:
            ev = await make(srv).read_all()
            return srv.received, ev
    received, ev = go(run())
    assert received == ["LIST UPS", "LIST VAR office", "LIST VAR lab", "LOGOUT"]
    assert all(ALLOWED_RX.match((c + "\n").encode()) for c in received)
    assert [u["name"] for u in ev["units"]] == ["office", "lab"]


def test_a_named_ups_asks_only_for_that_one():
    async def run():
        async with FakeNut({"a": ("A", {"ups.status": "OL"}), "b": ("B", {"ups.status": "OB"})}) as srv:
            ev = await make(srv, ups="b").read_all()
            return srv.received, ev
    received, ev = go(run())
    assert received == ["LIST UPS", "LIST VAR b", "LOGOUT"] and [u["name"] for u in ev["units"]] == ["b"]


def test_no_credentials_ever_leave():
    async def run():
        async with FakeNut() as srv:
            await make(srv).read_all()
            return " ".join(srv.received).upper()
    text = go(run())
    for word in ("USERNAME", "PASSWORD", "LOGIN", "INSTCMD", "SET ", "STARTTLS"):
        assert word not in text


# ---------------------------------------------------------------------------------------------------- what is read
def test_neutral_reading_of_a_ups():
    async def run():
        async with FakeNut() as srv:
            return await make(srv).read_all()
    ev = go(run())
    assert ev["type"] == "nut" and ev["source"] == "nut" and ev["host"] == "127.0.0.1" and isinstance(ev["t"], float)
    u = ev["units"][0]
    assert u == {"name": "office", "description": "Rack UPS", "status": "OL CHRG", "charge": 97.0, "charge_low": 20.0, "runtime_s": 1900.0,
                 "runtime_low_s": None, "load": 31.0, "input_v": 231.4, "output_v": 230.0, "battery_v": 27.3, "temp": 29.0, "power_w": 212.0,
                 "test": "Done and passed", "model": "UPS-1500"}


def test_unknown_variables_never_reach_the_reading():
    async def run():
        async with FakeNut() as srv:
            return await make(srv).read_all()
    text = repr(go(run()))
    for junk in ("SECRET", "ttyUSB", "serial", "driver.parameter"):
        assert junk not in text


def test_parse_lines_keeps_known_names_only_and_unescapes():
    lines = ['VAR u ups.status "OL"', 'VAR u ups.serial "x"', 'VAR u ups.model "A \\"Big\\" one"', "garbage", 'VAR u battery.charge ""', 'VAR  broken',
             'VAR u ups.load "12" trailing', 'UPS u "x"']
    got = nut.parse_lines(lines)
    assert got == {"ups.status": "OL", "ups.model": 'A "Big" one', "battery.charge": ""}


def test_parse_lines_clips_values_and_caps_the_work():
    got = nut.parse_lines(['VAR u ups.model "%s"' % ("x" * 500)])
    assert len(got["ups.model"]) == 120
    assert nut.parse_lines(['VAR u ups.status "OL"'] * 5000) == {"ups.status": "OL"}


def test_numbers_and_junk_numbers():
    for text, val in (("12", 12.0), ("27,3", 27.3), ("-4", -4.0), ("1e2", 100.0), ("nan", None), ("inf", None), ("", None), (None, None), ("1e12", None), ("x", None)):
        assert nut.number(text) == val


def test_neutral_with_nothing_has_no_numbers_and_text_fields_are_empty():
    u = nut.neutral("x" * 100, None, {})
    assert len(u["name"]) == 64 and u["description"] == "" and u["status"] == "" and u["charge"] is None and u["model"] == ""
    assert nut.neutral("u", "d", {"ups.temperature": "0"})["temp"] == 0.0
    assert nut.neutral("u", "d", {"battery.temperature": "33"})["temp"] == 33.0


def test_more_than_sixteen_ups_are_not_followed():
    units = {"u%02d" % i: ("U", {"ups.status": "OL"}) for i in range(30)}

    async def run():
        async with FakeNut(units) as srv:
            ev = await make(srv).read_all()
            return ev, srv.received
    ev, received = go(run())
    assert len(ev["units"]) == nut.MAX_UPS and len(received) == nut.MAX_UPS + 2


def test_a_ups_name_the_server_makes_up_is_not_asked_for():
    async def run():
        async with FakeNut(replies={"LIST UPS": 'BEGIN LIST UPS\nUPS "bad name" "x"\nUPS ok "fine"\nUPS evil;rm "x"\nEND LIST UPS\n'},
                           units={"ok": ("fine", {"ups.status": "OL"})}) as srv:
            ev = await make(srv).read_all()
            return ev, srv.received
    ev, received = go(run())
    assert [u["name"] for u in ev["units"]] == ["ok"] and received == ["LIST UPS", "LIST VAR ok", "LOGOUT"]


# ---------------------------------------------------------------------------------------------------- bad servers
def read_error(srv_kwargs, **nut_kw):
    async def run():
        async with FakeNut(**srv_kwargs) as srv:
            try:
                await make(srv, **nut_kw).read_all()
            except SourceError as exc:
                return exc.key
            except Exception as exc:                      # not a SourceError: the caller maps it with error_key
                return _common.error_key("nut", exc)
        return None
    return go(run())


def test_err_answers_map_to_reason_keys():
    assert read_error({"replies": {"LIST UPS": "ERR ACCESS-DENIED\n"}}) == "source.nut.refused"
    assert read_error({"replies": {"LIST UPS": "ERR ACCESS-DENIED-WITH-LONG-TEXT secret details\n"}}) == "source.nut.refused"
    assert read_error({"replies": {"LIST UPS": "ERR UNKNOWN-COMMAND\n"}}) == "source.nut.protocol"
    assert read_error({"replies": {"LIST UPS": "ERR DATA-STALE\n"}}) == "source.nut.protocol"
    assert read_error({"replies": {"LIST VAR office": "ERR DRIVER-NOT-CONNECTED\n"}}) == "source.nut.protocol"


def test_the_error_text_of_the_server_never_becomes_the_exception_text():
    async def run():
        async with FakeNut(replies={"LIST UPS": "ERR ACCESS-DENIED leaky-detail-text\n"}) as srv:
            try:
                await make(srv).read_all()
            except SourceError as exc:
                return str(exc)
    assert "leaky-detail-text" not in go(run())


def test_a_server_without_ups_is_an_error_not_an_empty_success():
    assert read_error({"units": {}}) == "source.nut.no_ups"
    assert read_error({"units": {"a": ("A", {})}}, ups="zzz") == "source.nut.no_ups"


def test_a_line_that_is_too_long():
    assert read_error({"replies": {"LIST UPS": "BEGIN LIST UPS\nUPS a \"%s\"\nEND LIST UPS\n" % ("x" * 3000)}}) == "source.nut.protocol"


def test_a_line_without_an_end_is_bounded_by_the_stream_limit():
    junk = "BEGIN LIST UPS\n" + "x" * 300_000          # no newline at all
    assert read_error({"replies": {"LIST UPS": junk}}) == "source.nut.protocol"


def test_too_many_lines():
    body = "".join('VAR office ups.status "OL"\n' for _ in range(nut.MAX_VARS * 2 + 5))
    assert read_error({"replies": {"LIST VAR office": "BEGIN LIST VAR office\n%sEND LIST VAR office\n" % body}}) == "source.nut.protocol"


def test_connection_closed_in_the_middle():
    assert read_error({"replies": {"LIST UPS": ("BEGIN LIST UPS\n", True)}}) == "source.nut.closed"


def test_nothing_is_listening(monkeypatch):
    async def run():
        s = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = s.sockets[0].getsockname()[1]
        s.close()
        await s.wait_closed()
        try:
            await Nut("127.0.0.1", port).read_all()
        except Exception as exc:
            return _common.error_key("nut", exc)
    assert go(run()) == "source.nut.refused"


def test_a_silent_server_times_out(monkeypatch):
    monkeypatch.setattr(nut, "READ_TIMEOUT_S", 0.2)

    async def run():
        async def mute(r, w):
            await asyncio.sleep(2)
        srv = await asyncio.start_server(mute, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            await Nut("127.0.0.1", port).read_all()
        except Exception as exc:
            return _common.error_key("nut", exc)
        finally:
            srv.close()
    assert go(run()) == "source.nut.timeout"


# ---------------------------------------------------------------------------------------------------- host and port
@pytest.mark.parametrize("host", ["", None, "   ", "bad host", "a\x00b", "h" * 254])
def test_bad_host_is_refused(host):
    with pytest.raises(SourceError) as e:
        Nut(host)
    assert e.value.key == "source.nut.config"


@pytest.mark.parametrize("port", [-1, 65536, "abc", "99999", "0x10"])
def test_bad_port_is_refused(port):
    with pytest.raises(SourceError) as e:
        Nut("192.0.2.5", port)
    assert e.value.key == "source.nut.config"


def test_port_zero_or_empty_means_the_default():
    assert Nut("192.0.2.5", 0).port == 3493 and Nut("192.0.2.5", None).port == 3493 and Nut("192.0.2.5", "3494").port == 3494


@pytest.mark.parametrize("name", ["bad name", "a;b", "x" * 65, "ü"])
def test_bad_ups_name_is_refused(name):
    with pytest.raises(SourceError):
        Nut("192.0.2.5", ups=name)


def test_ok_names_hosts_and_the_poll_interval():
    n = Nut(" nas.example.org ", 3493, "ups@main.1", interval=1)
    assert n.host == "nas.example.org" and n.ups == "ups@main.1" and n.interval == 5.0
    assert repr(n) == "Nut(nas.example.org:3493)" and str(n) == repr(n)
    assert Nut("192.0.2.5", interval=60).interval == 60.0


# ---------------------------------------------------------------------------------------------------- run() and test()
async def collect(source, count, timeout=10.0):
    events = []
    done = asyncio.Event()

    def emit(ev):
        events.append(ev)
        if len(events) >= count:
            done.set()
    task = asyncio.create_task(source.run(emit))
    try:
        await asyncio.wait_for(done.wait(), timeout)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return events


def test_run_emits_status_then_readings():
    async def run():
        async with FakeNut() as srv:
            return await collect(make(srv), 2)
    events = go(run())
    assert events[0]["type"] == "nut" and events[0]["units"][0]["name"] == "office"
    assert events[1] == {"type": "status", "source": "nut", "ok": True, "key": "source.nut.ok", "t": events[1]["t"]}


def test_run_reports_a_failure_once_with_its_reason(monkeypatch):
    async def run():
        s = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = s.sockets[0].getsockname()[1]
        s.close()
        await s.wait_closed()
        return await collect(Nut("127.0.0.1", port), 1)
    events = go(run())
    assert events == [{"type": "status", "source": "nut", "ok": False, "key": "source.nut.refused", "t": events[0]["t"]}]


def test_run_a_refusing_server_says_so():
    async def run():
        async with FakeNut(replies={"LIST UPS": "ERR ACCESS-DENIED\n"}) as srv:
            return await collect(make(srv), 1)
    ev = go(run())[0]
    assert ev["ok"] is False and ev["key"] == "source.nut.refused"


def test_run_keeps_going_when_the_consumer_fails():
    async def run():
        async with FakeNut() as srv:
            seen = []

            def emit(ev):
                seen.append(ev["type"])
                raise RuntimeError("consumer bug")
            task = asyncio.create_task(make(srv).run(emit))
            for _ in range(100):
                await asyncio.sleep(0.05)
                if len(seen) >= 2:
                    break
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return seen
    assert go(run())[:2] == ["nut", "status"]


def test_test_reports_names_and_count():
    async def run():
        async with FakeNut({"a": ("A", {"ups.status": "OL"}), "b": ("B", {"ups.status": "OL"})}) as srv:
            return await make(srv).test()
    ok, key, details = go(run())
    assert ok is True and key == "source.nut.ok" and details == {"ups": 2, "names": ["a", "b"]}


def test_test_reports_the_reason_and_no_details():
    async def run():
        async with FakeNut(replies={"LIST UPS": "ERR ACCESS-DENIED\n"}) as srv:
            return await make(srv).test()
    assert go(run()) == (False, "source.nut.refused", {})

    async def gone():
        s = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = s.sockets[0].getsockname()[1]
        s.close()
        await s.wait_closed()
        return await Nut("127.0.0.1", port).test()
    assert go(gone()) == (False, "source.nut.refused", {})


def test_a_server_that_drips_its_lines_is_cut_off_after_the_session_limit(monkeypatch):
    monkeypatch.setattr(nut, "SESSION_TIMEOUT_S", 0.4)

    async def run():
        async def drip(r, w):
            await r.readline()
            w.write(b"BEGIN LIST UPS\n")
            for i in range(100):                                  # every single line is in time, the whole answer never is
                w.write(b'UPS u%d "x"\n' % i)
                await w.drain()
                await asyncio.sleep(0.1)
        srv = await asyncio.start_server(drip, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            return await collect(Nut("127.0.0.1", port), 1)
        finally:
            srv.close()
    ev = go(run())[0]
    assert ev["ok"] is False and ev["key"] == "source.nut.timeout"
