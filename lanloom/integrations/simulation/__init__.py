"""The sample network as a built-in family: the reference for every adapter and the rehearsal target.

It reads and writes only an in-memory sample network; it never opens a connection. Not offered in the integration
dialog (the firewall editor uses it on its own in simulation mode)."""
from .. import IntegrationManifest

CAPABILITIES = {
    "family": "simulation",
    "api": {"kind": "simulation", "version": "1"},
    "read": ["networks", "zones", "clients"],
    "write": {"move": "B"},
    "rollback": {"device_native": False, "object_snapshot": True},
    "limits": {"objects_per_plan": 10},
    "lockout": ["mgmt_network", "self_path"],
}

MANIFEST = IntegrationManifest(
    family="simulation",
    label="Sample network",
    capabilities=CAPABILITIES,
    read="lanloom.integrations.simulation.read:SampleReader",
    write="lanloom.integrations.simulation.write:SampleWriter",
    selectable=False,
)
