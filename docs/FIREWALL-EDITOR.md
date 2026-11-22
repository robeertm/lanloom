# Firewall editor

The **Firewall** tab shows which zone and network every device sits in and what each zone may reach. In **Change** mode you can move a device to another network, give it a fixed address or a new name, start or shut down a guest of a hypervisor, move its network card to another bridge or VLAN and adjust its firewall, see in plain words what that means, and apply it as one signed change.

> ## ⚠️ Read this before you let it write to a real controller
>
> - **Out of the box nothing is written anywhere.** The editor runs in **simulation** mode against a built-in sample network. Real changes need a separate executor service, a built-in adapter for your controller family with write support (see [COMPATIBILITY.md](COMPATIBILITY.md)), and an explicit arming step on the host. All three are off by default.
> - **Moving a wireless client can reconfigure every access point.** Many controllers re-provision all access points when a client's network assignment changes. The wireless network can be gone for 2 to 4 minutes in the whole building. The preview says so; plan changes for a quiet moment and set `LANLOOM_FW_QUIET_HOURS`.
> - **You can lock yourself out.** A device that loses its internet or management access can cut off the very path you need to undo the change. The gate refuses moves of protected devices, moves into management networks and moves that take away the internet of the device you are using, and it keeps a local backup so the executor can roll back on its own. It cannot know every path in your network. Keep local console access to your controller.
> - **Private (changing) hardware addresses defeat per-device rules.** Phones and tablets often use a random address per network. A move that is tied to the address does not last. The preview asks you to switch that off on the device first.
> - **Things that use a device by its address break.** Automations, printers set up by address and similar links have to be updated. The preview lists known dependencies, but only those the controller state reports.
> - **A new fixed address takes effect later, a removed one can change the address.** A device picks up a fixed address when it renews its address (reconnect or restart it); without a fixed address it may get a different one later, and whatever reaches it by address loses it.
> - **Stopping a guest interrupts its services.** Lanloom shuts a guest down cleanly and never pulls the plug, but whatever the guest serves is gone until it starts again. Moving a guest's network card to another bridge or VLAN cuts its connection until it has an address in the new network. Guests that keep the network running (Lanloom itself, the executor, the name server, the gateway, a hub or controller) are refused - but Lanloom only knows what it can see. Tag guests that must never be touched with `lanloom-protect`.
> - **The executor holds controller credentials.** Use a dedicated local account with the least rights your controller offers, never an owner account, and keep its options file readable only by the executor. The executor can connect only to the app and to its controller (connection guard).
> - **Use at your own risk.** Keep local console access to your controller; Lanloom never writes without your passkey. This is no legal advice and no warranty.

## Modes

| | simulation (default) | external |
|---|---|---|
| Where plans run | inside the app, against a sample network in memory | in the separate executor service |
| What it writes | nothing | only the plan steps the adapter's capability list allows, after the gate |
| Approval | one click, nothing is written | passkey signature over the plan hash (user verification required) |
| Watch time after a change | 20 s (`LANLOOM_FW_SIM_WATCH_S`) | 60 min (`LANLOOM_EXECUTOR_WATCH_S`) |

The app itself never writes to a network device in either mode. It keeps the draft, computes the preview and builds the plan.

