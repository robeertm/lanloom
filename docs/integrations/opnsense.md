# OPNsense (local API): read adapter and rule changes

Lanloom reads an OPNsense firewall over its REST API with an **API key of a user made only for Lanloom**. The read half sends `GET` requests to a fixed list of paths and nothing else - no `POST` at all, so it cannot change, apply, reboot or update anything. Changing firewall rules is a separate, optional step: it runs only in the [executor](../FIREWALL-EDITOR.md#turning-on-the-external-executor) with a key of its own, after the gate and your passkey signature - see [Changing rules](#changing-rules-stage-b) below.

What you get:

- **Firewall tab, live and read only:** every assigned interface is a zone (with its network, VLAN and address), interface groups are zones of their own and the firewall itself is the *This firewall* zone. A rule on an interface becomes a rule from that interface's zone to the zone of its destination (another interface's network, the firewall itself, an address inside a known network, or any). You see the zones as a matrix (derived from the rules in the order the firewall evaluates them: floating rules, group rules, interface rules, each by sequence), every rule as a sentence with addresses, ports and schedule, port forwarding, and a table of source NAT, NPT and 1:1 NAT, static routes, VLANs and interface groups.
- **Classic rules** (Firewall → Rules → *interface*): releases from 25.1 list them in the search of the rule API, so they appear too, marked as classic and never changed by Lanloom. Earlier releases do not offer them through the API; there Lanloom shows the rules of the rule API (Firewall → Automation → Filter) only.
- **Fixed addresses** of the address services (Kea DHCP reservations, Dnsmasq hosts), **WAN links** with their gateways, **VPN instances** (WireGuard, IPsec connections, OpenVPN) with name and state.
- **Checks** in plain words: managing the firewall (HTTPS, SSH, HTTP) is allowed from a WAN interface, a rule lets everything from a WAN interface into an inside network with every port and no target limit, an enabled rule allows any to any, rules that are switched off, a gateway that is down.
- **Night report:** the family contributes its own section (rules added, removed, switched on or off, or acting differently since the last report; port forwarding added or removed; the checks above); it appears only when this connection exists.
- **Setup assistant:** after the gentle scan it suggests the connection for a device whose web page on port 443 has the firewall's login title, with the step-by-step guide below.

