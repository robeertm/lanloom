"""Plan steps, version 2: every kind of change a plan may carry, with the exact fields it may touch.

A plan step names one object of the neutral model (see ``neutral.py``) and the values of the fields it changes,
``before`` (as read when the plan was built) and ``after``. The executor compares ``before`` with its own fresh
read and refuses on any difference. Anything not described here is refused: there is no free-form step.

Shapes::

    {"kind": "move", "device": id, "before": {"network", "ip"}, "after": {"network", "ip"}}   # since plan v1
    {"kind": "<area>.<verb>", "object": id, "before": {...}, "after": {...}}                    # plan v2

* ``*.create``: ``before`` is ``{}``, ``after`` carries every required field; ``object`` is a new id chosen by the
  app (``new:`` + hex), the adapter maps it to the vendor's id.
* ``*.delete``: ``after`` is ``{}``, ``before`` carries the full object as read (it is also the way back).
* every other verb: ``before`` and ``after`` carry the same field names, at least one value differs.

Plans keep the lowest version that can express them: a plan of moves only stays ``"v": 1`` (byte-identical to
earlier releases, so an executor that has not been updated yet still takes it); any other kind makes it
``"v": 2``. An executor refuses plan versions above ``PLAN_VERSION_MAX`` instead of half-understanding them.
"""
import ipaddress
import re

PLAN_VERSION_MAX = 2
MAX_OBJECTS = 10           # hard limit per plan, not configurable
NEW_ID = re.compile(r"\Anew:[0-9a-f]{8,32}\Z")
_ID = re.compile(r"\A[A-Za-z0-9:_./-]{1,96}\Z")
_TEXT_BAD = re.compile(r"[\x00-\x1f\x7f]")
_PORTS = re.compile(r"\A(?:\d{1,5}(?:-\d{1,5})?)(?:,\d{1,5}(?:-\d{1,5})?){0,31}\Z")


class FieldError(ValueError):
    pass


# ---------------------------------------------------------------------------- field checks (value -> ok or raise)
def _text(limit=64, empty=False):
    def check(v):
        if not isinstance(v, str) or len(v) > limit or _TEXT_BAD.search(v) or (not empty and not v.strip()):
            raise FieldError("text")
    return check


def _ident(v):
    if not isinstance(v, str) or not _ID.match(v):
        raise FieldError("id")


def _ident_or_empty(v):
    if v != "":
        _ident(v)


def _ip(v):
    try:
        ipaddress.ip_address(v if isinstance(v, str) else "")
    except ValueError:
        raise FieldError("ip")


def _ip_or_empty(v):
    if v != "":
        _ip(v)


def _cidr(v):
    try:
        ipaddress.ip_network(v if isinstance(v, str) else "", strict=False)
    except ValueError:
        raise FieldError("cidr")


def _bool(v):
    if not isinstance(v, bool):
        raise FieldError("bool")


def _int(lo, hi):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise FieldError("int")
    return check


def _vlan_or_none(v):
    if v is not None:
        _int(1, 4094)(v)


def _vlans(v):
    if not isinstance(v, list) or len(v) > 256 or len(set(v)) != len(v):
        raise FieldError("vlans")
    for x in v:
        _int(1, 4094)(x)


def _enum(*values):
    allowed = frozenset(values)

    def check(v):
        if v not in allowed:
            raise FieldError("enum")
    return check


def _ports(v):
    if v == "":
        return
    if not isinstance(v, str) or not _PORTS.match(v):
        raise FieldError("ports")
    for part in v.split(","):
        lo, _, hi = part.partition("-")
        if not (1 <= int(lo) <= 65535) or (hi and not (int(lo) <= int(hi) <= 65535)):
            raise FieldError("ports")


def _endpoint(v):
    """Policy side: {"zone": id, "net": id?, "group": id?} - nothing else."""
    if not isinstance(v, dict) or "zone" not in v or not set(v) <= {"zone", "net", "group"}:
        raise FieldError("endpoint")
    for x in v.values():
        _ident(x)


