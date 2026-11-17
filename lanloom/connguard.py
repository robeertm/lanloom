"""Connection guard of the executor: the process may connect only to the addresses it was set up for.

The executor holds the write credentials, so it must never reach anything but its controller and the Lanloom app -
not even when a bug in an adapter (or a wrong address in an options file) tried to. ``install`` hooks
``socket.socket.connect`` (and ``connect_ex``, which asyncio and some clients use) for the whole process:

* ``allowed``: the only addresses a connection may go to (the controller's and the app's, resolved once at start);
* ``denied``: addresses that are refused even when they also appear in ``allowed`` - a rehearsal executor that
  writes to a test twin lists the real controller here, so a run against the twin can never reach the real one.

Local sockets (a file path) are not network connections and stay possible. Name lookups happen in the C library and
are not affected; a name that later resolves to a different address is refused (fail closed). ``Blocked`` is raised
before the connection is attempted.
"""
import ipaddress
import socket

_ORIG_CONNECT = socket.socket.connect
_ORIG_CONNECT_EX = socket.socket.connect_ex
_STATE = {"on": False, "allowed": frozenset(), "denied": frozenset()}


class Blocked(ConnectionRefusedError):
    """A connection outside the guard's list was about to be made - refused, nothing was sent."""


def _addr_of(address):
    if isinstance(address, (str, bytes)):              # AF_UNIX path: not a network connection
        return None
    host = address[0] if isinstance(address, tuple) and address else address
    try:
        return ipaddress.ip_address(str(host).split("%", 1)[0])
    except ValueError:
        raise Blocked("guard: not an address: %r" % (host,)) from None


def check(address) -> None:
    """Raises Blocked when the guard is on and ``address`` (a socket address tuple) is not allowed."""
    if not _STATE["on"]:
        return
    ip = _addr_of(address)
    if ip is None:
        return
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    text = str(ip)
    if text in _STATE["denied"]:
        raise Blocked("guard: connections to this address are refused in this executor")
    if text not in _STATE["allowed"]:
        raise Blocked("guard: address not on the executor's list")


def _connect(self, address):
    check(address)
    return _ORIG_CONNECT(self, address)


def _connect_ex(self, address):
    check(address)
    return _ORIG_CONNECT_EX(self, address)


def resolve(hosts) -> set:
    """Every address the given host names or addresses stand for right now (unknown names give nothing)."""
    out = set()
    for host in hosts or ():
        host = str(host or "").strip().strip("[]")
        if not host:
            continue
        try:
            out.add(str(ipaddress.ip_address(host)))
            continue
        except ValueError:
            pass
        try:
            for info in socket.getaddrinfo(host, None):
                out.add(str(ipaddress.ip_address(info[4][0].split("%", 1)[0])))
        except (OSError, UnicodeError, ValueError):
            continue
    return out


def install(allowed, denied=()) -> dict:
    """Turn the guard on for this process. ``allowed``/``denied``: host names or addresses. Returns what is in force."""
    allow = frozenset(resolve(allowed))
    deny = frozenset(resolve(denied))
    _STATE.update(on=True, allowed=allow - deny, denied=deny)
    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex
    return {"allowed": len(_STATE["allowed"]), "denied": len(deny)}


def uninstall() -> None:
    _STATE.update(on=False, allowed=frozenset(), denied=frozenset())
    socket.socket.connect = _ORIG_CONNECT
    socket.socket.connect_ex = _ORIG_CONNECT_EX


def active() -> bool:
    return _STATE["on"]
