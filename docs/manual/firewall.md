# Firewall

[Manual](README.md) · [Overview](overview.md) · [Map](map.md) · [Network](network.md) · [Devices](devices.md) · [Inventory](inventory.md) · [Vulnerabilities](vulnerabilities.md) · Firewall · [Data paths](data-paths.md) · [Nightly report](nightly-reports.md) · [Automations](automations.md) · [Running](running.md) · [Sites](sites.md) · [Updating](updating.md) · [Settings](settings.md) · [AI](ai.md)

## Purpose

Shows which zone and network every device sits in and what each zone may reach, read from your controller. In **Change**
mode you can draft a change (move a device to another network, give it a fixed address or a new name, create a network
with a VLAN of its own, change an address service, reserve or release a fixed address, add or switch off a port forward,
start or shut down a hypervisor guest; with a zone firewall also zones and rules), see in plain words what it would do, and apply it as one signed change. A
**network plan** keeps the target network of each device and shows how far the move has come. By default nothing is
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
- **Firewall board** (live configuration with rules, read only): three figures - *Blocked today* (what the rules that
  block or reject caught since the first reading of the day, with the rules that caught the most), *Own rules* and
  *Checks* - and the zones as pads with a line for every pair that may talk. A line is thin when the pair is allowed by
  the built-in defaults, thick when rules of your own decide it or it was busy in the last hour (with the number of
  hits), and red dashed when traffic of that pair was blocked in the last hour. Select a zone and then a second one, or a
  line, to see only the rules between those two zones; *Show as list* gives the same as a table.
- **May one reach the other?** Pick two places (the internet, a zone, a network or a device with an address), and if you
  like a protocol and a port. The answer comes from the rules as read, in their order: allowed, blocked or rejected, by
  which rule, written as a sentence. A rule that depends on something Lanloom cannot see (chosen devices, applications,
  regions, schedules, port groups) makes the answer **Depends** and is listed with the reason. Nothing is sent anywhere.
- **Rules in plain words.** Every rule has a sentence under its name (*Blocks everything in Guests from reaching everything
  in Internal*) and, where the controller counts, *N in the last hour* or *N blocked today*.
- **Checks with a suggestion.** Besides what the adapter finds (management open on the internet side, rules that let
  everything in), Lanloom points out a rule that an earlier rule always overrides, a guest zone that reaches the inside
  with every service, and an intrusion-detection suppression list that has grown long (more than 30 entries; only the
  number is read, never the entries). Each check says what you can do about it.
- **Draft bar** (Change mode): one chip per device; **Preview** shows every field old to new, the warnings and what
  blocks.
- **Guests** card (Change mode, with a hypervisor behind the editor): start, shut down, move a network card, switch the
  guest firewall and its rules.
- **Network plan**: the target network of each device (an existing one, or one still to come such as *IoT*), the
  progress (*5 of 12 moved*), one row per target with its devices, and a mark *→ IoT* on every device tile that still
  has to move. *Create this network* opens *New network* with the name filled in; *Draft N moves* puts the moves that
  are possible now into the draft. The plan is Lanloom's own note and works with a read-only connection too.
- **Networks**, **Fixed addresses** and **Port forwarding** cards (Change mode, when the executor can change the
  gateway): every network with VLAN, address service, zone and what uses it; *New network* from a template (IoT,
  Cameras, Work, LAN) with a free address range and VLAN filled in; reserve and release fixed addresses; add, change,
  switch on or off and delete port forwards.

![Network plan with progress and targets](../screenshots/netplan-light.png)

![Networks of the gateway with what uses them](../screenshots/networks-light.png)