def _bands(v):
    if not isinstance(v, list) or not v or len(set(v)) != len(v) or not set(v) <= {"2.4ghz", "5ghz", "6ghz"}:
        raise FieldError("bands")


def _dhcp(v):
    """A network's address service in the shape of the neutral model: {"on": bool, "range": [first, last] | None}."""
    if not isinstance(v, dict) or set(v) - {"on", "range"} or not isinstance(v.get("on"), bool):
        raise FieldError("dhcp")
    rng = v.get("range")
    if rng is None:
        return
    if not isinstance(rng, list) or len(rng) != 2:
        raise FieldError("dhcp")
    for x in rng:
        _ip(x)
    if ipaddress.ip_address(rng[0]).version != ipaddress.ip_address(rng[1]).version or \
            ipaddress.ip_address(rng[0]) > ipaddress.ip_address(rng[1]):
        raise FieldError("dhcp")


NETWORK_FIELDS = {"name": _text(), "cidr": _cidr, "vlan": _vlan_or_none, "dhcp": _dhcp, "zone": _ident,
                  "purpose": _enum("lan", "guest", "iot", "vpn")}
ZONE_FIELDS = {"name": _text(), "internet": _bool, "discovery": _bool}
POLICY_FIELDS = {"name": _text(), "src": _endpoint, "dst": _endpoint, "action": _enum("allow", "block", "reject"),
                 "proto": _enum("any", "tcp", "udp", "tcp_udp", "icmp"), "ports": _ports, "return_traffic": _bool,
                 "enabled": _bool}
PORT_FIELDS = {"name": _text(empty=True), "profile": _ident_or_empty, "native_vlan": _vlan_or_none, "tagged": _vlans,
               "poe": _bool}
SSID_FIELDS = {"name": _text(32), "network": _ident, "bands": _bands, "security": _enum("open", "wpa2", "wpa3", "wpa2_wpa3"),
               "hidden": _bool, "enabled": _bool}
FORWARD_FIELDS = {"name": _text(), "proto": _enum("tcp", "udp", "tcp_udp"), "wan_port": _ports, "dst_ip": _ip,
                  "dst_port": _ports, "enabled": _bool}
_BRIDGE = re.compile(r"\A[A-Za-z][A-Za-z0-9_.-]{0,31}\Z")


def _bridge(v):
    """A hypervisor bridge or virtual network name (vmbr0, things): no spaces, no separators of a config line."""
    if not isinstance(v, str) or not _BRIDGE.match(v):
        raise FieldError("bridge")


_RULE_SOURCE = re.compile(r"\A(?:[0-9A-Fa-f:.,/+!-]|[A-Za-z0-9_])*\Z")


def _rule_source(v):
    """Source of a guest firewall rule: empty (any), addresses/networks (comma list), or a named alias/set (+name)."""
    if not isinstance(v, str) or len(v) > 64 or not _RULE_SOURCE.match(v):
        raise FieldError("source")
    if v:
        for part in v.split(","):
            if not part:
                raise FieldError("source")
            name = part[1:] if part.startswith("+") else None
            if name is not None:
                if not re.match(r"\A[A-Za-z][A-Za-z0-9_-]{0,31}\Z", name):
                    raise FieldError("source")
                continue
            try:
                ipaddress.ip_network(part.lstrip("!"), strict=False)
            except ValueError:
                raise FieldError("source")


GUEST_NIC_FIELDS = {"bridge": _bridge, "vlan": _vlan_or_none, "firewall": _bool}
GUEST_RULE_FIELDS = {"name": _text(64, empty=True), "direction": _enum("in", "out"), "action": _enum("allow", "block", "reject"),
                     "proto": _enum("any", "tcp", "udp", "icmp"), "ports": _ports, "source": _rule_source,
                     "enabled": _bool}


