"""Proxmox VE family (local API), read half only (stage A).

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not
affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.

The read half talks to one node of a host or cluster (``https://<node>:8006``) with an API token that has the
built-in auditor role on ``/`` (token with privilege separation; see docs/integrations/proxmox.md). It reads nodes,
guests (virtual machines and containers) with their network cards, bridges and bonds, the software-defined network,
the firewall at datacenter, node and guest level, storage, backup jobs and the newest backup of every guest, and
the last hour of traffic per guest. It never writes: GET only, no sign-in (the token travels in a header).

Before a token is used its rights are checked (``/access/permissions``): a token that can change anything is
refused - a read-only token is the rule.
"""
from .. import IntegrationManifest

READ_AREAS = ["inventory", "topology", "guests", "bridges", "networks", "firewall", "storage", "backups", "stats"]

CAPABILITIES = {
    "family": "proxmox",
    "api": {"kind": "official-local", "version": ""},
    "read": list(READ_AREAS),
    "write": {},                       # stage A: reading only; guest power, NICs and guest rules come with stage B
    "rollback": {"device_native": False, "object_snapshot": True},
    "limits": {"objects_per_plan": 10},
    "lockout": ["own_guest", "node_bridge", "mgmt_network"],
}

# Plain settings and read-only credentials of the read half (the integration dialog builds its form from these).
FORM = (
    {"k": "url", "type": "url", "ph": "https://192.168.1.10:8006", "req": True},
    {"k": "token_id", "ph": "lanloom@pve!read", "req": True},
    {"k": "token_secret", "secret": True, "type": "password", "req": True},
    {"k": "allow_self_signed", "type": "checkbox"},
)

MANIFEST = IntegrationManifest(
    family="proxmox",
    label="Proxmox VE (local API)",
    capabilities=CAPABILITIES,
    read="lanloom.integrations.proxmox.read:ProxmoxReader",
    docs="docs/integrations/proxmox.md",
    config_fields=("url", "token_id", "allow_self_signed"),
    secret_fields=("token_secret",),
    form=FORM,
    report="lanloom.integrations.proxmox.report:night_section",
    detect=({"port": 8006}, {"port": 8006, "title": "proxmox"}),   # identifier: default API port and web page title
)
