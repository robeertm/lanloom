# MikroTik RouterOS (local API): read adapter

Lanloom reads a router that runs RouterOS 7.1 or later over its **REST interface** with a **user of its own whose group may only read**. Nothing is ever changed on the router from Lanloom in this release: the adapter sends `GET` requests on the paths listed below and nothing else. The router's REST interface also accepts changes and commands (restart, firmware, user management) on the very same paths through other methods; Lanloom refuses every one of them before it leaves the app - there is not even a sign-in request, because the user name and password travel with each request.

What you get:

- **The router itself:** name, model, RouterOS release, load, memory, uptime and the address it is reached at, as a device of the network view. A router that started again since the last report is noted in the night report.
- **Neighbouring network equipment** that the router sees through its discovery protocols (other routers, switches, access points): name, model, release, address and the port it hangs on.
- **Ports:** link, speed, power over Ethernet (on, powered), traffic counters, the bridge a port belongs to, its untagged VLAN and the VLANs tagged on it (from the bridge's VLAN table), which port carries the internet line and which one leads to a neighbouring device - for the front panel of the router card.
- **Networks:** every address on a LAN-side interface as a network with address range, VLAN (from VLAN interfaces and the bridge's own VLAN) and DHCP range; the network that holds the neighbouring equipment is marked as the management network.
- **Clients:** from DHCP leases, the address table and the bridge host table: wired clients with their port, wireless clients with wireless network and band (both wireless packages of RouterOS 7 are understood); static leases become fixed-address entries.
- **Port forwarding:** destination NAT rules with the outside port, the inside address and port, on/off and whether they are limited to a source.
- **Internet connections:** default routes with the DHCP and PPPoE clients behind them, with their state; a second line with a larger distance is shown as the reserve line.
- **Filter rules:** shown in the router's own order with action and state, never written. The night report notices when one is added, removed, switched on or off, or acts differently.
- **Checks** in plain words: an internet connection is down, no enabled rule on the input chain drops unwanted traffic (the router may be reachable from the internet side), rules are switched off.
- **Night report:** the family contributes its own section (restart, release change, neighbours that appeared or vanished, internet connections that went down or came back, filter rules and port forwarding that changed, devices the router had not known, and the checks above); it appears only when this connection exists.

Never kept: passwords, keys, PPPoE secrets and any reply field whose name says it carries a secret. Hardware addresses are only used to match devices in the topology; in the configuration view every object has an opaque id.

## Create a read-only user for Lanloom

You need the secure web service of the router, a user group that only reads, and a user in that group. The menu names are those of RouterOS 7 (WebFig or the terminal).

1. Check **IP → Services**: the service `www-ssl` must be enabled (port 443; give it a certificate, or accept a self-signed one in Lanloom). A plain `www` service works too but sends the password unencrypted: use it only on a network you fully trust. Restrict the services to your home network (*Available From*).
2. Create a group under **System → Users → Groups**, for example `lanloom-read`, with exactly these policies ticked: `read`, `api` and `rest-api`. Everything else stays off, especially `write`, `policy`, `reboot` and `ftp`.
3. Create a user under **System → Users** just for Lanloom, in that group, with a long password of its own and, under *Allowed Address*, the address of the computer Lanloom runs on. Never use your own administrator account.

Then in Lanloom: **Settings → Connections → + MikroTik RouterOS (local API)**.

Lanloom asks the router for the policy list of the user's group and **refuses a user whose group can change anything** (any of the policies named in step 2). If the router does not let the user read its user list, Lanloom cannot check this and accepts the user - the group in step 2 is then what keeps the user read only on the router. Lanloom's own side cannot send a change in any case.

## Settings

| field | meaning |
|---|---|
| Address | `https://<router>` (the `www-ssl` service; another port as `https://<router>:8443`) or `http://<router>` |
| User name | the user from step 3 |
| Password | its password; stored encrypted, never shown again |
| Accept a self-signed certificate | needed unless the router has a certificate your system trusts (a router uses none or its own by default) |

## What Lanloom sends

Exactly one code path sends requests, through an allow-list that is checked before anything leaves the app: `GET` on these paths, with the user name and password as Basic authentication, and nothing else (no `POST`, `PUT`, `PATCH` or `DELETE`, no query string):

- the router: `/rest/system/resource`, `/rest/system/identity`, `/rest/system/routerboard`;
- interfaces: `/rest/interface`, `/rest/interface/ethernet`, `/rest/interface/ethernet/poe`, `/rest/interface/bridge`, `/rest/interface/bridge/port`, `/rest/interface/bridge/vlan`, `/rest/interface/bridge/host`, `/rest/interface/vlan`, `/rest/interface/pppoe-client` and the wireless registration tables of the two wireless packages;
- addresses and services: `/rest/ip/address`, `/rest/ip/pool`, `/rest/ip/dhcp-server`, `/rest/ip/dhcp-server/lease`, `/rest/ip/dhcp-client`, `/rest/ip/arp`, `/rest/ip/neighbor`, `/rest/ip/route`;
- rules: `/rest/ip/firewall/filter` and `/rest/ip/firewall/nat`;
- the connection test only: `/rest/user` and `/rest/user/group` (the policies of the user's own group).

The paths are in `lanloom/integrations/mikrotik/read.py`. Cadence: every minute the state of ports, routes, clients, neighbours and wireless registrations, every five minutes everything (addresses, DHCP servers, bridge settings, VLAN table, firewall and NAT rules). A path the router does not have (an older release, a package that is not installed) or the user may not read is left out for an hour; the others still show, and the capability list of the connection shows which. After a refused sign-in Lanloom waits 15 minutes, so a wrong password cannot flood the router's log.

## Releases

| release | reads |
|---|---|
| RouterOS 7.x with the REST interface (7.1 and later) | everything above; field names checked against the published documentation, not yet against a running router |
| RouterOS 6.x | not supported (no REST interface) |

Tested versions are listed in [COMPATIBILITY.md](../COMPATIBILITY.md).

## What this release does not do

It does not change anything on the router. Writing would need a way back that the router does not offer over its API: its safe mode works only in an interactive session, not through REST. A later stage would therefore have to bring its own fallback (for example a scheduled reset script that the executor removes after a successful check) and be tested against a router image first; it is not part of this release.

## Troubleshooting

| status | what to do |
|---|---|
| Account was rejected | user name or password wrong, or the user's group lacks the `rest-api` policy; Lanloom pauses for 15 minutes |
| This account can change settings | the user's group has `write`, `policy`, `reboot` or `ftp`; use a group with `read`, `api` and `rest-api` only |
| Access denied for this account | the group may not read the interface list or any page Lanloom needs; add the policy `read` |
| Unexpected answer | the address is not the router, or its `www-ssl` (or `www`) service is off or on another port |
| Settings incomplete or invalid | address, user name or password missing or malformed |
| Not reachable | check address and port; the service must be reachable from the computer Lanloom runs on (*Available From* of the service) |
| TLS / certificate problem | tick *Accept a self-signed certificate* |
| Sign-in paused for a while | too many refused sign-ins; fix the password, Lanloom tries again after the pause |
| Clients or rules are missing | the release or the user may not read that page; the status of the connection lists the areas that are missing |

## Help us test

If you run Lanloom against a real router, the most useful feedback is: the RouterOS release and the router model, whether the ports, VLANs, clients and rules match what the router shows, an area that is missing or looks wrong, and the status line of the connection. Before you share a screenshot or a log, black out public addresses, host names and names you consider private.

---

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.