Never kept: API keys and secrets, private keys of VPN instances, pre-shared keys, certificates, passwords of dial-up links, users and groups. Some replies carry such values (the private key of a WireGuard instance, the password of a PPPoE link in an interface's settings): Lanloom drops every field whose name says it holds a key, a secret, a password or a certificate right when the reply arrives, and takes only the fields it names from the rest. Hardware addresses of fixed addresses are only used inside an id hash.

## Create a read-only key

The firewall grants privileges **per page**, and a page's privilege allows reading and writing alike - the API cannot be limited to reading on the firewall's side. Lanloom therefore asks for a user with only the pages it reads; its own side sends nothing but `GET` in any case.

1. **System → Access → Users → Add**: user name `lanloom-read`, a long random password, login shell *none*. Save.
2. In the user's **Effective privileges**, add only these (the search field finds them; names can differ slightly between releases): *Firewall: Automation: Filter* (on newer releases *Firewall: Rules*), *Firewall: Alias*, *Firewall: Groups*, *Firewall: NAT: Source*, *Firewall: NAT: Destination* (26.1 and later), *Interfaces: Overview*, *Interfaces: VLAN*, *Services: Kea DHCP*, *Services: Dnsmasq*, *System: Routes*, *System: Gateways*, *VPN: WireGuard*, *VPN: IPsec*, *VPN: OpenVPN*, *Diagnostics: System information* (tells Lanloom the release). Pages you leave out are simply left out of what Lanloom shows.
3. Under **API keys** of the same user press **+**; the browser downloads a file with `key` and `secret`. Keep it safe; the secret is shown only once.

Then in Lanloom: **Settings → Connections → + OPNsense (local API)**.

## Settings

| field | meaning |
|---|---|
| Address | `https://<firewall>` (add the port if the web interface does not run on 443) |
| API key | `key` from the downloaded file |
| API secret | `secret` from the downloaded file; stored encrypted, never shown again |
| Accept a self-signed certificate | needed unless the firewall has a certificate your system trusts (a fresh installation uses its own) |

## What Lanloom sends

Exactly one code path sends requests, through an allow-list that is checked before anything leaves the app: `GET` on these paths, nothing else (`lanloom/integrations/opnsense/read.py`, `PATHS`):

`/api/diagnostics/system/systemInformation`, `/api/core/firmware/status`, `/api/interfaces/overview/interfacesInfo`, `/api/firewall/filter/get`, `/api/firewall/filter/searchRule`, `/api/firewall/group/searchItem`, `/api/firewall/alias/get`, `/api/interfaces/vlan_settings/searchItem`, `/api/firewall/source_nat/searchRule`, `/api/firewall/d_nat/searchRule`, `/api/kea/dhcpv4/searchReservation`, `/api/dnsmasq/settings/searchHost`, `/api/routes/routes/searchroute`, `/api/routes/gateway/status`, `/api/wireguard/server/searchServer`, `/api/ipsec/connections/searchConnection`, `/api/openvpn/instances/search`.

Cadence: one poll every five minutes reads every path once. A path the release does not have (404) or the key may not read (403) is left out and asked again after an hour; the other areas still show. A refused key (401) pauses the connection for 15 minutes, so a wrong secret does not flood the firewall's log.

## Changing rules (stage B)

With the executor set up for this family, the zone firewall in the Firewall tab's *Change* mode can, for this firewall:

| change | how it is written |
|---|---|
| add a rule between two interfaces (or to the firewall itself) | `POST /api/firewall/filter/addRule` with a complete rule: one interface, direction in, IPv4, the source is that interface's network, the target another interface's network or `(self)`, protocol, one port or one range, on/off, and the description starting with `[lanloom]` (Lanloom's mark). It is appended after the existing rules (sequence one above the highest). |
| change a rule | `POST /api/firewall/filter/setRule/<uuid>` with only the changed fields: description, action, protocol, destination port, interface and source, target, on/off. Only **simple** rules: one interface, direction in, no aliases or address lists, no schedule, no policy routing, no inverted match, no source port. |
| switch a rule off or on | the same request with `enabled` only |
| remove a rule | `POST /api/firewall/filter/delRule/<uuid>`, only for rules whose description starts with `[lanloom]` |

Never, whatever a plan says: classic rules and automatic rules (anti-lockout and others), floating and group rules, NAT, aliases, interfaces, VLANs, address services, VPN, users, system settings, updates and reboots. Rules towards a WAN interface's network are not written either (a target on the internet side needs aliases, which Lanloom does not touch). The executor's allow-list (`lanloom/integrations/opnsense/write.py`, `ALLOW`) checks every field and value before a request leaves: an interface name, an interface's network or address, `any`, `(self)`, a port or one range, and a description without control characters.

Writes go into the configuration first; the plan becomes live with **one** apply at the end, so a plan that fails half-way never puts half a rule set live. The neutral rules of the zone firewall apply as for every family: no blocking rule on the firewall itself or the management zone, the path from the executor to the firewall must stay open after the plan, at most 10 objects per plan.

### Way back per release

Two layers:

1. **Lanloom's own** (every release): before the first write the executor reads every touched rule and keeps it. A failed step, a failed check after the apply, or a watch check that fails twice writes the kept rules back (rules the plan created are deleted, a removed own rule comes back) and applies once.
2. **The firewall's own** (only where the release has it): a savepoint before the first write, then `apply/<revision>` starts a timer on the firewall that puts the rules of the savepoint back after 60 seconds by itself. The executor reads every changed rule back and sends `cancelRollback/<revision>` only when all are as planned (and within 45 seconds). If the executor dies or loses its way to the firewall in between, the firewall reverts on its own.

| release | firewall's own way back | what the executor does |
|---|---|---|
| 23.1 – 23.7 (rule API from the plug-in *os-firewall*) | yes | savepoint, `apply/<revision>`, confirmation |
| 24.1 – 26.1 (rule API built in) | yes | savepoint, `apply/<revision>`, confirmation |
| 26.7 and later | no (the release dropped the savepoint) | `apply` only; Lanloom's own backup is the way back; the capability list says `device_native: false` |
| unknown release (the key may not read the release) | as the firewall answers | asks for a savepoint and takes the answer |

### Create the write key (executor only)

1. **System → Access → Users → Add** a second user, `lanloom-write`, login shell *none*.
2. Effective privileges: only *Firewall: Automation: Filter* (on newer releases *Firewall: Rules*), plus *Interfaces: Overview*, *Firewall: Groups*, *Firewall: Alias* and *Diagnostics: System information* for the fresh reads before every change.
3. **API keys → +**, keep the file.

Executor options file (`LANLOOM_EXECUTOR_ADAPTER=opnsense`, `LANLOOM_EXECUTOR_ADAPTER_OPTIONS=<path>`, mode 0600, owned by the executor's user):

```json
{"url": "https://192.168.1.1", "write_api_key": "…", "write_api_secret": "…",
 "cert_sha256": "<SHA-256 of the firewall's certificate>"}
```

`cert_sha256` binds the connection to the firewall's own certificate (`openssl s_client -connect <firewall>:443 </dev/null | openssl x509 -outform der | sha256sum`); `allow_self_signed: true` alone works too, but without that binding. Arming, passkey keys and the rest of the executor set-up: [FIREWALL-EDITOR.md](../FIREWALL-EDITOR.md#turning-on-the-external-executor).

## Releases

| release | reads | changes (executor) |
|---|---|---|
| 26.7 | everything above, classic rules (read only), destination NAT, Dnsmasq hosts | rules; Lanloom's own way back only |
| 26.1 | as 26.7 | rules; with the firewall's own way back |
| 25.1 – 25.7 | as 26.1 without destination NAT (no API for it yet) | rules; with the firewall's own way back |
| 24.1 – 24.7 | rules of the rule API, interfaces, groups, aliases, source NAT, Kea reservations (24.7), routes, gateways, VPN instances; no classic rules through the API | rules; with the firewall's own way back |
| 23.x | as 24.1 where the plug-in *os-firewall* is installed; the interface overview is missing (interface names come from the rule API) | rules (plug-in); with the firewall's own way back |

Checked against the source and the API reference of each release and against a stateful stand-in in the adapter tests; not yet against a running firewall. Tested versions are listed in [COMPATIBILITY.md](../COMPATIBILITY.md).

## Troubleshooting

| status | what to do |
|---|---|
| API key was rejected | key or secret wrong, or the user is disabled; Lanloom pauses for 15 minutes |
| Access denied for this account | the user has none of the pages listed in step 2 |
| Not reachable | check the address and port of the web interface and that the firewall answers from Lanloom's network |
| TLS / certificate problem | tick *Accept a self-signed certificate* |
| An area is empty (VPN, fixed addresses, port forwarding) | the page is not among the user's privileges, the service is not used, or the release has no API for it (destination NAT before 26.1) |
| Rules show "?" as a zone | the user may not read the interface overview; rules still show, interface names are missing |
| A rule cannot be changed in *Change* mode | it is not a simple rule (alias, address list, schedule, floating or group rule, classic rule); change it on the firewall |

## Help us test

If you run Lanloom against a real firewall, the most useful feedback is: the release, whether the zone matrix matches what you expect, rules that look wrong or are missing, and the status line of the connection. Before you share a screenshot or a log, black out public addresses, host names and alias names you consider private.

---

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.
