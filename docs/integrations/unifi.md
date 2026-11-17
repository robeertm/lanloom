# UniFi Network (local API): read adapter and client write half

Lanloom reads the configuration and the state of a UniFi Network controller (a console or a self-hosted Network application) over its local APIs. The web app **only reads**. Since 1.23.0 the optional executor service can also change **clients** - move a client to another network, give it a fixed address, rename it - after the safety checks and your passkey signature (stage B, see [Changing clients](#changing-clients-stage-b) below). Nothing else is ever written.

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

Lanloom checks the account's role on the site when you test or save the connection and **refuses an account that can change settings** (site administrator, owner or super administrator). Do not use your own administrator account.

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

Everything else - blocking clients, networks, firewall rules, port settings, wireless networks, firmware, restarts, accounts - is refused by the allow-list below the adapter, whatever a plan says.

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

## Troubleshooting

| status | what to do |
|---|---|
| API key was rejected | create a new key; check the address (console vs. self-hosted path) |
| Account was rejected | user name or password wrong, or the account is not local |
| This account can change settings | give it the view-only role, or create a separate view-only account |
| Sign-in paused | wait 15 minutes after repeated refusals; check the password first |
| Access denied for this account | the role lacks rights to the Network application |
| TLS / certificate problem | tick *Accept a self-signed certificate* |
| Executor: "A wired device changes its network through its switch port" | the client is connected by cable; only wireless clients can be moved by a client setting |
| Executor: certificate does not match the pinned one | the console's certificate was renewed (or it is another device): read the new fingerprint and update `cert_sha256` |
| Executor: account cannot write | the executor's account has the view-only role; give it *Site Admin* |

---

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.
