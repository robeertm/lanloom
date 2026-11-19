# FRITZ!Box (local API): read adapter

Lanloom reads a FRITZ!Box (the home router, with or without mesh repeaters) over **TR-064**, the box's documented local interface, with **an account made only for Lanloom**. Nothing is ever changed on the box from Lanloom in this release: the adapter can send exactly one kind of `POST` - a SOAP call that is a row of a fixed table of `Get` actions - and `GET` of the two documents those calls hand out. A request outside that table is refused before it leaves Lanloom: no `Set`, no `Add`, no `Delete`, no reconnect, no reboot, no firmware action, no change of any setting (no port forwarding, no guest network, no password).

What you get:

- **Hosts:** every device the box knows - name, address, wired or wireless, active now or only remembered. Active devices feed the network view and the inventory.
- **Mesh:** where each device hangs - on the box or on which repeater, on which LAN port of it, on which radio band (2.4, 5 or 6 GHz). This is the part a plain host list cannot tell, and the reason for the family: behind a FRITZ!Box that Lanloom does not otherwise see into, the network view shows the real tree. Repeaters and other mesh devices appear as network equipment with model, firmware and state.
- **The box itself:** model, firmware, uptime, the home network with its address range and DHCP, and the state of the internet connection.
- **Checks** in plain words: the internet connection is down, a mesh device is not connected.
- **Night report:** the family contributes its own section (mesh devices that went offline, came back, joined or left, firmware changes of the box or a mesh device, devices the box had not known at the last report, and the checks above); it appears only when this connection exists.

Never kept: the serial number, the device log (the reply that carries it is read and dropped), passwords and keys, the external address, guest network and VPN details. Hardware addresses are only used to match devices in the topology; in the configuration view every object has an opaque id.

## Create a user for Lanloom

1. Open the box's web page and sign in. Under **Home Network → Network → Network Settings** allow **access for applications** (TR-064). Without it the box answers nothing on the TR-064 port.
2. Under **System → FRITZ!Box Users** add a user just for Lanloom (for example `lanloom`) with a long password of its own, **with access from the home network only** (not from the internet).
3. Give it the right **FRITZ!Box settings** - TR-064 needs it for the host list and the mesh. The box has no read-only user. What keeps Lanloom from changing anything is Lanloom's own side (the allow-list below), not the box's rights; so never use your own account and never reuse its password.

Then in Lanloom: **Settings → Connections → + FRITZ!Box (local API)**.

Lanloom cannot check the rights of the account (TR-064 does not tell them), so it reports the account scope as unknown.

## Settings

| field | meaning |
|---|---|
| Address | `http://<box>` (for example `http://192.168.178.1`, or the name the box answers to in your home network). Without a port Lanloom uses `49000` for `http` and `49443` for `https` |
| User name | the user from step 2 |
| Password | its password; stored encrypted, never shown again |
| Accept a self-signed certificate | needed for `https` (the box uses its own certificate); plain `http` inside the home network needs nothing |

## What Lanloom sends

Exactly one code path sends requests, through an allow-list that is checked before anything leaves the app (`lanloom/integrations/fritzbox/read.py`, `ALLOW`):

- `POST` to the control path of one service with a SOAP call from this table, each without arguments: `X_AVM-DE_GetHostListPath` and `X_AVM-DE_GetMeshListPath` (service Hosts), `GetInfo` (DeviceInfo, LANHostConfigManagement, LANEthernetInterfaceConfig), `GetStatusInfo` (WANIPConnection, WANPPPConnection). Lanloom builds the envelope itself from the checked row.
- `GET` of the host list (XML) and the mesh topology (JSON) at the path the box handed out; the path must match one exact pattern (`hostlist.lua` or `meshlist.lua` with a session ID).

The box asks for HTTP digest authentication on the calls; Lanloom answers the challenge and reuses it. The box delays sign-ins after wrong passwords, so a refused sign-in pauses every further attempt for **15 minutes**, and sign-ins answered from a fresh challenge are capped at 60 an hour.

Cadence: every minute the hosts and the mesh, every five minutes everything else.

## Releases

| release | reads |
|---|---|
| FRITZ!OS 7.x with TR-064 and the mesh list | everything above; field names checked against the published interface descriptions and the published mesh topology schema, not yet against a running box |
| Releases without the mesh list | hosts, box information and internet state; devices all hang on the box in the network view (no repeater tree), the family reports it |
| Boxes with TR-064 switched off | not readable; the connection test says so |

An area a release does not have, or an action the account may not call, is left out and the others still show. Tested versions are listed in [COMPATIBILITY.md](../COMPATIBILITY.md).

## Troubleshooting

| status | what to do |
|---|---|
| Account was rejected | user name or password wrong, or the user may not sign in from the home network; Lanloom pauses for 15 minutes and does not try again meanwhile |
| Unexpected answer | TR-064 is not switched on (see step 1), or the address is not the box (check the name or address and the port) |
| Access denied for this account | the user lacks the right *FRITZ!Box settings* |
| Settings incomplete or invalid | address or user name missing or not valid |
| Not reachable | check the address; the TR-064 port is `49000` (`49443` for `https`) and the box answers it only inside the home network |
| TLS / certificate problem | `https` uses the box's own certificate: tick *Accept a self-signed certificate*, or use `http` inside the home network |
| Sign-in paused for a while | a refused sign-in: fix the password, Lanloom tries again after the pause |
| Devices do not hang below their repeater | the release has no mesh list, or the repeater is not part of the mesh; the host list alone cannot tell |

## Help us test

If you run Lanloom against a real box, the most useful feedback is: the FRITZ!OS version, whether the hosts, the repeaters and the place of each device match what the box shows in its home network overview, an area that is missing or looks wrong, and the status line of the connection. Before you share a screenshot or a log, black out public addresses, host names and names you consider private.

---

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.
