"""The sample network as a built-in family: the reference for every adapter and the rehearsal target.

It reads and writes only an in-memory sample network (with a sample hypervisor and its guests); it never opens a
connection. Not offered in the integration
dialog (the firewall editor uses it on its own in simulation mode)."""
from .. import IntegrationManifest

CAPABILITIES = {
    "family": "simulation",
    "api": {"kind": "simulation", "version": "1"},
    "read": ["networks", "zones", "policies", "clients", "guests", "bridges", "firewall", "dhcp_reservations", "port_forwards",
             "ports", "port_profiles"],
    "write": {"move": "B", "client.set.name": "B", "client.set.fixed_ip": "B", "client.set.network": "B",
              "guest.nic": "B", "guest.fw_rule": "B", "guest.fw_rule_create": "B", "guest.fw_rule_delete": "B",
              "guest.firewall": "B*", "guest.power": "B*",
              "network.create": "B", "network.update.name": "B", "network.update.dhcp": "B", "network.delete": "B",
              "dhcp.reserve": "B", "dhcp.release": "B", "forward.create": "B*", "forward.update.name": "B",
              "forward.update.enabled": "B*", "forward.update.wan_port": "B*", "forward.update.dst_ip": "B*",
              "forward.update.dst_port": "B*", "forward.update.proto": "B*", "forward.delete": "B",
              "network.update.zone": "B", "zone.create": "B", "zone.update.name": "B", "zone.delete": "B",
              "policy.create": "B", "policy.update": "B", "policy.delete": "B", "policy.reorder": "B",
              "port.set.name": "B", "port.set.profile": "B", "port.set.native_network": "B", "port.set.tagged_mode": "B",
              "port.set.tagged": "B", "port.set.poe": "B*", "port.cycle": "B*"},
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