class Kind:
    """One step kind: which object list it acts on and which fields it may touch."""

    def __init__(self, name, area, fields, *, verb="set", required=(), inverse=None, confirm_fields=(), confirm=False):
        self.name, self.area, self.fields, self.verb = name, area, dict(fields), verb
        self.required = tuple(required)
        self.inverse = inverse or name
        self.confirm = confirm                      # every step of this kind needs its own confirmation (B*)
        self.confirm_fields = frozenset(confirm_fields)   # ... or only when one of these fields changes


def _k(name, area, fields, **kw):
    return name, Kind(name, area, fields, **kw)


# The complete list. Firmware, restarts, accounts, keys, remote access, VPN/WAN settings, port mirroring and factory
# resets are no step kind and never will be (see docs/INTEGRATIONS.md, "Hard limits").
KINDS = dict([
    _k("move", "devices", {"network": _ident, "ip": _ip_or_empty}),
    _k("client.set", "devices", {"name": _text(), "fixed_ip": _ip_or_empty, "network": _ident}),
    _k("client.block", "devices", {"blocked": _bool}, confirm=True),
    # a new network names its zone only where the family places it (a zone firewall); without zones it has none
    _k("network.create", "networks", NETWORK_FIELDS, verb="create", required=("name", "cidr"), inverse="network.delete"),
    _k("network.update", "networks", NETWORK_FIELDS),
    _k("network.delete", "networks", NETWORK_FIELDS, verb="delete", inverse="network.create"),
    _k("zone.create", "zones", ZONE_FIELDS, verb="create", required=("name",), inverse="zone.delete"),
    _k("zone.update", "zones", ZONE_FIELDS),
    _k("zone.delete", "zones", ZONE_FIELDS, verb="delete", inverse="zone.create"),
    _k("policy.create", "policies", POLICY_FIELDS, verb="create", required=("name", "src", "dst", "action"), inverse="policy.delete"),
    _k("policy.update", "policies", POLICY_FIELDS),
    _k("policy.delete", "policies", POLICY_FIELDS, verb="delete", inverse="policy.create"),
    _k("policy.reorder", "policies", {"order": _int(0, 100000)}),
    _k("port.set", "ports", PORT_FIELDS, confirm_fields=("poe",)),
    _k("ssid.create", "ssids", SSID_FIELDS, verb="create", required=("name", "network", "bands", "security"), inverse="ssid.delete"),
    _k("ssid.update", "ssids", SSID_FIELDS),
    _k("ssid.delete", "ssids", SSID_FIELDS, verb="delete", inverse="ssid.create"),
    _k("dhcp.reserve", "dhcp_reservations", {"client": _ident, "ip": _ip}, verb="create", required=("client", "ip"), inverse="dhcp.release"),
    _k("dhcp.release", "dhcp_reservations", {"client": _ident, "ip": _ip}, verb="delete", inverse="dhcp.reserve"),
    # a port forward opens a device to the internet: every new one, and every change other than its name, is confirmed
    _k("forward.create", "port_forwards", FORWARD_FIELDS, verb="create", required=("name", "proto", "wan_port", "dst_ip"),
       inverse="forward.delete", confirm=True),
    _k("forward.update", "port_forwards", FORWARD_FIELDS, confirm_fields=("enabled", "wan_port", "dst_ip", "dst_port", "proto")),
    _k("forward.delete", "port_forwards", FORWARD_FIELDS, verb="delete", inverse="forward.create"),
    _k("guest.power", "guests", {"status": _enum("running", "stopped")}, confirm=True),
    _k("guest.firewall", "guests", {"firewall": _bool}, confirm=True),
    _k("guest.nic", "guest_nics", GUEST_NIC_FIELDS),
    _k("guest.fw_rule", "guest_rules", GUEST_RULE_FIELDS),
    # a new guest rule names its guest (``guest``, the neutral guest id) - the only create whose object hangs below another
    _k("guest.fw_rule_create", "guest_rules", dict(GUEST_RULE_FIELDS, guest=_ident), verb="create",
       required=("guest", "direction", "action", "enabled"), inverse="guest.fw_rule_delete"),
    _k("guest.fw_rule_delete", "guest_rules", dict(GUEST_RULE_FIELDS, guest=_ident), verb="delete", inverse="guest.fw_rule_create"),
    _k("host.bridge", "bridges", {"vlan_aware": _bool, "ports": _vlans}),
])


