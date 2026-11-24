# UniFi Network (local API): read adapter and client write half

Lanloom reads the configuration and the state of a UniFi Network controller (a console or a self-hosted Network application) over its local APIs. The web app **only reads**. Since 1.23.0 the optional executor service can also change **clients** - move a client to another network, give it a fixed address, rename it - and since 1.37.0 **internal networks**, **fixed addresses** and **port forwards** and then **zones and zone rules**, each after the safety checks and your passkey signature (stage B, see [Changing clients](#changing-clients-stage-b) and [Changing networks, fixed addresses and port forwards](#changing-networks-fixed-addresses-and-port-forwards-stage-b) below). Zones and rules are described under [Zone firewall](#zone-firewall-stage-b), **switch ports** (name, profile, untagged and tagged networks, power over Ethernet, power cycle) under [Switch ports](#switch-ports-stage-b), **wireless networks** (SSIDs) and **blocking devices** under [Wireless networks and blocked devices](#wireless-networks-and-blocked-devices-stage-b). Nothing else is ever written.

What you get:

- **Firewall tab, live and read only:** the zones of the zone-based firewall, the networks in each zone with VLAN and address range, the firewall rules (policies) in the controller's order with their filters, a zone matrix, and hit counters where the controller reports them. Port forwarding and wireless networks (name, network, bands, security type) are listed below.
- **Network equipment and ports:** gateways, switches and access points with model, address, firmware, uplink and every switch port (link, speed, power over Ethernet, native VLAN, profile, uplink and neighbour).
- **Physical topology:** which client sits on which switch port or access point. This feeds the same topology resolver as the generic switch-controller source (manual entries still win, see [TOPOLOGY.md](../TOPOLOGY.md)).
- **Fixed addresses** (DHCP reservations), WAN and VPN connections (names and kinds only).

Never read or kept: wireless passphrases and pre-shared keys, RADIUS settings, account data, serial numbers. Policy and object ids are turned into opaque ids; hardware addresses are used only to match devices on the map.

## Two APIs, two optional credentials

| | Official integration API | Classic API |
|---|---|---|
| credential | **API key** (recommended) | **local account with view-only rights** |
| adds | versioned objects: networks, zones, policies, wireless networks, devices, clients, WAN, VPN, ACL and DNS rules (titles) | rule hit counters, switch-port settings and profiles, port forwarding, fixed addresses, the exact port of each client; rules on releases before the official API |
| paths | `/proxy/network/integration/v1/…` (console) or `/integration/v1/…` (self-hosted) | `/proxy/network/api/s/<site>/…` and `/proxy/network/v2/api/site/<site>/…` (console) or without `/proxy/network` (self-hosted) |

At least one credential is needed, both together give the full picture. The official API is used wherever it covers an area; the classic API fills the gaps.

### Create an API key

1. In the controller open **Settings → Control Plane → Integrations** (the wording differs slightly between releases).
2. Create a key, preferably while signed in as an administrator whose role is **view only**, and copy it once.
3. Paste it into Lanloom (**Settings → Connections → + UniFi Network (local API)**, field *API key*).

An API key does not tell Lanloom which rights it has, so the connection test cannot refuse a key that could write. Lanloom never sends anything but read requests with it (see below); still, a key of a view-only administrator is the safer choice.

### Create a view-only account (optional, recommended for hit counters and ports)

1. **Settings → Admins & Users → Create new**: a **local** account (no remote or cloud access), role **View Only** for the Network application.
2. Enter user name and password in Lanloom.

Lanloom checks the account's role on the site when you test or save the connection and **refuses an account that can change settings** (site administrator, owner or super administrator). The role on the site decides: an account with the role *View Only* is accepted even when a self-hosted controller also lists it as an owner; a super administrator is always refused. Do not use your own administrator account.

## Settings

| field | meaning |
|---|---|
| Address | `https://<console address>` or, self-hosted, `https://<host>:8443` |
| API key | optional, see above |
| User name / Password | optional view-only account |
| Site | the site's short name (`default` unless you created more sites) |
| Accept a self-signed certificate | needed for most consoles in a home network, which use their own certificate |

## What Lanloom sends

Exactly one code path sends requests, through an allow-list that is checked before anything leaves the app:

- `GET` on the read endpoints listed in `lanloom/integrations/unifi/read.py` (`CLASSIC_GET`, `OFFICIAL_GET`), and nothing else;
- for the view-only account: one `POST` to the sign-in endpoint whose body is exactly user name and password, and the sign-out.

Sign-ins are capped (six per hour per controller and account) and a refused sign-in pauses further attempts for 15 minutes, so a wrong password cannot lock the account. After HTTP 401 Lanloom signs in again once; after 403 it never does. Clients and devices are read every minute, the configuration every five minutes.

## Traffic statistics

The adapter reads byte totals per client and per port for the live traffic of the map. It does **not** feed the data paths (traffic by country) or the conversations between devices: neither the official API nor the classic lists name a destination or country, and application categories cannot honestly be turned into countries. For those, let the gateway export NetFlow / IPFIX to Lanloom's receiver, see [DATA-PATHS.md](../DATA-PATHS.md#what-the-adapter-statistics-can-and-cannot-do).

## Releases

| controller release | reads |
|---|---|
| Network 10.1 or later | everything above; the official API adds firewall policies |
| Network 10.0 | official API without policies; policies through the classic API (view-only account) |
| Network 9.x | official API for devices and clients only; zones and policies through the classic API |
| before the zone-based firewall | classic firewall rules shown as a list (not as zones) |

Tested versions are listed in [COMPATIBILITY.md](../COMPATIBILITY.md).

## Changing clients (stage B)

The write half runs only inside the executor service (its own container, see [FIREWALL-EDITOR.md](../FIREWALL-EDITOR.md#turning-on-the-external-executor)), never in the web app, with an account the app never sees. It can do exactly this:

| change | what is written | notes |
|---|---|---|
| move a client to another network | network override and a fixed address in the new network | wireless clients only - a cable-connected client changes its network through its switch port (port settings come in a later release); the client reconnects, the executor waits for it |
| fixed address | fixed address on (with the address) or off | the address must lie inside the client's network, must not be the network's own address or its gateway, and must not belong to another client; the client picks it up when it renews its address |
| name | the client's name (alias) | 1 to 64 characters |

Everything else - firmware, restarts, accounts - is refused by the allow-list below the adapter, whatever a plan says.

## Changing networks, fixed addresses and port forwards (stage B)

| change | what is written | notes |
|---|---|---|
| new network | `rest/networkconf`: an internal (corporate) LAN network with a VLAN of its own, its gateway address and range, the address service (on/off, range) | private IPv4, /16 to /29, overlapping nothing; VLAN 2 to 4009 (the controller keeps 4010 and above), not used by another network; it lands in the internal zone |
| change a network | name and address service (on/off, range) | never WAN, VPN or guest networks; the address service stays on while clients take dynamic addresses there |
| delete a network | the network | only networks the executor created itself (remembered in `unifi-own.json` in its state directory) and only when nothing refers to them any more |
| fixed address | `rest/user`: fixed address on (with the address) or off | inside the client's own network, not its gateway, not taken by another client |
| new port forward | `rest/portforward`: name, protocol (TCP, UDP or both), outside port(s), target address and port, on, from any source, on the internet uplink | the target is a private address inside a local network, never a gateway address or the controller; an outside port is used by one forward only |
| change a port forward | name, on/off, protocol, ports, target | switching off and renaming need no confirmation of their own, everything else does |
| delete a port forward | the forward | only forwards the executor created itself |

Each body is checked field by field by the allow-list before it is sent (a new network and a new forward must carry every field, each value in its range), each object is read back after writing, and a rollback puts back exactly the fields that were changed (a created object is deleted again, a deleted one created again from the backup). Tested against a self-hosted Network application 10.0.162 (create, rename, address range, delete own, forward with and without its confirmation, switch off, delete, release and reserve, refused deletion of a foreign network, partial write, crash after creating and the restart that removes it again).

**Checks the controller does not make itself.** Tested on a self-hosted Network application 10.0: it stores a network id that does not exist, an address that does not fit the named network, an empty or a very long name without a word. The write half checks all of these before it sends anything, reads every client back after writing and stops (the executor rolls back) when the values did not take.

**Account.** Client settings exist only in the classic API (the official integration API has no client writes), so the executor signs in with a **local account of its own**: role *Site Admin* (or a custom role with rights to the Network application only), no remote access, no system rights. The read-only account of the app stays separate. An owner or super administrator works but gives a warning in the executor log; a view-only account is refused (it cannot write).

**Options file** of the executor (`LANLOOM_EXECUTOR_ADAPTER=unifi`, `LANLOOM_EXECUTOR_ADAPTER_OPTIONS=<path>`, mode 0600, owned by the executor's user, never a symbolic link):

```json
{"url": "https://192.168.1.1", "site": "default",
 "write_username": "lanloom-writer", "write_password": "…",
 "allow_self_signed": true, "cert_sha256": "<SHA-256 of the console's certificate>"}
```

`cert_sha256` binds the connection to the console's own certificate (recommended when the console uses a self-signed one; without it the executor logs a warning). One way to read it: `openssl s_client -connect 192.168.1.1:443 </dev/null 2>/dev/null | openssl x509 -outform der | sha256sum`. Unknown option names are refused at start, so a typo cannot go unnoticed.

**Sessions.** At most four sign-ins an hour, counted across executor restarts (a small file in the executor's state directory), a 15-minute pause after a refused session, one new sign-in after HTTP 401 and none after 403, a planned new sign-in before the controller's own session limit.

**Connection guard.** The executor process can connect only to the app and to the controller's address (resolved at start); anything else is refused before a connection is made. A rehearsal executor that writes to a test controller lists the real console under `LANLOOM_EXECUTOR_DENY_HOSTS`, so a run against the test controller can never reach the real one.

**Before the first real change:** keep local console access at hand, try the change on the sample network (simulation) first, and read the warnings in the preview. Use at your own risk; Lanloom never writes without your passkey.

## Zone firewall (stage B)

Zones and zone rules are written over the **classic zone-firewall API** of the controller (`v2/api/site/<site>/firewall/zone`, `firewall-policies` and `firewall-policies/batch-reorder`) with the **same local site-administrator write account** as the other stage-B changes; no further account or option is needed. Rules of the official integration API stay read-only in the app (the app shows them; writes go through the executor's classic account).

| change | what is written | notes |
|---|---|---|
| new zone | a zone with its name | the zone starts empty |
| rename a zone | the zone's name | not for built-in zones |
| move a network | the zone's list of networks (the network leaves its old zone and joins the new one) | into a zone of its own or back into the internal zone; never the management network, never into another system zone |
| delete a zone | the zone | only zones the executor created (remembered in `unifi-own.json`, area `zone`), only when empty and no rule refers to it |
| new rule | a rule in the zone-firewall rule list | allow, block or reject; protocol any, TCP, UDP or both; sides are a zone, optionally one network; ports only on the destination |
| change a rule | name, on or off, action, protocol, sides, ports, return traffic | only rules whose every setting Lanloom can carry over |
| reorder rules | `firewall-policies/batch-reorder` | within the own rules of one zone pair |
| delete a rule | the rule | only rules the executor created (area `policy`) |

**Whole bodies, field-exact.** The controller takes rule updates only whole, so the write half always sends the complete, checked set of fields. The allow-list below the adapter checks every request field by field: a zone carries `name`, `network_ids` and, on update, `_id`; a rule carries name, enabled, action (`ALLOW`, `BLOCK`, `REJECT`), `create_allow_respond`, logging (kept as it was), protocol (`all`, `tcp`, `udp`, `tcp_udp`), `ip_version` (`BOTH` or `IPV4`, kept), connection state `ALL`, schedule `ALWAYS`, and the two sides with a zone id plus either any network or one network, ports only on the destination. Anything else is refused whatever a plan says. Every object is read back after writing and the plan stops (and rolls back) when the values did not take.

**Backup and way back.** Before a plan the executor backs up all zones (with their networks) and the own rules. The way back restores names, network membership, rule settings and the order per zone pair; created zones and rules are deleted again, which the executor does only for what it remembers creating.

**Own path stays open.** The executor determines the local address it reaches the controller from (without sending anything) and checks that its own path to the controller stays open after the plan; together with the other lockout rules (see [FIREWALL-EDITOR.md](../FIREWALL-EDITOR.md#zone-firewall-zones-and-rules)) it refuses a plan that would cut it.

**Requirements and limits.** Network 9.0 or later on a gateway that runs the zone-based firewall. Older controllers with classic firewall rules stay read-only. Rules with schedules, connection-state filters, address, region, app or domain matches, source ports, an IPsec match or an ICMP type are shown but never changed, and built-in rules are never changed.

**Tested** against a self-hosted Network application 10.0.162 test twin switched to the zone-based firewall: a zone created, a network moved into it, a block rule and an exception for one network and TCP 445, a reorder so that the exception decides first, switching a rule off, renaming it and undoing all of it, lockout scenarios refused with nothing sent, a partial write rolled back, and a crash after creating a zone recovered; the end state equaled the start state.

**How to test safely.** Rehearse in the simulation first (its sample network has zone rules). Then run the executor with `LANLOOM_EXECUTOR_TARGET=simulation` as described above, so the whole signed path is exercised without a controller. For the first real change pick a harmless one, for example renaming a zone Lanloom created, and keep local console access at hand. Use at your own risk; Lanloom never writes without your passkey.

## Switch ports (stage B)

Switch ports are written over the **classic API** with the **same local site-administrator write account** as the other stage-B changes; no further account or option is needed.

**What is written.** The switch's port overrides: `PUT rest/device/<id>` with the field `port_overrides` only. The controller replaces the whole list, so the write half sends the list exactly as it read it and changes only the entry of the planned port and only these fields:

| change | field of the entry |
|---|---|
| name | `name` |
| port profile | `portconf_id` |
| untagged network | `native_networkconf_id` |
| tagged networks | `tagged_vlan_mgmt` / `forward` (the mode) and `excluded_networkconf_ids` |
| power over Ethernet | `poe_mode` (`auto` or `off`) |

**Body check.** Before a request is sent, the write half compares the new list with the list it read; any difference other than the planned fields of the planned entry refuses the write. The allow-list below the adapter additionally checks the shape of every entry.

**Tagged networks.** Releases since 7.x keep the tagged networks of a port as a mode plus a list of networks left out; a choice is stored as the networks that are *not* tagged. Older releases only know all, untagged only and customize, so *a choice* is refused there.

**Power cycle.** `POST cmd/devmgr` with `{"cmd": "power-cycle", "mac": ..., "port_idx": ...}`: the power of the port is switched off for a few seconds. It is a one-off command; there is no way back.

**Read back and way back.** After every write the half reads the switch back and stops when the values did not take; the executor then rolls back. The way back writes the saved override entries of the touched ports, one write per switch.

**Power modes left alone.** Only automatic and off are set. Ports in a passive or pass-through power mode are never changed.

**What is read.** For every port the profile (id and name), the untagged network, the tagged mode and choice, the power mode (auto, off, passive, pass-through) and draw, the mirror or aggregate mode, the internet ports of gateways and LLDP neighbours; for every profile its networks. This feeds the lockout rules (see [FIREWALL-EDITOR.md](../FIREWALL-EDITOR.md#switch-ports)).

**Tested** against the adapter tests (console and self-hosted layout). The self-hosted test twin (Network application 10.0.162) has no switch hardware, so the test places a switch record into the twin's database and writes its overrides through the real API.

## Wireless networks and blocked devices (stage B)

Wireless networks and blocks are written over the **classic API** with the **same local site-administrator write account** as the other stage-B changes; no further account or option is needed.

**What is written.**

| change | request | fields |
|---|---|---|
| new SSID | `POST rest/wlanconf` | name, network, bands, security, hidden, enabled and the fixed defaults of a new SSID (all access points, default user group) |
| change an SSID | `PUT rest/wlanconf/<id>` | only the changed fields: `name`, `networkconf_id`, `wlan_bands`, `security`, `wpa_mode`, `wpa3_support`, `wpa3_transition`, `hide_ssid`, `enabled` |
| delete an SSID | `DELETE rest/wlanconf/<id>` | only an SSID this write half created |
| block or unblock a device | `POST cmd/stamgr` | exactly `cmd` (`block-sta` or `unblock-sta`) and `mac` |

The neutral security values map to the controller's fields: `open` is `security: open`; `wpa2_personal`, `wpa2_wpa3_personal` and `wpa3_personal` are `security: wpapsk` with `wpa3_support` and `wpa3_transition` set accordingly (WPA3 personal: support on, transition off).

**Allow-list.** The allow-list below the adapter refuses every field of `rest/wlanconf` that is not listed above and every `cmd/stamgr` command other than `block-sta` and `unblock-sta`. In particular `kick-sta` is refused: a disconnected device reconnects at once, so it is never a lasting answer and never offered.

**Passwords.** The password field (`x_passphrase`) is written only when an SSID is created protected or an open SSID gets a security. The write half generates 24 random characters and sends them to the controller; they are not part of a plan, a preview, the history, a snapshot or a log, and not part of what the app reads. Read the password, show its QR code or set your own in the controller's own app. An SSID that keeps its security keeps its password.

**What is left alone.** Radio settings (channel, channel width, transmit power), enterprise security that signs devices in through a login server (RADIUS) and the network and security of an SSID with private pre-shared keys per device (the controller lets each key pick a network). The editor says why a field is locked.

**Read back and way back.** After every write the half reads the wireless networks (or the device) back and stops when the values did not take; the executor then rolls back. The way back restores the saved fields of a changed SSID (never its password), deletes an SSID the plan created, creates an own SSID the plan deleted again (with a new random password) and unblocks a device the plan blocked, or blocks one the plan unblocked.

**What is read.** For every SSID the name, network, bands, security (`open`, `wpa2_personal`, `wpa2_wpa3_personal`, `wpa3_personal`, other values as the controller reports them), hidden flag, state and guest flag, and for every device whether it is blocked. This feeds the protection rules (see [FIREWALL-EDITOR.md](../FIREWALL-EDITOR.md#wireless-networks-and-blocked-devices)).

**Tested** against a recorded test fake of the classic API. Real writes follow only after acceptance at a tester.

## Troubleshooting

| status | what to do |
|---|---|
| API key was rejected | create a new key; check the address (console vs. self-hosted path) |
| Account was rejected | user name or password wrong, or the account is not local |
| This account can change settings | give it the view-only role on the site, or create a separate view-only account (a super administrator is always refused) |
| Sign-in paused | wait 15 minutes after repeated refusals; check the password first |
| Access denied for this account | the role lacks rights to the Network application |
| TLS / certificate problem | tick *Accept a self-signed certificate* |
| Executor: "A wired device changes its network through its switch port" | the client is connected by cable; only wireless clients can be moved by a client setting |
| Executor: certificate does not match the pinned one | the console's certificate was renewed (or it is another device): read the new fingerprint and update `cert_sha256` |
| Executor: account cannot write | the executor's account has the view-only role; give it *Site Admin* |

---

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.
