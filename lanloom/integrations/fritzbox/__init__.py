"""FRITZ!Box family (home router, TR-064 and mesh topology): read half only (stage A).

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not
affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.

The read half talks to the box over TR-064 (``http://<box>:49000``, or ``https`` on port ``49443`` with the box's own
certificate) with an account made for Lanloom. TR-064 asks for HTTP digest authentication on every call; the transport
answers the challenge. The adapter can send exactly one kind of POST - a SOAP call that is a row of a fixed table of
``Get`` actions (see ``read.CALLS``) - and ``GET`` of the two documents those calls hand out: the host list (XML) and the
mesh topology (JSON). Anything else is refused before a byte leaves Lanloom: no ``Set``, no ``Add``, no ``Delete``, no
reconnect, no reboot, no change of any setting.

It reads which devices the box knows (name, address, wired or wireless, active now), where they hang in the mesh (which
repeater, which LAN port, which radio band), the box and its mesh devices with model, firmware and state, the home
network with its DHCP range and the state of the internet connection, and turns them into the neutral model
(equipment, end devices, a network, plain checks) plus the topology observation the core merges with its other sources.
"""
from .. import IntegrationManifest

READ_AREAS = ["inventory", "clients", "topology", "networks", "wans", "stats"]

CAPABILITIES = {
    "family": "fritzbox",
    "api": {"kind": "official-local", "version": ""},
    "read": list(READ_AREAS),
    "write": {},                       # stage A: reading only; port forwards and the guest network come after a test bench of their own
    "rollback": {"device_native": False, "object_snapshot": True},
    "limits": {"objects_per_plan": 10, "logins_per_hour": 60},
    "lockout": ["mgmt_network", "gateway_uplink", "self_path"],
}

# Plain settings and credentials of the read half (the integration dialog builds its form from these).
FORM = (
    {"k": "url", "type": "url", "ph": "http://192.168.178.1", "req": True},
    {"k": "username", "ph": "lanloom", "req": True},
    {"k": "password", "secret": True, "type": "password", "req": True},
    {"k": "allow_self_signed", "type": "checkbox"},
)

MANIFEST = IntegrationManifest(
    family="fritzbox",
    label="FRITZ!Box (local API)",
    capabilities=CAPABILITIES,
    read="lanloom.integrations.fritzbox.read:FritzboxReader",
    docs="docs/integrations/fritzbox.md",
    config_fields=("url", "username", "allow_self_signed"),
    secret_fields=("password",),
    form=FORM,
    report="lanloom.integrations.fritzbox.report:night_section",
    # identifiers: the web page title and the SSDP server/product string of the box
    detect=({"title": "fritz!box"}, {"ssdp": "fritz!box"}),
    prefill_scheme="http",
    guide="tr064",
)
