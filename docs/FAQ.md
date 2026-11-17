# FAQ

Short answers. Something does not work? See [TROUBLESHOOTING.md](TROUBLESHOOTING.md). How a view works? See the [manual](manual/README.md). Which devices are supported? See [COMPATIBILITY.md](COMPATIBILITY.md).

### Why does Lanloom use host networking?
To see your local network it needs the host's neighbour table, multicast announcements (mDNS, SSDP) and the host's own TCP byte counters. A container on a private bridge network sees none of that. Host networking opens no extra privileges: the container is unprivileged, read-only and has no capabilities. On desktop container runtimes host networking is limited, see [INSTALL.md](INSTALL.md).

### Does it slow down my network?
No. Probing is rate-limited to about 40 new connections per second, 24 concurrent and 4 per host. A gentle scan of a /24 takes roughly 10 to 60 seconds and sends very little data.

### Why are some devices "unknown"?
Classification uses deterministic rules on what a device reveals: announcements, device descriptions, open ports and neutral words in titles and host names. Devices that reveal nothing stay unknown. Open the component card to see the reasons and rename or re-type the device yourself.

### Why do some traces have no moving dots?
Only measured bytes are animated. Dots appear for this host's own TCP flows, for the internet uplink when a router or switch is connected over SNMP, for MQTT topic groups, and in the Network view along the known cabling between this host and its peers. Other traces are dashed, which means "connection known, traffic not measured".

### What leaves my network?
Nothing, except the optional update check, which asks the public release feed for the latest version at most every 6 hours (15 minutes after a failed check) and only while the page is open. Turn it off with `LANLOOM_UPDATE_CHECK=0` or in the settings. There is no telemetry. If you switch on a delivery channel for the nightly report, its short summary goes to the address you entered; by default no channel is on.

### What is the nightly report?
Every morning at 06:30 (changeable) Lanloom sums up the night in the Reports tab: outages, new and missing devices, interrupted connections, data paths hints, configuration changes, expiring certificates and its own updates. It is rule-based and made on the host. If Lanloom was off at that time, the report is made at the next start. See [NIGHTLY-REPORT.md](NIGHTLY-REPORT.md).

### Can Lanloom change anything on my devices?
No. It is read-only by construction. The scanner only probes, the hub connection cannot call services, the MQTT client cannot publish, SNMP uses read operations only and the switch controller connection only reads two lists with a view-only account. The firewall editor runs as a simulation against a sample network unless you set up the separate executor service with a built-in adapter for your controller family that can write (see [COMPATIBILITY.md](COMPATIBILITY.md)) and arm it explicitly; even then every change needs your passkey and is backed up, watched and rolled back on its own if something goes wrong. See [FIREWALL-EDITOR.md](FIREWALL-EDITOR.md).

### Does it support IPv6?
Not in 1.0. Scanning is IPv4 only.

### How many devices can it handle?
It is designed for homes and small offices, up to a few hundred devices. A scanned range can be at most a /22 (1024 addresses).

### Can I use it on my phone?
Yes. The interface is responsive with bottom tabs. Open it in the phone's browser and add it to the home screen. For passkeys and remote access, use HTTPS through a reverse proxy.

