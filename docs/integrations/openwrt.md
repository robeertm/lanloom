# OpenWrt (local ubus API): read adapter and rule changes

Lanloom reads an OpenWrt router over its ubus JSON-RPC endpoint (`POST /ubus`, served by `uhttpd` and `rpcd`, the same endpoint the web interface uses) with a **login made only for Lanloom** whose access list allows reading. The read half sends calls from a fixed allow-list and nothing else; it cannot change, apply, reboot or install anything, and the app refuses a login whose access list allows any change. Changing firewall rules is a separate, optional step: it runs only in the [executor](../FIREWALL-EDITOR.md#turning-on-the-external-executor) with a login of its own, after the gate and your passkey signature - see [Changing rules](#changing-rules-stage-b) below.

What you get:

- **Firewall tab, live and read only:** every firewall zone is a zone (with its networks), a zone that masquerades or is named like a WAN is an external zone, and the router itself is the *This router* zone. Rules are read in configuration order (a rule without a destination goes to the router, a rule without a source comes from the router, a destination of `*` means any zone), followed by the zone forwardings (allow A to B, built in) and each zone's input policy (zone to router, built in). Pairs of zones that nothing decides follow the firewall's forward default. You see the zones as a matrix, every rule as a sentence, port forwards (redirects with target DNAT), and a table of rules that are only shown: MARK, NOTRACK and HELPER rules, source NAT redirects, includes, bridge VLANs and static routes.
- **Fixed addresses** (host sections of the address service), **WAN links** (interfaces of an external zone).
- **Wireless networks:** every wireless interface in access point mode with band, security (open, WEP, WPA personal, WPA enterprise, OWE), hidden or not, switched on or off, and the clients on air.
- **Clients** from address leases, the web interface's host hints and the associated wireless clients; wireless clients hang below the router in the topology.
- **Checks** in plain words: managing the router is allowed from the internet side (`admin_on_wan`), a rule lets everything from the internet side into an inside zone (`wan_inbound_open`), an enabled rule allows any to any (`any_any`), the forward default is accept (`forward_accept`), rules that are switched off (`rules_disabled`), an open wireless network (`ssid_open`), a WAN that is down (`wan_down`).
- **Night report:** the family contributes its own section *Router* (rules and forwardings added, removed, switched or acting differently; port forwards added or removed; wireless networks new, gone, switched on or off, or with another security); it appears only when this connection exists.
- **Setup assistant:** after the gentle scan it suggests the connection for a device whose web page has the router's web interface title, with the step-by-step guide below.

Never kept: wireless keys, dial-up (PPPoE) passwords, WireGuard private and pre-shared keys, modem PINs, certificates, and any other option whose name says it holds a secret. Lanloom drops these right when the reply arrives and takes only the fields it names from the rest. Hardware addresses of fixed addresses are only used inside an id hash.

## Create a read-only login

The router grants rights per access list in `/usr/share/rpcd/acl.d/`. Create the list in `/usr/share/rpcd/acl.d/lanloom-read.json`:

```json
{"lanloom-read": {"description": "Lanloom: read only",
  "read": {"ubus": {"system": ["board", "info"], "uci": ["get"], "network.interface": ["dump"], "network.device": ["status"],
                     "luci-rpc": ["getHostHints", "getDHCPLeases"], "iwinfo": ["devices", "info", "assoclist"]},
           "uci": ["network", "firewall", "dhcp", "wireless"]}}}
```

Then add the login to `/etc/config/rpcd`:

```
config login
	option username 'lanloom'
	option password '$1$...'
	list read 'lanloom-read'
```

Create the password hash with `uhttpd -m 'your-password'` and paste its output as `password`. Apply with `/etc/init.d/rpcd restart`.

Calls of `luci-rpc` need the web interface's helper package (`rpcd-mod-luci` or the web interface itself); without it Lanloom reads clients from the wireless state only. `iwinfo` needs the wireless packages; a router without wireless simply shows no wireless networks.

Then in Lanloom: **Settings → Connections → + OpenWrt (local API)**.

## Settings

| field | meaning |
|---|---|
| Address | `https://<router>` (use `http://` only on a trusted wire; Lanloom warns) |
| User name | `lanloom` |
| Password | the password you hashed above; stored encrypted, never shown again |
| Accept a self-signed certificate | needed unless the router has a certificate your system trusts |

### HTTPS

The default web server answers on plain HTTP. Install the TLS package of the web interface (`luci-ssl`, or `uhttpd` with `px5g`/`libustream` and a certificate) so that `https://<router>` works. A fresh certificate is self-signed, so tick *Accept a self-signed certificate* or, better, pin it in the executor with `cert_sha256` (below).

## What Lanloom sends

Exactly one code path sends requests, through an allow-list that is checked before anything leaves the app: JSON-RPC calls on `/ubus`, each checked for object, method and arguments (`lanloom/integrations/openwrt/read.py` and `model.py`, `call_check`):

- the session login;
- `system board` and `system info`;
- `uci get` of the configurations `network`, `firewall`, `dhcp` and `wireless`;
- `network.interface dump` and `network.device status`;
- `luci-rpc getHostHints` and `luci-rpc getDHCPLeases` (optional);
- `iwinfo devices`, `info` and `assoclist`.

Cadence and limits: one poll every five minutes. The session lasts 15 minutes and is reused; Lanloom signs in at most four times an hour. A call the access list leaves out or a package that is not installed is skipped and asked again after an hour; the other areas still show. A refused password pauses the connection for 15 minutes, so a wrong password does not flood the router's log. At sign-in the router returns the login's access list (`acls`); if it allows any change, Lanloom refuses the login (`account_scope`, capability `can_write`).

## Changing rules (stage B)

With the executor set up for this family, the zone firewall in the Firewall tab's *Change* mode can, for this router:

| change | how it is written |
|---|---|
| add a rule | `uci add firewall rule` as a named section `lanloom_<12 hex>` with a complete rule: source zone, destination zone (a zone, the router itself or any), action allow, block or reject (`ACCEPT`, `DROP`, `REJECT`), protocol any, tcp, udp, tcp+udp or icmp, one port or one range, on/off, and the name starting with `[lanloom]` (Lanloom's mark). It is appended after the existing rules. |
| change a rule | `uci set` of only the changed options: name, source, destination, protocol, destination port, target, on/off. Only **simple** rules: one source zone, no addresses, marks, schedules or limits. The name changes only for Lanloom's own rules and for rules that already carry a name. |
| switch a rule off or on | the same call with `enabled` only |
| remove a rule | `uci delete` of the whole section, only for sections named `lanloom_...` |

Never, whatever a plan says: interfaces, zones, forwardings, redirects, wireless, the address service, users, system settings, updates and reboots. The executor's allow-list (`lanloom/integrations/openwrt/write.py`) checks every call, field and value before it leaves: `uci add` (configuration `firewall`, type `rule`, a `lanloom_` section name, complete values with the mark), `uci set` (the rule options above), `uci delete` (a whole section only when named `lanloom_...`; otherwise only the options `dest` and `dest_port`), `uci changes` and `uci revert` of `firewall`, `uci apply` (with `rollback` and a timeout of 90 to 600 seconds, or without), `uci confirm`, `uci rollback`; reads `uci get` of `firewall` and `network`, `system board` and `network.interface dump`.

The changes are staged in the session first, so nothing is live until the one `uci apply` at the end; a plan that fails half-way never puts half a rule set live. The neutral rules of the zone firewall apply as for every family: no blocking rule on the router itself or the management zone, the path from the executor to the router must stay open after the plan, at most 10 objects per plan.

### Stable references

UCI names an anonymous section at load time from its position and content (`cfgXXXXXX`), so that name changes after every commit. Lanloom therefore refers to an unnamed rule by its `name` option and its position among rules with the same name. A rule without a name is shown, never changed, and cannot be renamed through Lanloom. Rules Lanloom creates are named sections, `lanloom_<12 hex>`, with a name starting with `[lanloom]`.

### Way back

Two layers:

1. **Lanloom's own:** before the first change the executor reads every touched rule and keeps it. Because changes are staged in the session, trouble before the apply only discards them (`uci revert`); trouble after it writes the kept rules back (rules the plan created are deleted, a removed own rule comes back) and applies.
2. **The router's own:** the executor sends `uci apply` with `rollback: true` and `timeout: 90` (rpcd has this since 18.06). The router keeps the previous configuration and puts it back by itself unless the same session sends `uci confirm`. The executor reads every changed rule back and sends `uci confirm` only when all are as planned and within 60 % of the timeout; if it finds trouble first it sends `uci rollback` (immediate). If the executor dies or loses its way to the router in between, the router reverts on its own after 90 seconds. While another apply is pending, the executor refuses to start one.

### Create the write login (executor only)

Create `/usr/share/rpcd/acl.d/lanloom-write.json`:

```json
{"lanloom-write": {"description": "Lanloom: firewall rules",
  "read": {"ubus": {"system": ["board"], "uci": ["get"], "network.interface": ["dump"]},
           "uci": ["network", "firewall"]},
  "write": {"ubus": {"uci": ["add", "set", "delete", "changes", "revert", "apply", "confirm", "rollback"]},
            "uci": ["firewall"]}}}
```

and a second login in `/etc/config/rpcd`:

```
config login
	option username 'lanloom-write'
	option password '$1$...'
	list read 'lanloom-write'
	list write 'lanloom-write'
```

Restart `rpcd` again. This login is used only by the executor; the app never sees it.

Executor options file (`LANLOOM_EXECUTOR_ADAPTER=openwrt`, `LANLOOM_EXECUTOR_ADAPTER_OPTIONS=<path>`, mode 0600, owned by the executor's user):

```json
{"url": "https://192.168.1.1", "write_username": "lanloom-write", "write_password": "…",
 "cert_sha256": "<SHA-256 of the router's certificate>"}
```

`cert_sha256` binds the connection to the router's own certificate:

```
openssl s_client -connect 192.168.1.1:443 </dev/null | openssl x509 -outform der | sha256sum
```

`allow_self_signed: true` alone works too, but without that binding. An `http://` address works, but the executor warns: the password and every change then cross the wire in the clear. Arming, passkey keys and the rest of the executor set-up: [FIREWALL-EDITOR.md](../FIREWALL-EDITOR.md#turning-on-the-external-executor).

## Releases

| release | firewall | reads | changes (executor) |
|---|---|---|---|
| 21.02 | fw3 (iptables) | everything above | rules; with the router's own way back |
| 22.03, 23.05, 24.10 | fw4 (nftables) | everything above | rules; with the router's own way back |

Checked against the behaviour of the rpcd and uhttpd source and against a stateful stand-in of rpcd's `uci` object (session staging, apply, rollback and confirm timer, renaming of anonymous sections after a commit) in the adapter tests; not yet against a real router. Tested versions are listed in [COMPATIBILITY.md](../COMPATIBILITY.md).

## Troubleshooting

| status | what to do |
|---|---|
| Login was rejected | user name or password wrong; Lanloom pauses for 15 minutes |
| Access denied for this account | the access list lacks the calls above, or the login is not in `/etc/config/rpcd`; restart `rpcd` |
| This login can change settings | the access list has a `write` part; use a read-only list, Lanloom refuses such a login |
| Not reachable | check the address and that `uhttpd` answers on `/ubus` from Lanloom's network |
| TLS / certificate problem | tick *Accept a self-signed certificate* |
| No clients from leases | the web interface's helper package is missing; clients then come from the wireless state |
| No wireless networks | no wireless packages, or no interface in access point mode |
| A rule cannot be changed in *Change* mode | it is not a simple rule (marks, addresses, schedule, several source zones) or it has no name; change it on the router |
| A change was reverted after 90 seconds | the executor could not confirm in time or a check failed; the router put the rules back by itself |

## Help us test

If you run Lanloom against a real router, the most useful feedback is: the release, whether the zone matrix matches what you expect, rules that look wrong or are missing, and the status line of the connection. Before you share a screenshot or a log, black out public addresses, host names and names you consider private.

---

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.
