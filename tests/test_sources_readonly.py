"""Source checker: the live sources only ever send read operations, as the README promises.

The checks read the source code (AST), so a new write path fails here even before any behaviour test covers it:
* raw sends happen only at the known send sites,
* SNMP builds only Get, GetNext and GetBulk PDUs,
* MQTT builds only CONNECT, SUBSCRIBE, PINGREQ and DISCONNECT,
* the hub allow-list holds only read and subscribe message types,
* host networking only dumps the kernel socket table (netlink sock_diag),
* no source starts processes or loads native code."""
import ast
from pathlib import Path

from lanloom.sources import hostnet, hub, mqtt, snmp

SRC = Path(__file__).resolve().parent.parent / "lanloom" / "sources"
RAW_SEND = {"write", "send", "sendto", "sendall", "sendmsg", "send_text", "send_bytes", "writelines"}

# (file, enclosing function, method) of every raw send; each one is the single exit of an allow-listed path
SEND_SITES = {
    ("_ws.py", "WebSocket.connect", "write"),        # HTTP upgrade request of the WebSocket handshake
    ("_ws.py", "WebSocket._frame", "write"),         # frames; only called by the hub through Hub._send
    ("hub.py", "Hub._send", "send_text"),            # checks ALLOWED_TYPES / ALLOWED_FIELDS / ALLOWED_EVENTS
    ("mqtt.py", "_Wire.write", "write"),             # re-checks ALLOWED_PACKETS on the first byte
    ("mqtt.py", "Mqtt._connect", "write"),
    ("mqtt.py", "Mqtt._close", "write"),
    ("mqtt.py", "Mqtt._session", "write"),
    ("snmp.py", "SnmpClient._send", "send"),         # messages around build_pdu, which allows read PDUs only
    ("hostnet.py", "NetlinkReader.dump", "send"),    # sock_diag dump request
}


def tree(name):
    return ast.parse((SRC / name).read_text(encoding="utf-8"))


def calls(t):
    """(enclosing qualified name, call node) for every call."""
    out = []

    def walk(node, stack):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                walk(child, stack + [child.name])
                continue
            if isinstance(child, ast.Call):
                out.append((".".join(stack), child))
            walk(child, stack)

    walk(t, [])
    return out


def callee(call):
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None


def test_raw_sends_only_at_known_sites():
    found = set()
    for path in sorted(SRC.glob("*.py")):
        for where, call in calls(tree(path.name)):
            if callee(call) in RAW_SEND:
                found.add((path.name, where, callee(call)))
    assert found == SEND_SITES


def test_mqtt_writes_only_built_allowed_packets():
    assert mqtt.ALLOWED_PACKETS == frozenset({mqtt.CONNECT, mqtt.SUBSCRIBE, mqtt.PINGREQ, mqtt.DISCONNECT})
    allowed = {"connect_packet", "subscribe_packet", "pingreq_packet", "disconnect_packet"}
    for where, call in calls(tree("mqtt.py")):
        name = callee(call)
        if name == "build_packet":
            assert isinstance(call.args[0], ast.Name) and call.args[0].id in {"CONNECT", "SUBSCRIBE", "PINGREQ",
                                                                              "DISCONNECT"}, where
        if name == "write" and where.startswith("Mqtt."):
            arg = call.args[0]
            assert isinstance(arg, ast.Call) and callee(arg) in allowed, (where, ast.unparse(arg))


def test_snmp_builds_only_read_pdus():
    assert snmp._READ_PDUS == frozenset({0xA0, 0xA1, 0xA5})       # Get, GetNext, GetBulk
    names = {n.id for n in ast.walk(tree("snmp.py")) if isinstance(n, ast.Name)}
    assert not {n for n in names if "SET" in n.upper() and n.upper().startswith("PDU")}
    consts = {n.value for n in ast.walk(tree("snmp.py")) if isinstance(n, ast.Constant) and isinstance(n.value, int)}
    assert 0xA3 not in consts and 0xA4 not in consts and 0xA6 not in consts   # Set, Trap, Inform
    for where, call in calls(tree("snmp.py")):
        if callee(call) in ("build_pdu", "_request"):
            arg = call.args[0]
            assert isinstance(arg, ast.Name) and arg.id in {"PDU_GET", "PDU_GETNEXT", "PDU_GETBULK", "tag"}, where


def test_hub_allow_list_only_reads_and_subscribes():
    for t in hub.ALLOWED_TYPES:
        assert t in ("auth", "subscribe_events") or t.startswith("get_") or t.endswith("/list"), t
    assert hub.ALLOWED_EVENTS == frozenset({"state_changed"})
    seen = set()
    for where, call in calls(tree("hub.py")):
        if callee(call) in ("_send", "_request", "_registry") and len(call.args) >= 2:
            arg = call.args[1]                      # (conn, message type, **fields)
            if where in ("Hub._request", "Hub._registry") and isinstance(arg, ast.Name) and arg.id == "type_":
                continue                            # pass-through; its callers are checked here
            assert isinstance(arg, ast.Constant) and arg.value in hub.ALLOWED_TYPES, (where, ast.unparse(arg))
            seen.add(arg.value)
    assert "auth" in seen and "get_states" in seen


def test_hostnet_only_dumps_the_socket_table():
    assert hostnet.NETLINK_SOCK_DIAG == 4 and hostnet.SOCK_DIAG_BY_FAMILY == 20
    for where, call in calls(tree("hostnet.py")):
        if callee(call) == "send":
            assert ast.unparse(call.args[0]).startswith("diag_request("), where
    req = hostnet.diag_request(2, 1)
    assert int.from_bytes(req[4:6], "little") == hostnet.SOCK_DIAG_BY_FAMILY
    assert int.from_bytes(req[6:8], "little") & hostnet.NLM_F_DUMP == hostnet.NLM_F_DUMP


def test_no_processes_or_native_code():
    banned = {"subprocess", "ctypes", "cffi", "multiprocessing", "pty"}
    for path in sorted(SRC.glob("*.py")):
        t = tree(path.name)
        for node in ast.walk(t):
            if isinstance(node, ast.Import):
                assert not {a.name.split(".")[0] for a in node.names} & banned, path.name
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in banned, path.name
        for _, call in calls(t):
            f = call.func
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "os":
                assert f.attr not in ("system", "popen", "execv", "execve", "spawnv", "fork"), path.name


def test_checker_notices_a_new_write_path():
    bad = ast.parse("class Hub:\n    def turn_on(self, ws):\n        ws.send_text('{}')\n")
    sites = {("hub.py", w, callee(c)) for w, c in calls(bad) if callee(c) in RAW_SEND}
    assert sites and not sites <= SEND_SITES