### Which languages are available?
English and German. Every text, number, date and time follows the language you choose (decimal comma and 24-hour clock in German, proper singular and plural). Another language can be suggested in an issue; see [CONTRIBUTING.md](../CONTRIBUTING.md#translations).

### Can I change the colours?
Yes. Settings > Appearance > Colour theme offers **Circuit green** (default) and **Aurora**; "Light / dark" next to it switches between light, dark and automatic. Both settings are remembered in the browser, so every device can have its own. Warning and error colours keep their meaning in every theme.

### Where is my data stored?
In the data volume mounted at `/data`: the inventory database (`lanloom.db`), the key file (`secret.key`), the setup code file, the updater's database copies (`backups`) and the automatic configuration backups (`config-backups`). Nothing is sent elsewhere.

### How do I back up Lanloom or move it to another computer?
Settings › Backup downloads one file encrypted with your passphrase and can write automatic backups on a schedule. On the new computer, install Lanloom and choose *Restore from a backup instead* on the first-start page; amd64 and arm64 can be mixed. See [BACKUP.md](BACKUP.md).

### I forgot my password. What now?
Ask an admin for a one-time reset link (Settings, Users). Only if no admin can sign in: stop the container, delete `lanloom.db` in the data volume and start again. This is the last resort, and it loses your inventory and settings. A new setup code is printed and the wizard starts over. Back up first if you can (see [INSTALL.md](INSTALL.md)).

### I lost my phone with the authenticator app. What now?
Use one of your ten recovery codes at the second step, then set up the app again. Without codes, ask an admin for a reset link and tick the option to remove the second factor. If you are the only admin and have no recovery code, see [INSTALL.md](INSTALL.md#forgot-password).

### Can we share one account?
Better not. Every person gets an account with their own role (Settings, Users, Invite someone), so the audit log shows who did what and one person's sessions can be ended without touching the others.

### Why does the sign-in ask for a code?
A second factor is required for admins by default; an admin can extend this to other roles in Settings, Security. After a grace period the next sign-in leads to a set-up page. See [ACCESS.md](ACCESS.md).

### Where can I see who changed something?
In Settings, Security, Audit log (admins). It lists every change with user, role, time, target, result and origin, can be filtered and exported as CSV, and shows if the log was tampered with. See [ACCESS.md](ACCESS.md#audit-log).

### Can a reverse proxy with its own sign-in log people in?
Yes, optionally. Set `LANLOOM_AUTH_HEADER` and `LANLOOM_TRUSTED_PROXIES` (and the group settings to map groups to roles), and make sure the proxy removes these headers from client requests. See [ACCESS.md](ACCESS.md#sign-in-through-an-authentication-proxy).

### Can I try it without scanning my network?
Yes. Set `LANLOOM_DEMO=1` (optionally with `LANLOOM_DEMO_SEED=1`) to run a fictional network. Nothing is scanned.

### Why does Lanloom suggest an adapter?
In the connect step of the setup assistant, Lanloom compares what the gentle scan already saw (open ports, web page titles, mDNS and SSDP announcements) with the detection signs of each integration family. It never tries to sign in. A suggestion shows a guide for creating a read-only account or token and a prefilled address; you enter the credentials yourself and press *Test*. Ignore a suggestion and nothing happens.

### Does the sample network touch my data?
No. *Explore a sample network* shows a fictional network from a separate in-memory store, read-only, per browser session. Your real inventory is not read or changed there, every change is refused, and the sample ends with sign-out or after 15 minutes without activity.

### Does the AI see my data?
Only if you set one up, and only what you ask about. A local AI runs in your network and gets the real text; nothing leaves your network. A cloud service only ever receives placeholders instead of addresses, host and device names, wireless network names, serial numbers and locations, only for the functions you allow, and never more requests than the daily limit. "Preview what is sent" shows the exact text first. Details: [integrations/ai.md](integrations/ai.md).

### Which hardware do I need for a local AI?
A computer that is always on, with at least 4 GB of memory for a small model and 8 GB or more for a medium one; a graphics unit makes answers much faster. Without one, the number of processor cores is the limit and answers can take a minute. Lanloom measures the computer and says honestly if it is too small. If this host is small, put the model on another computer in your network (Settings, AI, "Another computer") or use a cloud service.

### Can the AI change my settings?
No. It only reads and explains. Its firewall suggestions reach the draft only when you take them over, and then the firewall editor shows a preview, asks for your confirmation and, for real changes, a passkey signature. A suggested device kind counts only after you confirm it, and then only as a weak clue.

### A connection shows "Stale" or "Connection fails". What now?
*Stale* means data stopped coming; Lanloom reconnects on its own with a growing pause of up to one minute, so a device that rebooted heals by itself. *Connection fails* names a reason; the table in [TROUBLESHOOTING.md](TROUBLESHOOTING.md#read-the-status-of-a-source) lists every reason with its usual cause and fix, and each source and adapter has its own section there.

### How do I report a problem?
Open an issue with the matching form (bug, wish, adapter request or test report) and attach the diagnostic file from *Settings > General > Diagnostic export*. It holds counts, states and reason keys, no addresses, names or credentials; open it and look before you share it. Never post passwords, tokens, API keys or recovery codes. See [TROUBLESHOOTING.md](TROUBLESHOOTING.md#the-diagnostic-export).

### The scan finds nothing. Why?
Almost always because the container cannot see your network: a desktop container runtime runs without host networking, so there is no neighbour table and no mDNS or SSDP. Run Lanloom on a server (a small single-board computer or a NAS is fine) and keep host networking, or type your range in by hand and accept that only TCP probes work. More in [TROUBLESHOOTING.md](TROUBLESHOOTING.md#scanning-and-devices).

### Why is a device missing or in the wrong network?
Lanloom only scans networks it can reach and that an administrator allowed, never public ranges and never anything larger than a /22. Devices behind a router appear after you allow that network under *Settings > Network*. A device in the wrong place can be corrected on its card with a manual entry, which overrides every source.

### Which devices and firmware versions are supported?
The generic sources (this host, SNMP, MQTT, a hub's WebSocket API, a switch controller) work with any device that speaks the protocol. Device families with their own adapter and the versions they were checked against are in the matrix of [COMPATIBILITY.md](COMPATIBILITY.md). A family that is not listed can be requested with the adapter request form.

### Does a read-only account really protect my devices?
Lanloom refuses accounts that can change settings where it can check it (device-family adapters) and has no code path that writes in the generic sources. Still use the least rights the device offers and prefer tokens to passwords, see [SECURITY-MODEL.md](SECURITY-MODEL.md).

### Can I run it behind a reverse proxy or below a sub-path?
Yes. Set `LANLOOM_TRUSTED_ORIGINS` and `LANLOOM_TRUSTED_PROXIES`, and `LANLOOM_BASE_PATH` for a sub-path, as described in [INSTALL.md](INSTALL.md#behind-a-reverse-proxy). The message "The request came from another site and was blocked" means the proxy's public address is not in `LANLOOM_TRUSTED_ORIGINS`.

### How do I remove a device that is gone?
Open its card (hidden devices: tick *Show hidden* in the device list) and choose *Forget device* (operators; only for hidden or offline devices, after a confirmation). Or see the *Probably gone* rows of the [inventory](manual/inventory.md), which can be decommissioned and kept as history. Devices without a sign of life for a while are marked automatically.

### A device appears twice behind a Wi-Fi repeater
Many repeaters rewrite the hardware address of their clients: the first three bytes are replaced by a private prefix, the last three stay. Lanloom identifies a device by that address, so it shows a second entry. When the IP address is the same, or the device's host or mDNS name still contains its original address, the card offers *Same device* (never an automatic merge). Join the two there, or decommission the old entry in the inventory, or forget it on its card. If the name contains the original address, the maker is taken from it.

### Where is the manual?
In [docs/manual/](manual/README.md): one page per view with its purpose, what you see, how to use it, its limits and what it does with your data. The *About this view* rail (key `I`) links to the page of the open view.

## How does Lanloom know which device is on which switch port?

From up to four sources, in this order of precedence: a manual "connected via" entry on the component card, a switch controller with a view-only account, LLDP neighbours over SNMP and the switch address table over SNMP. Without any of them the Network view shows the gateway and one zone per subnet as before. Details: [TOPOLOGY.md](TOPOLOGY.md).
