# Proxmox VE (local API): read adapter

Lanloom reads a Proxmox VE host or cluster over its local API with an **API token that can only read**. Nothing is ever changed on the host from Lanloom in this release; a token that could change anything is refused.

What you get:

- **Guests on the map:** every virtual machine and container hangs below the node it runs on (link *virtual*), with the bridge and VLAN of its network card. This uses the hardware addresses of the guests' network cards, so it is exact even when a switch learns all of them on the node's port. Manual entries still win (see [TOPOLOGY.md](../TOPOLOGY.md)).
- **Component card of a node:** version, cluster, processor and memory use, load, uptime; the guests on it (state, number, kind, VLAN, newest backup); storage with its fill level; bridges and bonds; findings.
- **Component card of a guest:** node, state, processor and memory, addresses (from the guest agent of a virtual machine, or the container's interfaces), network cards (bridge, VLAN tag or trunks, firewall flag, rate limit), newest backup (age, verified, part of a backup job, failed backups), traffic now and during the last hour, its firewall.
- **Firewall tab, live and read only:** the firewall at datacenter, node and guest level (options, rules in order, security groups, aliases, IP sets), all guest network cards in one table, bridges, bonds and the software-defined network (zones, VNets with tags and subnets).
- **Findings** in plain words: a node offline, storage almost full (90 %, 95 %), a running guest without any backup, a newest backup older than seven days, a failed backup task in the last 48 hours, a network card on a bridge that does not exist, a VLAN tag outside the VLANs a VLAN-aware bridge allows, a guest firewall that cannot work because the datacenter firewall is off, and - together with a controller adapter that reads switch ports - a guest VLAN that the node's switch port does not carry.
- **Night report:** the family contributes its own section (guests stopped or added since the last report, failed backups, findings); it appears only when this connection exists.

Never read or kept: passwords, keys and secrets of guests (cloud-init password, SSH keys), notes and descriptions, storage credentials. Guest and object ids become opaque ids; hardware addresses of network cards leave the adapter only as a hash and are used to match devices on the map.

## Create a read-only API token

The rule is a separate user with the built-in **PVEAuditor** role and a token with privilege separation.

1. **Datacenter → Permissions → Users → Add**: user `lanloom`, realm *Proxmox VE authentication server* (`lanloom@pve`). A password is not needed for the token.
2. **Datacenter → Permissions → API Tokens → Add**: user `lanloom@pve`, token ID `read`, keep **Privilege Separation** ticked. Copy the secret; it is shown only once.
3. **Datacenter → Permissions → Add → API Token Permission**: path `/`, token `lanloom@pve!read`, role **PVEAuditor**, *Propagate* ticked.

On the command line of a node the same is:

```
pveum user add lanloom@pve
pveum user token add lanloom@pve read --privsep 1
pveum acl modify / --tokens 'lanloom@pve!read' --roles PVEAuditor
```

Then in Lanloom: **Settings → Connections → + Proxmox VE (local API)**.

Lanloom checks the token's own permission list (`/access/permissions`) when you test or save the connection. If the token holds any privilege other than the audit ones (`*.Audit`, `Sys.Syslog`), it is **refused** ("This account can change settings"). Tokens of the superuser (`root@pam`) are always refused. Do not use your own administrator token.

Addresses of virtual machines come from the guest agent. Releases with the audit privilege for the guest agent (`VM.GuestAgent.Audit`) answer an auditor token; older releases ask for `VM.Monitor`, which can do more than read. Lanloom never asks for that: without it, the agent request is simply refused and the guest is matched by the hardware address of its network card instead (containers report their addresses either way).

## Settings

| field | meaning |
|---|---|
| Address | `https://<address of one node>:8006` (port 8006 is the default when you leave it out) |
| API token ID | `user@realm!name`, for example `lanloom@pve!read` |
| Token secret | the secret shown when the token was created; stored encrypted, never shown again |
| Accept a self-signed certificate | needed unless the node has a certificate your system trusts (a fresh installation uses its own) |

One connection reads the whole cluster through the node you enter. For a stand-alone host, enter that host.

## What Lanloom sends

Exactly one code path sends requests, through an allow-list that is checked before anything leaves the app: `GET` on the read endpoints listed in `lanloom/integrations/proxmox/read.py` (`GET_PATHS`) and nothing else. There is no sign-in; the token travels in the `Authorization` header. Nothing can start, stop, change or delete anything.

Cadence: cluster status and resources (guest state, load, traffic counters) every minute; everything else every five minutes, with a request budget per poll for very large clusters. A node that does not answer, a feature that is not installed (software-defined network, guest agent) or an area the token may not read is skipped; the rest is still read, and the capability list says what was missing.

## Releases

| release | reads |
|---|---|
| Proxmox VE 8.x and 9.x | everything above |
| Proxmox VE 7.x | everything above except container interfaces (container addresses come from their static configuration only) |

Tested versions are listed in [COMPATIBILITY.md](../COMPATIBILITY.md).

## Troubleshooting

| status | what to do |
|---|---|
| API token was rejected | token ID or secret wrong, or the token was deleted or has expired |
| This account can change settings | remove other roles from the token (and its user, when privilege separation is off); give it PVEAuditor on `/` only |
| Not reachable | check address and port 8006; the node must be reachable from the Lanloom host |
| TLS / certificate problem | tick *Accept a self-signed certificate* |
| Guests are missing on the map | only devices that are also in Lanloom's device list are placed (run a scan); a guest in another network needs that network in the scan |

---

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.
