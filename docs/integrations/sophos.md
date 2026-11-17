# Sophos Firewall (local API): read adapter

Lanloom reads a Sophos Firewall (SFOS) over its local XML API with a **dedicated administrator account whose profile only allows reading**. Nothing is ever changed on the firewall from Lanloom in this release: the adapter can only send a query document that holds the account and a list of objects to *get*; a document with anything else (set, remove, any other element) is refused before it leaves Lanloom.

What you get:

- **Firewall tab, live and read only:** the zones as a matrix (which zone may reach which, derived from the rules in their order; traffic no rule allows is dropped), every firewall rule in order with action, zones, addresses, services and schedule, grouped by zone pair, user rules (identity-based) marked as such, port forwarding (NAT rules that translate the destination), and a table of internet connections, VPN connections (IPsec, SSL VPN), fixed addresses of the DHCP servers, source NAT, static routes and rule groups.
- **Networks** of the firewall's interfaces, VLANs and aliases with their zone, address range and whether a DHCP server serves them.
- **Checks** in plain words: managing the firewall (HTTPS, SSH) is allowed from a WAN zone, a rule lets everything from a WAN zone in with every service and no target limit, an enabled rule allows every zone to every zone with every service, rules that are switched off.
- **Night report:** the family contributes its own section (rules added, removed, switched on or off, or acting differently since the last report; port forwarding added or removed; the checks above); it appears only when this connection exists.

Never kept: passwords, pre-shared keys, certificates, user and group lists. The adapter does not ask for users, certificates or the administrator list at all. Two of the objects it reads can carry a secret in the firewall's reply - the pre-shared key of an IPsec connection and the PPPoE password of an interface (recent releases send them encrypted). Lanloom takes only name, type and state of a connection and the address settings of an interface; the rest of the reply is dropped in the same step and never stored, logged or shown. Object names become opaque ids; hardware addresses of fixed DHCP addresses are only used inside an id hash.

## Create a read-only account

You need three things on the firewall: the API switched on for Lanloom's address, a profile that can only read, and an administrator with that profile. The menu names below are those of SFOS 19 to 21; older releases use similar names.

1. **Profiles → Device access → Add**: name `Read only`. In the header row choose **Read-only** for every area (the column sets all lines at once). Save.
2. **Authentication → Users → Add**: user name `lanloom-read`, user type **Administrator**, profile **Read only**, a long random password. Do not use your own administrator account.
3. **Backup & firmware → API**: tick **API configuration**, and under **Allowed IP address** add the address of the computer Lanloom runs on. Apply.
4. **Administration → Device access**: the zone Lanloom is in (usually LAN) needs **HTTPS** under *Administrative services* - the API answers on the admin port (4444 by default).

Then in Lanloom: **Settings → Connections → + Sophos Firewall (local API)**.

The API does not tell Lanloom which rights an account has, so Lanloom cannot check the account the way it does for families whose API reports it - the profile in step 1 is what keeps the account read only on the firewall. Lanloom's own side cannot send a change in any case.

## Settings

| field | meaning |
|---|---|
| Address | `https://<firewall>:4444` (the admin port; change it if you moved the web admin console) |
| User name | the administrator from step 2, for example `lanloom-read` |
| Password | its password; stored encrypted, never shown again |
| Accept a self-signed certificate | needed unless the firewall has a certificate your system trusts (a fresh installation uses its own) |

## What Lanloom sends

Exactly one code path sends requests, through an allow-list that is checked before anything leaves the app: a `POST` to `/webconsole/APIController` with one form field `reqxml`, whose document must be `<Request><Login>user, password</Login><Get>…</Get></Request>` with only the objects listed in `lanloom/integrations/sophos/read.py` (`READ_ENTITIES`), each once and empty. Nothing else can be sent.

Cadence: one request every five minutes reads everything (one sign-in per poll). When a release does not know one of the objects and refuses the combined request, Lanloom asks object by object once and leaves the unknown ones out from then on. After a refused sign-in Lanloom waits 15 minutes before it tries again, so a wrong password cannot lock the account.

The firewall logs each API request as an administrator sign-in of `lanloom-read`; that is expected.

## Releases

| release | reads |
|---|---|
| SFOS 19.0, 20.0, 21.0, 21.5 and 22.0 (including the free home edition) | everything above; checked against the published API reference of each release |
| SFOS 18.x | expected to read the same; not checked yet |
| SFOS 17.x | zones, interfaces, VLANs, objects, rules, DHCP; NAT rules are part of the firewall rules there and are left out |

Tested versions are listed in [COMPATIBILITY.md](../COMPATIBILITY.md).

## Troubleshooting

| status | what to do |
|---|---|
| Account was rejected | user name or password wrong, or the user is not of type *Administrator*; Lanloom pauses for 15 minutes |
| The firewall does not accept API requests from this address | add Lanloom's address under *Backup & firmware → API → Allowed IP address* |
| The API is switched off on the firewall | tick *API configuration* under *Backup & firmware → API* |
| Not reachable | check address and port (4444 by default) and that the zone allows HTTPS administration |
| TLS / certificate problem | tick *Accept a self-signed certificate* |
| Access denied for this account | the profile may not read the configuration; set every area to *Read-only* (not *None*) |
| Rules show a zone "?" | the profile may not read zones; rules still show, zone names are missing |

## Help us test

If you run Lanloom against a real firewall, the most useful feedback is: the SFOS release, whether the zone matrix matches what you expect, rules or NAT rules that look wrong or are missing, and the status line of the connection. Before you share a screenshot or a log, black out public addresses, host names and object names you consider private.

---

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.