![Rules in the controller's order](../screenshots/firewall-rules-dark.png)

![The firewall board: blocked today, own rules, checks and the zones with the traffic between them](../screenshots/firewall-board-light.png)

![May one reach the other? The answer names the deciding rule and the rules that cannot be judged](../screenshots/firewall-ask-dark.png)

![Checks with what you can do about them](../screenshots/firewall-checks-light.png)

![Switch front panels in the equipment card](../screenshots/switch-front-light.png)

The **mode switch** *Live | Plan | Firewall | Data paths* at the top of this page, of the [network](network.md) view and
of [Data paths](data-paths.md) moves between these views. From the search (`Ctrl K`): *May one reach the other?* opens the
question, *Switch ports and front view* the equipment card.

## How to use it

1. Switch to **Change**. Drag a device onto another network, or tap or click it to open its sheet: *Move to ...*,
   *Name*, *Fixed address*, and *Uses it by its address* (hub integrations, presence trackers, automations and Lanloom's
   own sources that reach the device by its address). Gateway changes start from the *Networks*, *Fixed addresses* and
   *Port forwarding* cards. Only what the connected controller can change is offered.
2. Collect changes in the draft bar and press **Preview**.
3. Read the preview; tick the marked points (private address, your own device, quiet hours, every new or changed
   port forward) that need an explicit confirmation.
4. An admin applies the plan. In the simulation that is one click and nothing is written. With the external executor it
   is a passkey signature over the plan, then the executor checks, backs up, writes, watches and rolls back on its own
   if the network turns unhealthy. A successful change can be undone for 24 hours.

More:

- **Plan a segmentation:** *Plan targets* on the *Network plan* card: choose a target network (or *A new network, by
  name …*), tick the devices, save. Once the network exists and a device can go there, *Draft N moves* drafts them -
  at most 10 objects per change, so a large plan moves in a few rounds.
- **On the phone** the sheet opens from the bottom; *Move to ...* leads to the list of networks.
- **From the search** (`Ctrl K`): *Change a device (network, name, fixed address) ...*, a device hit then offers *Move to
  ... / change*; *Change a guest (start, shut down, network card, firewall) ...* opens the *Guests* card; *Networks and
  VLANs*, *Port forwarding*, *Fixed addresses* and *Network plan* open their cards; *Zones of the firewall* and *New
  firewall rule* open the zones card and the rule dialog.
- **Deep links:** `#/firewall/device/<address>` opens change mode with the sheet of that device,
  `#/firewall/pick` a searchable list of the devices that can be changed, `#/firewall/networks`,
  `#/firewall/forwards`, `#/firewall/reservations`, `#/firewall/zones`, `#/firewall/rules/new`, `#/firewall/ask` and `#/firewall/plan` (`#/firewall/plan/edit` with the plan
  dialog open) their cards.
- **AI suggestions** (optional, operators): *AI suggestions* proposes moves the editor itself would allow; they join the
  draft only when you choose *Take over as draft* (see [AI](ai.md)).

![Preview of a change](../screenshots/firewall-preview-light.png)

![Preview of a new network, a port forward and a released fixed address](../screenshots/gateway-preview-dark.png)

### Zones

The **Zones** card (Change mode, when the executor can change the zone firewall) lists every zone with its networks and how many networks and rules refer to it. Built-in zones carry a *built-in* chip, zones Lanloom created *created by Lanloom*.

![The Zones card with networks per zone](../screenshots/zone-firewall-zones-light.png)

<img src="../screenshots/zone-firewall-zones-phone-light.png" width="45%" alt="The Zones card on a phone">

1. Press **New zone**, type a name, add it to the draft.
2. **Rename** works for zones that are not built in.
3. **Remove** is offered only for zones Lanloom created and only when no network and no rule is in them any more; otherwise the card says *empty it first*. The controller would delete the rules of a removed zone without a word, so Lanloom refuses.
4. To move a network, use the select **Move to zone ...** on its row: into a zone of its own or back into the internal zone. The management network and the system zones (external, gateway, VPN, hotspot, DMZ) are never offered.

On a phone every control is finger-sized and the zone select wraps below the row.

### Changing rules

In Change mode each changeable row of the rule list has an **on** checkbox, **Change**, up and down arrows and, for rules Lanloom created, **Remove**. **New rule** sits above the list; planned new rules appear above the list marked *planned*.

![The rule list with the tools in Change mode](../screenshots/zone-firewall-rules-dark.png)

1. **New rule** or **Change** opens a dialog: name, action (allow, block, reject), *From* zone and optionally one network, *To* zone and optionally one network, protocol (any, TCP, UDP, TCP and UDP), ports (a list or ranges, only with TCP or UDP) and *Allow answers back* (return traffic).
2. The arrows move a rule within the own rules of its zone pair - one move per pair and plan.
3. The **on** checkbox switches a rule off or on.
4. Press **Preview**. Every zone and rule change is shown field by field, old to new, with warnings which zone pairs open or close.

![A new rule in the dialog](../screenshots/zone-firewall-rule-new-light.png)

![Preview of zone and rule changes](../screenshots/zone-firewall-preview-dark.png)

A rule that lets traffic in from the internet or from another zone into the management zone needs its own confirmation tick.

![The confirmation of a rule that opens the management zone](../screenshots/zone-firewall-confirm-light.png)

Only rules whose every setting Lanloom can carry over are changeable; rules with schedules, connection-state filters, address, region, app or domain matches, source ports, an IPsec match or an ICMP type stay read-only, and built-in rules of the controller are never changed. Lanloom refuses a blocking rule that involves the management network, the management zone or the gateway, and a plan after which you, Lanloom or the executor could no longer reach the gateway or the controller; the refusal names the path and nothing is sent.

### May one reach the other? in the editor

The question card of the read firewall (see above) is also in the editor, below the rule list. It answers from the editor's rules - the simulation or the controller behind the executor - and, while you have a draft, shows the answer **after your draft** under the answer now. So you can check a new rule, a rule switched off, a new order or a network moved into another zone before you approve anything. Two places in the same zone are allowed when no rule decides. Nothing is sent anywhere; viewers may use it. Without a read firewall, *May one reach the other?* in the search (`Ctrl K`) opens this card.

![May one reach the other? in the editor: the answer now and after the draft](../screenshots/zone-firewall-ask-light.png)

<img src="../screenshots/zone-firewall-ask-phone-light.png" width="45%" alt="May one reach the other? in the editor, on a phone">

### Gateway firewalls with rules on interfaces

Some firewalls keep their rules on interfaces instead of between zones. Lanloom reads them the same way: every interface is a zone (its network, VLAN and address are the zone's network), interface groups are zones of their own, and the firewall itself is the zone *This firewall*. A rule on an interface becomes a rule from that interface's zone to the zone of its destination; the order is the firewall's evaluation order (floating rules first, then group rules, then interface rules, each by sequence; classic rules, where the firewall lists them, come last and carry a *built-in* chip). The board, the rule sentences, *May one reach the other?*, the checks and the night report section work as for a zone firewall.

With the executor set up for such a family, *Change* mode offers **New rule** between two interfaces (or to the firewall itself), **Change** and the **on** checkbox for simple rules (one interface, no address list, alias, schedule or policy routing) and **Remove** for rules Lanloom created. The arrows are not offered: a new rule is appended after the existing ones, so a new blocking rule takes effect only where no earlier rule already allows the same traffic - the preview and *May one reach the other?* after your draft show that before you approve. Rules towards the internet side are not written (they need address lists on the firewall).

The plan is written into the firewall's configuration first and put live with one apply at the end. Where the firewall's release has its own revert timer, the executor arms it before the apply and confirms only after it has read every changed rule back as planned; if the executor or its connection fails in between, the firewall puts the rules back by itself after 60 seconds. On releases without it, Lanloom's own backup is the way back. The adapter guide in [COMPATIBILITY.md](../COMPATIBILITY.md) lists the releases.

### Switch ports

When the editor's source can change switch ports (the simulation can; for real writes see [COMPATIBILITY.md](../COMPATIBILITY.md)), the editor has a **Switch ports** card. It shows every switch of the controller as its faceplate, the same front view as *Switch front panels* above. Pick a port to open its panel: link, untagged network with its VLAN, tagged networks, power over Ethernet, the devices behind it and the last 24 hours. In Change mode the panel has **Change port ...**; ports with a change in the draft carry an accent ring and the panel says *Planned*. Viewers see the faceplates without the button.

![The Switch ports card: the faceplate of a switch with port 7 selected and the button Change port](../screenshots/switch-ports-editor-light.png)

1. Pick a port and press **Change port ...**.
2. The sheet offers what the controller family can write, each field only when it can: *Name* (empty means the switch's own name for the port), *Port profile* (a named port setting of the controller, or *Own settings (no profile)*), *Untagged network* (devices plugged in without VLAN tags land there), *Tagged networks* and *Power over Ethernet*. With a profile chosen the profile decides the networks, and the power when it names one; network fields of the same draft are dropped.
3. *Tagged networks* is **None (untagged only)**, **All networks** or **A choice** with a checkbox per network. Only networks with a VLAN of their own are listed, and the untagged network cannot also be tagged.
4. *Power over Ethernet* can be switched on or off, only on ports that can deliver power. Ports in a passive or pass-through power mode are left alone.
5. **Power cycle** switches the power of the port off for a few seconds so that the device behind it restarts. It is a one-off command; there is nothing to undo.
6. Add it to the draft, then **Preview** and approve as for every other change (a passkey with the executor, a run in the simulation otherwise). Backup, watch and automatic rollback apply.

![The Change port sheet: name, profile, untagged network, tagged networks with checkboxes, power over Ethernet and power cycle](../screenshots/switch-port-dialog-light.png)

<img src="../screenshots/switch-ports-phone-light.png" width="45%" alt="The Switch ports card on a phone"> <img src="../screenshots/switch-port-dialog-phone-light.png" width="45%" alt="The Change port sheet on a phone">

The preview says in plain words what happens: devices on the port change network and get new addresses (they are named), a device keeps a fixed address of its old network, something that uses a device by its address (a hub integration, a tracker, an automation, a Lanloom source) is named, tagged networks change (devices that understand VLANs, such as access points or servers with virtual machines, see other networks), several devices share the port (a small switch behind it), a port is switched off (the devices that go off are named), a power cycle (the devices that restart are named), a rename.

![Preview of a power cycle, a port switched off and a network change, with warnings and confirmation checkboxes](../screenshots/switch-port-preview-dark.png)

**What is locked.** Lanloom refuses before anything is written, in the preview and again in the executor with its own fresh read:

- Only the **name** may change on the uplink of a switch, on the internet port of a gateway, on a port behind which network equipment hangs (a switch or access point, or a known neighbour), on a port where another device announces itself with LLDP, and on mirror or aggregation ports.
- A port that carries a device Lanloom protects (gateway, servers others depend on, hubs, the host that runs Lanloom), the device of the person who approves, the executor or the controller keeps its networks and its power. The panel shows a lock and names the reason: *only its name can change*.
- Only local networks can be used; never an internet uplink or a VPN network.

![A locked port: the note says that only its name can change](../screenshots/switch-ports-locked-dark.png)

**Confirmations.** Every change of power, every power cycle, and the management network as the untagged network of a port or newly tagged on it need their own confirmation tick in the preview. The last one says that whatever is plugged in reaches the admin pages of the network equipment.

### Wireless networks and blocked devices

When the editor's source can change wireless networks (the simulation can; for real writes see [COMPATIBILITY.md](../COMPATIBILITY.md)), the editor has two cards: **Wireless networks (n)**, every SSID with the network it carries, its bands, its security and the devices that use it, and **Blocked devices (n)**. In Change mode the first card has **New SSID** and a **Change...** button per SSID; the second has **Block a device...**. Viewers see both cards without the buttons.

![The Wireless networks card in Change mode](../screenshots/wireless-editor-light.png)

1. Press **New SSID**, or **Change...** on an SSID.
2. The dialog offers *Name (SSID)* (up to 32 characters), *Network* (the network whose devices join through it; only local networks are listed), *Bands* (2.4, 5 and 6 GHz; at least one), *Security* (open, WPA2 personal, WPA2/WPA3 personal or WPA3 personal) and *Hide the name*. An SSID can also be switched off and on from its row, and deleted when Lanloom created it.
3. Add it to the draft, then **Preview** and approve as for every other change (a passkey with the executor, a run in the simulation otherwise). Backup, watch and automatic rollback apply.

![The SSID dialog: name, network, bands, security and hidden](../screenshots/wireless-ssid-dialog-light.png)

<img src="../screenshots/wireless-phone-light.png" width="45%" alt="The Wireless networks card on a phone">

**Passwords.** Lanloom never handles a wireless password. It is not part of a plan, a preview, the history, a snapshot or a log. A new protected SSID (and an open one that becomes protected) gets a random password of 24 characters from the executor. Read it, show its QR code or set your own in the controller's own app.

**Block a device.** The *Blocked devices* card lists every blocked device with **Unblock**. **Block a device...** opens a picker (search by name, address or SSID); the device sheet has a block switch as well. A block cuts the device off at once and it cannot join again until it is unblocked; unblocking goes back on the way back (undo). A block and an unblock always need their own confirmation in the preview. A block is lasting: disconnecting a device (a kick) is never offered, because it simply comes back.

![The Block a device picker](../screenshots/wireless-block-dialog-dark.png)

The preview says in plain words what happens: how many devices lose the wireless network or have to join again, which devices change network and get new addresses, that a name is hidden, that a band is left, that an SSID has no password, that the management network goes on an SSID, that a password is generated, and that more than four SSIDs on one band cost airtime on every access point.

![Preview of a new SSID, a renamed one and a block, with warnings and confirmation checkboxes](../screenshots/wireless-preview-dark.png)

**What is refused.** Before anything is written, in the preview and again in the executor with its own fresh read:

- A change that drops clients (switching off, deleting, renaming, another network, a dropped band, another security) while a device Lanloom protects, the approver's device, the executor, the controller or the host that runs Lanloom is connected over that SSID. Hiding the name never drops anyone.
- An open SSID anywhere but on a guest network; on a guest network it needs its own confirmation. The management network on an SSID needs its own confirmation and is never open there.
- 6 GHz with anything but WPA3 personal.
- A name that another SSID already has; more than one change per SSID or device in a plan.
- Blocking a device Lanloom protects, the approver's own device, the executor, the controller or the host that runs Lanloom.
- Deleting an SSID Lanloom did not create.

**What is never offered.** Radio settings (channel, channel width, transmit power), enterprise security that signs devices in through a login server (RADIUS), the network or security of an SSID with private pre-shared keys per device, and passwords.

Command palette: *Change a wireless network* (`#/firewall/wireless`) and *Block a device* (`#/firewall/block`) open the cards in Change mode. A section *Wireless networks* of the nightly report names new, gone and renamed SSIDs, changed networks and security, an SSID that lost its password, and devices that were blocked or unblocked.

## Limits

- **Checks.** A fixed address must lie inside the device's network, must not be the network's own address or its
  gateway and must not belong to another device. A device connected by cable cannot be moved by a client setting (its
  network follows the switch port), and a device whose network is unknown while it is offline can only be renamed.
- **The gate.** At most 10 objects per change; protected devices (gateway, network equipment, servers others depend on,
  the host that runs Lanloom) never move; management networks are no target; the state must still match the plan.
- **Networks.** A new network is an internal one with a VLAN of its own (2 to 4094, not used by another network) and
  a private IPv4 range between /16 and /29 that overlaps no other network; its address service range lies inside it and
  leaves out the gateway address. A network that holds devices keeps its range and VLAN, and its address service stays
  on while devices take dynamic addresses there. Only networks Lanloom created and nothing uses any more can be
  deleted. The management network, internet uplinks, VPN and guest-portal networks are never changed.
- **Port forwards.** The target is a private address inside a local network, never the gateway, the controller or
  network equipment; an outside port is used by one forward only. Every new forward and every change other than its
  name needs its own confirmation; switching one off never does. Only forwards Lanloom created can be deleted.
- **Guests.** Guests that keep the network running (name server, gateway, Lanloom itself, hub or controller, or tagged
  `lanloom-protect`) show a lock and the reason and offer no stop, card or rule changes. A stop is always a clean
  shutdown; creation, removal, migration, snapshots, backups and host networking are not part of any change.
- **Switch ports.** One change per port and plan; a power cycle and new settings of the same port go into two plans.
  Ports in a passive or pass-through power mode keep their power setting, and a port whose setting the controller does
  not report cannot change it. Mirror and aggregation ports change only their name.
- **Wireless networks.** One change per SSID and per device and plan. A change that drops clients is refused while a protected
  device, the approver's device, the executor, the controller or Lanloom's host uses the SSID. Passwords are never part of
  a plan. A block and an unblock always need their own confirmation.
- **What Lanloom never writes:** firmware, restarts, accounts, keys, remote access, VPN and WAN settings, port
  mirroring and factory resets.
- Real writes need a separate executor service, a built-in adapter for your controller family with write support
  (see [COMPATIBILITY.md](../COMPATIBILITY.md)), the phrase `LANLOOM_EXECUTOR_ARM=I-UNDERSTAND` and a file created by
  hand. All of this is off by default.

## Privacy

- **Read:** your controller or hypervisor through a built-in read-only adapter with a read-only account; the devices
  and networks Lanloom already knows.
- **Stored:** drafts and the network plan (target per device, by its opaque id) in Lanloom's database (part of the
  backup). The executor keeps its own backups and a protocol
  without addresses or secrets. Read credentials are encrypted in the app; write credentials live only in the
  executor's options file.
- **Sent:** the app never writes to a network device. Only the optional executor does, to its controller, after a
  passkey signature. With AI suggestions on a cloud service, addresses and names are replaced by placeholders first
  (see [AI](ai.md#privacy)).
- **Roles:** viewers look. Operators prepare drafts and previews and undo drafts. Admins apply plans; with the external
  executor each change needs a passkey signature. Operators also keep the network plan. Every draft, plan change,
  approval and undo is in the audit log, and changes on a real controller appear in the nightly report under *Changes
  written*; the progress of the network plan has a section *Network plan* and the zone pairs that opened or closed a section *Zone matrix*, and changed port profiles, networks and power of switch ports a section *Port settings* (port and network names only, no addresses), and changed wireless networks and blocked devices a section *Wireless networks* (names only). Viewers may use *May one reach the other?*.

## See also

- [FIREWALL-EDITOR.md](../FIREWALL-EDITOR.md) - modes, the gate, backup and rollback, turning on the executor
- [COMPATIBILITY.md](../COMPATIBILITY.md) - device families with read and write support
- [SECURITY-MODEL.md](../SECURITY-MODEL.md) - what the executor can and cannot do
- [COMPATIBILITY.md](../COMPATIBILITY.md) - the supported device families, each with its own guide