def step_object(step: dict):
    """The id of the object a step acts on (``device`` for the v1 move, ``object`` for everything else)."""
    if not isinstance(step, dict):
        return None
    return step.get("device") if step.get("kind") == "move" else step.get("object")


def is_v1(step: dict) -> bool:
    return isinstance(step, dict) and step.get("kind") == "move" and set(step) == {"kind", "device", "before", "after"}


def plan_version(steps: list) -> int:
    return 1 if steps and all(is_v1(s) for s in steps) else 2


def changed_fields(step: dict) -> list:
    """Field names a step changes (create/delete: every field it carries)."""
    kind = KINDS.get(step.get("kind"))
    if kind is None:
        return []
    if kind.verb == "create":
        return sorted(step["after"])
    if kind.verb == "delete":
        return sorted(step["before"])
    return sorted(f for f in step["after"] if step["before"].get(f) != step["after"][f])


def check_step(step) -> None:
    """Shape and value check of one step. Raises FieldError (the caller turns it into a refusal)."""
    if not isinstance(step, dict):
        raise FieldError("step")
    kind = KINDS.get(step.get("kind"))
    if kind is None:
        raise FieldError("kind")
    key = "device" if kind.name == "move" else "object"
    if set(step) != {"kind", key, "before", "after"}:
        raise FieldError("shape")
    obj = step[key]
    if kind.verb == "create":
        if not isinstance(obj, str) or not NEW_ID.match(obj):
            raise FieldError("id")
    else:
        _ident(obj)
    before, after = step["before"], step["after"]
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise FieldError("shape")
    if kind.name == "move":           # the v1 shape, unchanged
        if set(before) != {"network", "ip"} or set(after) != {"network", "ip"} or after["ip"] == "":
            raise FieldError("shape")
    elif kind.verb == "create":
        if before or not after or set(after) - set(kind.fields) or set(kind.required) - set(after):
            raise FieldError("shape")
    elif kind.verb == "delete":
        if after or not before or set(before) - set(kind.fields):
            raise FieldError("shape")
    else:
        if not after or set(before) != set(after) or set(after) - set(kind.fields):
            raise FieldError("shape")
        if before == after:
            raise FieldError("unchanged")
    for side in (before, after):
        for field, value in side.items():
            kind.fields[field](value)


def needs_confirmation(step: dict) -> bool:
    kind = KINDS[step["kind"]]
    if kind.name == "forward.update" and (step.get("after") or {}).get("enabled") is False and \
            set(changed_fields(step)) <= {"enabled", "name"}:
        return False                          # switching a port forward off (and renaming it) never opens anything
    return kind.confirm or bool(kind.confirm_fields & set(changed_fields(step)))


def confirm_token(step: dict) -> str:
    return "confirm:%s:%s" % (step["kind"], step_object(step))


def inverse_step(step: dict, created_id: str = None) -> dict:
    """The step that undoes ``step``. A create is undone by deleting the object under the id the adapter gave it
    (``created_id``); a delete is undone by creating it again under a fresh ``new:`` id."""
    import hashlib
    kind = KINDS[step["kind"]]
    key = "device" if kind.name == "move" else "object"
    obj = step[key]
    if kind.verb == "create":
        obj = created_id or obj
    elif kind.verb == "delete":
        obj = "new:" + hashlib.sha256(obj.encode()).hexdigest()[:16]
    return {"kind": kind.inverse, key: obj, "before": dict(step["after"]), "after": dict(step["before"])}
