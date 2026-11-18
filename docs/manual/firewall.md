# Firewall

[Manual](README.md) · [Overview](overview.md) · [Map](map.md) · [Network](network.md) · [Devices](devices.md) · [Inventory](inventory.md) · Firewall · [Data paths](data-paths.md) · [Nightly report](nightly-reports.md) · [Automations](automations.md) · [Running](running.md) · [Updating](updating.md) · [Settings](settings.md) · [AI](ai.md)

## Purpose

Shows which zone and network every device sits in and what each zone may reach, read from your controller. In **Change**
mode you can draft a change (move a device to another network, give it a fixed address or a new name, start or shut
down a hypervisor guest), see in plain words what it would do, and apply it as one signed change. By default nothing is
written anywhere: the editor is a simulation.

> Read [FIREWALL-EDITOR.md](../FIREWALL-EDITOR.md) before you let it write to a real controller. Changes on a
> controller can interrupt connections, moving a wireless client can reconfigure every access point, and a wrong move
> can lock you out. Keep local console access. Use at your own risk.

## What you see

![The live configuration of a controller, read only](../screenshots/firewall-live-light.png)

- **Sources.** The simulation (a built-in sample network), the external executor (when set up) and, when a
  device-family adapter reads your controller, the **live configuration** - which is read only.
- **Zones** with their networks and the devices in them; a pin marks a fixed address.
- **Live configuration** (read only): the controller's real zones and networks, the clients in each network, the rules
  in the controller's order with filters and hit counters, a zone matrix, port forwarding, wireless networks and the
  network equipment with its ports. A zone firewall adapter adds plain checks (management open on the internet side,
  rules that let everything in) and a table of internet connections, VPN, fixed addresses, source NAT, static routes and
  rule groups. A hypervisor adapter adds findings, the firewall at datacenter, node and guest level, every guest
  network card and the bridges of each node.
- **Draft bar** (Change mode): one chip per device; **Preview** shows every field old to new, the warnings and what
  blocks.
- **Guests** card (Change mode, with a hypervisor behind the editor): start, shut down, move a network card, switch the
  guest firewall and its rules.

![Rules in the controller's order](../screenshots/firewall-rules-dark.png)

## How to use it

1. Switch to **Change**. Drag a device onto another network, or tap or click it to open its sheet: *Move to ...*,
   *Name*, *Fixed address*. Only what the connected controller can change is offered.
2. Collect changes in the draft bar and press **Preview**.
3. Read the preview; tick the marked points (private address, your own device, quiet hours) that need an explicit
   confirmation.
4. An admin applies the plan. In the simulation that is one click and nothing is written. With the external executor it
   is a passkey signature over the plan, then the executor checks, backs up, writes, watches and rolls back on its own
   if the network turns unhealthy. A successful change can be undone for 24 hours.

More:

- **On the phone** the sheet opens from the bottom; *Move to ...* leads to the list of networks.
- **From the search** (`Ctrl K`): *Change a device (network, name, fixed address) ...*, a device hit then offers *Move to
  ... / change*; *Change a guest (start, shut down, network card, firewall) ...* opens the *Guests* card.
- **Deep links:** `#/firewall/device/<address>` opens change mode with the sheet of that device,
  `#/firewall/pick` a searchable list of the devices that can be changed.
- **AI suggestions** (optional, operators): *AI suggestions* proposes moves the editor itself would allow; they join the
  draft only when you choose *Take over as draft* (see [AI](ai.md)).

![Preview of a change](../screenshots/firewall-preview-light.png)

## Limits

- **Checks.** A fixed address must lie inside the device's network, must not be the network's own address or its
  gateway and must not belong to another device. A device connected by cable cannot be moved by a client setting (its
  network follows the switch port), and a device whose network is unknown while it is offline can only be renamed.
- **The gate.** At most 10 objects per change; protected devices (gateway, network equipment, servers others depend on,
  the host that runs Lanloom) never move; management networks are no target; the state must still match the plan.
- **Guests.** Guests that keep the network running (name server, gateway, Lanloom itself, hub or controller, or tagged
  `lanloom-protect`) show a lock and the reason and offer no stop, card or rule changes. A stop is always a clean
  shutdown; creation, removal, migration, snapshots, backups and host networking are not part of any change.
- **What Lanloom never writes:** firmware, restarts, accounts, keys, remote access, VPN and WAN settings, port
  mirroring and factory resets.
- Real writes need a separate executor service, a built-in adapter for your controller family with write support
  (see [COMPATIBILITY.md](../COMPATIBILITY.md)), the phrase `LANLOOM_EXECUTOR_ARM=I-UNDERSTAND` and a file created by
  hand. All of this is off by default.

## Privacy

- **Read:** your controller or hypervisor through a built-in read-only adapter with a read-only account; the devices
  and networks Lanloom already knows.
- **Stored:** drafts in Lanloom's database (part of the backup). The executor keeps its own backups and a protocol
  without addresses or secrets. Read credentials are encrypted in the app; write credentials live only in the
  executor's options file.
- **Sent:** the app never writes to a network device. Only the optional executor does, to its controller, after a
  passkey signature. With AI suggestions on a cloud service, addresses and names are replaced by placeholders first
  (see [AI](ai.md#privacy)).
- **Roles:** viewers look. Operators prepare drafts and previews and undo drafts. Admins apply plans; with the external
  executor each change needs a passkey signature. Every draft, approval and undo is in the audit log, and changes on a
  real controller appear in the nightly report under *Changes written*.

## See also

- [FIREWALL-EDITOR.md](../FIREWALL-EDITOR.md) - modes, the gate, backup and rollback, turning on the executor
- [COMPATIBILITY.md](../COMPATIBILITY.md) - device families with read and write support
- [SECURITY-MODEL.md](../SECURITY-MODEL.md) - what the executor can and cannot do
- [COMPATIBILITY.md](../COMPATIBILITY.md) - the supported device families, each with its own guide