**AI suggestions (optional).** With an AI set up, *AI suggestions* in the editor proposes moves – only those the editor itself would allow. Asking never touches the draft: the suggestions you pick join it only when you choose *Take over as draft*, and from there the same preview, confirmation and signature apply. See [integrations/ai.md](integrations/ai.md#suggestions-for-networks-and-the-firewall).

### Live configuration (read only)

When a device-family adapter reads your controller (see [COMPATIBILITY.md](COMPATIBILITY.md)), the Firewall tab offers it as a further source next to the simulation or the executor, and opens it by default while the editor runs the simulation. It shows the controller's real zones and networks, the clients in each network, the firewall rules in the controller's order with their filters and hit counters, a zone matrix (what applies to whole zones; a number counts your own rules that narrow it), port forwarding, wireless networks and the network equipment with its ports. This view is **read only**: there is no change mode, no draft and no plan. The switch at the top returns to the simulation, which stays available for practice.

A zone firewall adapter appears the same way as a controller: zone matrix, rules in the firewall's order (one row per source and destination zone of a rule, with addresses, services, schedule and user rules), port forwarding, plain checks (management open on the internet side, rules that let everything in) and a table of internet connections, VPN connections, fixed addresses, source NAT, static routes and rule groups. Rules for every zone are grouped under *any zone*. It is read only as well.

A hypervisor adapter appears the same way. Its view has no zone board; instead it lists findings, the firewall at datacenter, node and guest level (options, rules in order, security groups, aliases, IP sets), every guest network card with bridge, VLAN and firewall flag, and the bridges, bonds and software-defined network of each node. It is read only as well.

How the matrix is read: for each pair of zones Lanloom walks the enabled rules in the controller's order and takes the first one that covers the whole zone pair (no network, address, port, protocol, connection-state or schedule filter). Narrower rules are listed below the matrix with their filters; they decide for the traffic they match.

### Changing a device: network, fixed address, name

In **Change** mode, drag a device onto another network, or tap or click it (phones, keyboard: Enter) to open its sheet:

- **Network** - *Move to …* lists every network; the ones it cannot go to say why (protected device, management network, connected by cable when the network follows the switch port, network not known while the device is offline).
- **Name** - the name the controller shows for it (1 to 64 characters).
- **Fixed address** - *Always give this device the same address* with the address; it must lie inside the device's network, must not be the network's own address or its gateway and must not belong to another device. Taking the tick away removes the fixed address.

Only what the connected controller can change is offered (its capability list); nothing is shown greyed out. The changes collect in the draft bar at the bottom, one chip per device; **Preview** shows every field old → new, and moving and changing the same device in one draft becomes one step. Known devices that are offline right now but carry settings of their own (name, fixed address, network override) are listed too, so they can be renamed or get their address before they come back.

The command palette (Ctrl K) has *Change a device (network, name, fixed address) …*, and a device found there offers *Move to … / change*; both open this sheet. Changing needs the **Operator** role (drafts and previews); applying needs an **Admin** with a passkey. Every draft change is written to the audit log.

### Changing the gateway: networks, fixed addresses, port forwards

When the executor's adapter can change the gateway (the simulation's sample gateway can; for real writes see the family guide listed in [COMPATIBILITY.md](COMPATIBILITY.md)), **Change** mode adds three cards:

