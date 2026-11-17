"""The sample network as a built-in family: the reference for every adapter and the rehearsal target.

It reads and writes only an in-memory sample network (with a sample hypervisor and its guests); it never opens a
connection. Not offered in the integration
dialog (the firewall editor uses it on its own in simulation mode)."""
from .. import IntegrationManifest

CAPABILITIES = {
    "family": "simulation",
    "api": {"kind": "simulation", "version": "1"},
    "read": ["networks", "zones", "clients", "guests", "bridges", "firewall"],
    "write": {"move": "B", "client.set.name": "B", "client.set.fixed_ip": "B", "client.set.network": "B",
              "guest.nic": "B", "guest.fw_rule": "B", "guest.fw_rule_create": "B", "guest.fw_rule_delete": "B",
              "guest.firewall": "B*", "guest.power": "B*"},
    "rollback": {"device_native": False, "object_snapshot": True},
    "limits": {"objects_per_plan": 10},
    "lockout": ["mgmt_network", "self_path", "own_guest"],
}

MANIFEST = IntegrationManifest(
    family="simulation",
    label="Sample network",
    capabilities=CAPABILITIES,
    read="lanloom.integrations.simulation.read:SampleReader",
    write="lanloom.integrations.simulation.write:SampleWriter",
    selectable=False,
)