- **Networks** - every network with address range, VLAN, address service, zone and what uses it (devices, fixed addresses, wireless networks, switch ports, port forwards). *New network* starts from a template (IoT, Cameras, Work, LAN) with a free private /24, a VLAN no other network uses (the third number of the address where it is free) and an address service from .100 to .199 filled in; the new network lands in the internal zone (it can be moved to a zone of its own, see [Zone firewall](#zone-firewall-zones-and-rules)). *Change* renames a network or changes its address service (on/off, range). *Delete* appears only for networks Lanloom created that nothing uses any more.
- **Fixed addresses** - the gateway's reservations; *Release* frees one, *Reserve an address* gives a known device a fixed address inside its own network.
- **Port forwarding** - *New port forward* (name, protocol, outside port or list or range, target device or address, target port), *Change*, *Switch on* / *Switch off*, and *Delete* for forwards Lanloom created.

The preview names every field old → new and warns in plain words: a device outside a new address range gets a new address at its next lease, a switched-off address service stops handing out addresses, a renamed network keeps its old name in rules and dashboards, a released fixed address may change, and **what uses the device by its address** - a hub integration whose device page points to it, a presence tracker, an automation with the address written in its configuration, or one of Lanloom's own sources. A new port forward (or a change other than its name) says which port becomes reachable from the internet and needs its own tick.

Refused right in the preview, and again by the executor right before it writes: a range that is not private, not between /16 and /29 or overlaps another network; a VLAN outside 2 to 4094 or already used; an address service range outside the network or over the gateway address; a range or VLAN change of a network in use; switching off the address service while devices take dynamic addresses; changes of the management network, internet uplinks, VPN and guest-portal networks; deleting a network or forward Lanloom did not create; a fixed address outside the device's network, on the gateway or already taken; a forward to a public address, a gateway, the controller or network equipment, or on an outside port another forward uses.

### Zone firewall: zones and rules

When the executor's adapter can change zones and rules (the simulation can; for real writes see the family guide listed in [COMPATIBILITY.md](COMPATIBILITY.md); the gateway must run a zone-based firewall), **Change** mode adds a **Zones** card and tools on the rule list. Real writes still go only through the executor, every plan is signed with a passkey, and backup, rollback and watch work as for every other change.

**Zones card.** Every zone with its networks and how many networks and rules refer to it. Built-in zones carry a *built-in* chip, zones Lanloom created *created by Lanloom*.

- *New zone* - a name only. The zone starts empty; networks come in by *Move to zone …*.
- *Rename* - zones that are not built in.
- *Remove* - only zones Lanloom created, and only when no network and no rule is in them any more. The controller would delete the rules of a removed zone without a word; Lanloom refuses instead and the card says *empty it first*.
- *Move to zone …* - a select per local network: into a zone of its own or back into the internal zone. Never the management network, never into another system zone (external, gateway, VPN, hotspot, DMZ).

**Rule tools.** In Change mode each changeable rule row of the *Firewall rules* card offers:

- an *on* checkbox - switch the rule off or on;
- *Change* - name, action (allow, block, reject), *From* zone and optionally one network of it, *To* zone and optionally one network, protocol (any, TCP, UDP, TCP and UDP), ports (a list or ranges, only with TCP or UDP) and *allow answers back* (return traffic);
- up and down - move the rule within the own rules of its zone pair; one move per pair and plan, not together with a new or removed rule of that pair;
- *Remove* - only rules Lanloom created.

*New rule* above the list opens the same dialog; planned new rules are shown above the list marked *planned*. Only rules whose every setting Lanloom can carry over are changeable: rules with schedules, connection-state filters, address, region, app or domain matches, source ports, an IPsec match or an ICMP type stay read-only, and built-in rules of the controller are never changed.

**May one reach the other?** The card of the read firewall view (`#fw-ask`, see [the manual](manual/firewall.md)) also answers in the editor, from the editor's rules (simulation or executor): the answer now and, while there is a draft, the answer **after your draft** - a new rule, a rule switched off, a new order or a network moved into another zone are evaluated on the state after the plan. Two places in the same zone are allowed when no rule decides. Nothing is sent anywhere. API (role viewer): `GET /api/firewall/ask?source=editor&from=<internet|zone:id|network id|device id|address>&to=...&proto=tcp|udp&port=1-65535` returns the answer of the read view plus `after` (`{"verdict", "verdict_label", "reason", "rule", "certain"}`, or none without a draft).

**Preview.** New, changed and removed zones and rules are listed field by field, old → new. Warnings name the zone pairs that open or close (*Internal → Cameras will be blocked*), a network that moves zone and a rule that is switched off. A rule that lets traffic in from the internet (the external zone) or from another zone into the management zone needs its own confirmation tick.

**Lockout rules** - the same rules in the app's preview and again in the executor right before it writes, against its own fresh read. A refusal stops the plan and nothing is sent:

- never a blocking (block or reject) rule whose source or target is the management network, the management zone or the gateway (it would cut address service, name service and the controller);
- a reachability check after the plan: the device of the person who approves, Lanloom itself and the executor must still reach the gateway (name service, UDP 53) and the controller (TCP on its port) if they could before; a plan that would cut such a path is refused and the path is named;
- a zone is deleted only when Lanloom created it and it is empty;
- the management network never moves to another zone;
- at most 10 objects per plan, as before.

**Capability keys.** The new step write keys are `network.update.zone`, `zone.create`, `zone.update.name`, `zone.delete`, `policy.create`, `policy.update`, `policy.delete` and `policy.reorder`. The simulation and the write half of a device family with a zone firewall declare them. An executor of an earlier release does not, so the controls are not offered until the executor is updated by hand.

**Roles.** Viewers may use *May one reach the other?*; drafting needs the **Operator** role, building and approving a plan an **Admin** with a passkey. Changes are in the audit log. The command palette has *Zones of the firewall* and *New firewall rule* (deep links `#/firewall/zones`, `#/firewall/rules/new`); *May one reach the other?* opens the editor's card (`#/firewall/ask`) when no firewall is read. The nightly report has a section **Zone matrix**.

### Changes made outside Lanloom

The editor writes `fw_change` for what it wrote itself. Rules changed directly on the device or in the controller are noticed
from the configuration the connection reads: a small state without addresses, compared after two equal reads in a row, becomes
journal events (`fw_drift`) shown in the change list, the nightly report and the incident files (*What changed before*, a day
back). A change the editor wrote or was writing is not told twice. See the
[manual page](manual/incidents.md#rule-changes-made-outside-lanloom).

### Switch ports

When the editor's source can change ports (the simulation can; for real writes see the family guide listed in [COMPATIBILITY.md](COMPATIBILITY.md)), **Change** mode adds a **Switch ports** card: every switch of the controller as its faceplate (the same front view as the read-only *Switch front panels*). A port opens its panel (link, untagged network with VLAN, tagged networks, power over Ethernet, devices behind it, last 24 hours); in Change mode the panel has **Change port ...** and a port with a change in the draft carries an accent ring and the note *Planned*. Real writes still go only through the executor, every plan is signed with a passkey, and backup, rollback and watch work as for every other change.

**Plan step kinds.**

- `port.set` - settings of one port. Fields (only those that change): `name` (empty = the switch's own name for the port), `profile` (id of a port profile; empty = own settings), `native_network` (the untagged network; `native_vlan` instead for families that name it by VLAN number), `tagged_mode` (`all`, `none` or `custom`), `tagged` (network ids of the choice, only with `custom`) and `poe` (`on` or `off`). With a profile in the same draft the profile decides the networks (and the power when it names it), so network fields are dropped.
- `port.cycle` - an action: `before` is `{}`, `after` is `{"cycle": true}`. There is no way back and the step is always confirmed.

**Capability keys.** `port.set.name`, `port.set.profile`, `port.set.native_network`, `port.set.tagged_mode`, `port.set.tagged`, `port.set.poe` (B*) and `port.cycle` (B*). The sheet offers each field only when the executor's family declares its key. An executor of an earlier release does not, so the controls are not offered until the executor is updated by hand.

**Lockout rules** - the same rules in the app's preview and again in the executor right before it writes, against its own fresh read. A refusal stops the plan and nothing is sent:

- only the **name** may change on the uplink of a switch, the internet port of a gateway, a port behind which network equipment hangs (a switch or access point whose uplink points there, or a known neighbour), a port where another device announces itself with LLDP, and mirror or aggregation ports;
- a port that carries a device Lanloom protects (gateway, servers, hubs, Lanloom's own host), the approver's device, the executor or the controller keeps its networks and its power; the panel's lock note names the reason (*only its name can change*);
- only local networks are used, never internet uplinks or VPN networks.

**Confirmations (B\*).** Every change of power, every power cycle, and the management network as the untagged network of a port or newly tagged on it (*whatever is plugged in reaches the admin pages of the network equipment*) need their own confirmation tick.

**Plan rules.** One change per port and plan (a power cycle and new settings of the same port go into two plans); at most 10 objects per plan, as before. Power can only be set on ports that can deliver it, and only to automatic or off; passive and pass-through modes are left alone.

**Preview warnings.** Devices on the port change network and get new addresses (named); a device keeps a fixed address of its old network; something that uses a device by its address (hub integration, tracker, automation, Lanloom source) is named; tagged networks change (VLAN-aware devices such as access points or servers with virtual machines see other networks); several devices share the port (a small switch behind it); power off names the devices that switch off; a power cycle names the devices that restart; a rename.

**Simulation.** The sample network has a sample switch *Core switch* with 16 copper ports and one fibre cage, two access points on power over Ethernet behind a trunk profile, the wired sample devices on their ports and four port profiles (*Access point trunk*, *Cameras*, *Smart home*, *Work desk*). Ports of protected sample devices (storage, home server, broker, hub) show the lock.

**Roles and palette.** Drafting needs the **Operator** role, approving an **Admin**; viewers see the faceplates without the button. The command palette has *Change a switch port* (`#/firewall/ports`), which opens the card in Change mode.

**Nightly report.** A section *Port settings* (only when a connection reads port settings) compares with the previous night, whoever changed a setting (Lanloom or the controller's own pages): see [NIGHTLY-REPORT.md](NIGHTLY-REPORT.md).

### Wireless networks and blocked devices

When the editor's source can change wireless networks (the simulation can; for real writes see the family guide listed in [COMPATIBILITY.md](COMPATIBILITY.md)), **Change** mode adds two cards: **Wireless networks (n)** with **New SSID** and **Change...** per SSID, and **Blocked devices (n)** with **Block a device...**. The device sheet has a block switch. Real writes still go only through the executor, every plan is signed with a passkey, and backup, rollback and watch work as for every other change.

**Plan step kinds.**

- `ssid.create` - a new wireless network. Fields: `name` (up to 32 characters), `network` (a local network id), `bands` (`2.4ghz`, `5ghz`, `6ghz`; at least one), `security` (`open`, `wpa2_personal`, `wpa2_wpa3_personal`, `wpa3_personal`), `hidden`, `enabled`.
- `ssid.update` - the same fields, only those that change (`enabled: false` switches it off).
- `ssid.delete` - only an SSID Lanloom created.
- `client.block` - block or unblock a device: field `blocked` (`true` or `false`). Always confirmed (B\*). There is no step that disconnects a device without blocking it: a disconnected device simply comes back, so it is never offered.

**Passwords.** A password is never a field of a step and never appears in a plan, a preview, the history, a snapshot or a log. For a new protected SSID (or an open one that becomes protected) the executor generates a random password of 24 characters. The user reads it, shows its QR code or sets their own in the controller's own app. An SSID that keeps its security keeps its password.

**Capability keys.** `ssid.create`, `ssid.update.name`, `ssid.update.network`, `ssid.update.bands`, `ssid.update.security`, `ssid.update.hidden`, `ssid.update.enabled`, `ssid.delete` and `client.block` (B\*). The cards offer each field only when the executor's family declares its key. An executor of an earlier release does not, so the controls are not offered until the executor is updated by hand.

**Never written.** Radio settings (channel, channel width, transmit power), enterprise security that signs devices in through a login server (RADIUS), the network or security of an SSID with private pre-shared keys per device, and passwords.

**Lockout rules** - the same rules in the app's preview and again in the executor right before it writes, against its own fresh read. A refusal stops the plan and nothing is sent:

- a change that drops clients (off, delete, rename, another network, a dropped band, another security) is refused while a device Lanloom protects (gateway, servers, hubs, Lanloom's own host), the approver's device, the executor or the controller is connected over that SSID; hiding the name never drops anyone;
- an SSID carries only a local network, never an internet uplink or a VPN network;
- a device Lanloom protects, the approver's own device, the executor, the controller and Lanloom's host are never blocked.

**Confirmations (B\*).** Every block and unblock; an open SSID, which is allowed only on a guest network (*anyone nearby can join*); and the management network on an SSID (*whoever joins reaches the admin pages of the network equipment*; never together with open).

**Plan rules.** 6 GHz only with WPA3 personal; names are unique; one change per SSID and per device and plan; at most 10 objects per plan, as before. The preview warns when more than 4 SSIDs share a band (every extra SSID costs airtime on every access point).

**Preview warnings.** How many devices lose the wireless network or have to join again, which devices change network and get new addresses, that devices that know a hidden name still join, that a band is left, that an SSID has no password, that a password is generated, that a device with a private hardware address may come back as a new device after a block, and that an unblocked device may join again.

**Simulation.** The sample network has 6 SSIDs (a guest network, an IoT network and a hidden one among them) and one blocked device (*Visitor phone*) to rehearse with. SSIDs and devices of protected sample devices show the lock.

**Roles and palette.** Drafting needs the **Operator** role, approving an **Admin**; viewers see the cards without the buttons. The command palette has *Change a wireless network* (`#/firewall/wireless`) and *Block a device* (`#/firewall/block`).

**Nightly report.** A section *Wireless networks* (only when a connection reads wireless networks) compares with the previous night, whoever changed a setting: see [NIGHTLY-REPORT.md](NIGHTLY-REPORT.md).

### Network plan

The **Network plan** card keeps, per device, the network it should end up in - an existing network, or the name of one still to come (*IoT*). It shows the progress (*X of Y moved*), one row per target with its devices (moved ones ticked) and marks each device tile that still has to move (*→ IoT*). *Plan targets* opens a dialog: choose the target, tick the devices, save. When a target network does not exist yet, *Create this network* opens *New network* with the name filled in; once it exists, *Draft N moves* puts the moves that are possible now into the draft (at most one plan's worth, 10 objects). The plan is Lanloom's own note, stored by the devices' opaque ids, never a controller setting; it works with a read-only connection too, needs the **Operator** role to change and is in the audit log. The nightly report has a section **Network plan** with the progress and the devices moved since the last report.

### Changing a guest: start, shut down, network card, firewall

With a hypervisor family behind the editor (the simulation has a sample hypervisor next to the sample network; for real writes see the guide of the hypervisor family listed in [COMPATIBILITY.md](COMPATIBILITY.md)), **Change** mode adds a **Guests** card:

- **Start** / **Shut down** - a clean shutdown with a time limit; a guest that does not shut down keeps running and the plan is rolled back. Both need their own confirmation in the preview.
- **Network card** - *Change …* opens a sheet with the bridge (bridges of the guest's node and virtual networks of the cluster), the VLAN tag (empty = none) and the card's firewall flag. A VLAN the bridge does not allow, a bridge the node does not have and a VLAN on a virtual network that carries its own are refused right in the preview.
- **Firewall** - switch the guest's own firewall on or off (own confirmation), switch a rule on or off, change a plain rule (direction, action, protocol, ports, source, comment), add a rule (appended at the end, marked as Lanloom's own) or remove one Lanloom created.

Guests that keep the network running show a lock chip with the reason (name server, runs Lanloom, gateway, …) and no stop, card or rule controls; templates, guests locked or protected on the host and guests run by the cluster's high-availability manager show no controls either. The same rules are checked again by the executor right before it writes. One change per guest object and plan; a new or removed rule is the only rule change of its guest in a plan. The command palette has *Change a guest (start, shut down, network card, firewall) …*, which opens this card.

The nightly report has a section **Changes written**: what the executor changed on the controller since the last report (done, undone, refused), with a point for each change that was put back on its own or could not be put back. It appears when an external executor is set up; rehearsals on the sample network never appear.

## How a change runs

```
browser ── draft ──▶ app: preview (plain words, warnings, blocks) ──▶ plan (ids only) + hash + nonce + 5 min expiry
   │  passkey: WebAuthn assertion, challenge = SHA-256(canonical plan)
   └──────────────▶ app pre-checks the signature with the stored public key and queues the plan
executor (pull every 10 s, bearer token) ◀── GET /api/executor/plans
   verify shape · target · expiry · nonce unused · signature against ITS OWN pinned keys (rp id, origin, user verified, counter)
   read the controller fresh · gate · backup of every touched object (0600) · inverse plan
   apply through the adapter · let the controller settle · watch · done, or roll back on its own
   └──▶ POST /api/executor/report (progress) and /api/executor/state (fresh state)
```

**Plan format** (canonical JSON: sorted keys, no spaces, no floating-point numbers, so the hash is the same in the browser, the app and the executor):

```json
{"v": 1, "id": "p_1762790000_1a2b3c4d", "target": "controller", "base": "fp:…", "created": 1762790000,
 "expires": 1762790300, "nonce": "…", "requester": {"user": "admin", "device": null},
 "steps": [{"kind": "move", "device": "d10",
            "before": {"network": "n_main", "ip": "192.168.50.22"},
            "after":  {"network": "n_work", "ip": "192.168.40.22"}}],
 "warnings": ["fw.warn.new_ip", "fw.warn.reprovision"], "confirmed": [], "costs": {"objects": 1}, "undo_of": null}
```

Device and network ids are opaque. The adapter derives them (for example as a salted hash) and resolves them again from its own fresh read; hardware addresses never leave the executor.

## The gate

Right before writing, against a fresh controller read:

1. Plan shape valid, target is the executor's own target, not expired, nonce never seen.
2. Signature valid for a pinned key (simulation in the app: no signature, nothing is written).
3. At most **10 objects** per change. Step kinds are checked against the capability list, for example `network.update.zone`, `zone.create`, `zone.update.name`, `zone.delete`, `policy.create`, `policy.update`, `policy.delete` and `policy.reorder` for the zone firewall.
4. The controller state still matches the plan (`base` fingerprint and every `before`), otherwise "the network changed in the meantime".
5. **Lockout protection:** protected devices (gateway, network equipment, servers others depend on, the host running Lanloom) never move or change; management networks are no target; wired devices that share a port with others do not move alone, and devices whose network follows their switch port are not moved by a client setting; the device you approve from must keep its internet. For zones and rules the lockout rules are listed under [Zone firewall](#zone-firewall-zones-and-rules), including the reachability check of the gateway and the controller.
6. **Addresses:** a new or fixed address lies inside its network, is not the network's own address or its gateway, belongs to no other device and is not used twice in the same plan (the gate checks this itself; not every controller does).
7. Marked points (private address, your own device, quiet hours) were confirmed in the preview.
8. The adapter reports the controller healthy and no other change is still running or being watched.

## Backup, watch, rollback, history

- Before the first write the executor stores a raw snapshot of every touched object in `<state>/backup/<plan>/before.json` (mode 0600, created exclusively, never through a symbolic link) and builds the inverse plan.
- After writing it lets the controller settle, then watches (default 60 minutes, one probe per minute). If the adapter reports the network unhealthy, **everything** goes back. If single devices do not come back in their new network on two probes in a row, **only those** go back. There is at most one automatic rollback.
- If the process dies mid-change, the next start restores the backup before doing anything else.
- `<state>/protocol.jsonl` keeps one line per run (plan hash, key, user, times, result; no addresses, no secrets). The app shows the recent changes, and a successful change can be **undone for 24 hours**; the undo is a new plan with its own preview and signature.

## Controller adapters

Adapters are **built in only**. Each device family has its own folder in `lanloom/integrations/<family>/` and is listed in a fixed registry; the executor loads the write half of exactly the family named in `LANLOOM_EXECUTOR_ADAPTER`, and nothing else can be loaded (no module paths, no plugin folder). Which families exist and what each can read and write is listed in [COMPATIBILITY.md](COMPATIBILITY.md); how adapters are built (two halves, capability list, neutral model, plan steps, credentials) is described in [INTEGRATIONS.md](INTEGRATIONS.md). Requests for a family are welcome as issues, see [CONTRIBUTING.md](../CONTRIBUTING.md).

The write half of an adapter subclasses `lanloom.firewall.adapter.ControllerAdapter`:

| method | contract |
|---|---|
| `read()` | fresh neutral state (see INTEGRATIONS.md): zones, allowed zone pairs, networks, devices with opaque ids, and, depending on the family, ports, policies, wireless networks, guests |
| `capabilities()` | the family's capability list; the gate refuses every step it does not cover, and the editor only shows what it covers |
| `snapshot(ids)` | raw backup of the touched objects; per-object lists allow rolling back single objects |
| `apply(step)` | one plan step; idempotent (already done: do nothing); a create returns the id the controller gave the new object |
| `restore(snapshot)` | put the objects back exactly as in the snapshot and remove what the plan created |
| `health()` | `(ok, reason)`: is the controller fit for a change right now |
| `settle(steps)` | wait until provisioning finished |
| `probe(steps)` | `{"global_ok": bool, "missing": [ids not as planned]}` |
| `lockout(state, step)` | extra lockout rule of the family (the neutral rules always run) |
| `plan_lockout(state, steps)` | the same over the whole plan (rules that depend on several steps) |
| `endpoints()` | host names or addresses the adapter connects to; the executor's connection guard allows nothing else |

Every request of a write half goes through one send path with a field-exact allow-list (method, path pattern, body fields, allowed values), checked below the adapter. See `SimulatedController` in `lanloom/firewall/adapter.py` for a complete reference and `tests/test_plan_v2.py` for stand-ins that fail on purpose.

## Turning on the external executor

1. Create a token and put the settings in a `.env` file next to `compose.yaml`:

   ```sh
   echo "LANLOOM_FW_EXECUTOR=external" >> .env
   echo "LANLOOM_EXECUTOR_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" >> .env
   echo "LANLOOM_EXECUTOR_ORIGINS=https://lanloom.example" >> .env
   docker compose --profile executor up -d
   ```

   The executor starts with `LANLOOM_EXECUTOR_TARGET=simulation`: it rehearses the complete signed path against its own sample network. The app shows "Executor connected".
2. Signing needs a passkey and therefore HTTPS (or localhost). Add a passkey under **Settings**. Open **Firewall → Executor**, copy the signing key and pin it in the executor:

   ```sh
   docker compose exec -T executor python -m lanloom.executor trust-key < key.json
   ```

   The command prints a fingerprint. Compare it with the one shown in the app; only then is the key trusted. The executor never takes keys from the app on its own.
3. Rehearse a few changes in the rehearsal target. Every change asks for the passkey.
4. Only then, to write for real (and only for a family listed in COMPATIBILITY.md with write support): put the write credentials in an options file, set `LANLOOM_EXECUTOR_TARGET=controller`, `LANLOOM_EXECUTOR_ADAPTER=<family id>`, `LANLOOM_EXECUTOR_ADAPTER_OPTIONS=/path/options.json` and `LANLOOM_EXECUTOR_ARM=I-UNDERSTAND`, and create the arming file by hand:

   ```sh
   docker compose exec executor touch /state/armed
   ```

   The options file must be mode 0600, belong to the executor's user and be a regular file (a symbolic link is refused). Prefer an access token over a password and give it the least rights your controller offers. The executor refuses superuser principals and warns about owner-level roles. Without both the phrase and the file the executor refuses to start with a real adapter. Remove the file to disarm.

The executor is updated by hand (`docker compose pull executor && docker compose --profile executor up -d executor`) on purpose: the one-click updater of the app does not touch it, so the app cannot replace the component that holds the controller credentials. An executor that is older than the app keeps working for the plans it understands: plans keep the lowest version that can express them (moves only stay version 1), a plan of a newer version is refused with a clear message instead of being half-understood, the executor logs that it is older than the app, and the app's executor dialog shows it.

## Settings

| variable | where | default | meaning |
|---|---|---|---|
| `LANLOOM_FW_EXECUTOR` | app | `simulation` | `external` hands plans to the executor service |
| `LANLOOM_EXECUTOR_TOKEN` / `_FILE` | app and executor | – | shared token (at least 24 characters) for the executor routes |
| `LANLOOM_FW_QUIET_HOURS` | app and executor | off | for example `21-7`: changes then need an explicit confirmation |
| `LANLOOM_FW_SIM_WATCH_S` | app | `20` | watch time of the simulation |
| `LANLOOM_EXECUTOR_APP` | executor | `http://127.0.0.1:7380` | address of the app |
| `LANLOOM_EXECUTOR_TARGET` | executor | `simulation` | `controller` for real writes (needs adapter and arming) |
| `LANLOOM_EXECUTOR_ADAPTER` | executor | – | family id of a built-in write adapter (`controller` target only) |
| `LANLOOM_EXECUTOR_ADAPTER_OPTIONS` | executor | – | options file with the write credentials (0600, owned by the executor's user) |
| `LANLOOM_EXECUTOR_ORIGINS` | executor | – | origins the app is opened from; required for `controller` |
| `LANLOOM_EXECUTOR_WATCH_S` | executor | `3600` | watch time after a real change |
| `LANLOOM_EXECUTOR_DENY_HOSTS` | executor | – | host names or addresses the executor must never connect to (for example the real controller while it rehearses against a test controller) |
