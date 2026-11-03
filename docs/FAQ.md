# FAQ

### Why does Lanloom use host networking?
To see your local network it needs the host's neighbour table, multicast announcements (mDNS, SSDP) and the host's own TCP byte counters. A container on a private bridge network sees none of that. Host networking opens no extra privileges: the container is unprivileged, read-only and has no capabilities. On desktop container runtimes host networking is limited, see [INSTALL.md](INSTALL.md).

### Does it slow down my network?
No. Probing is rate-limited to about 40 new connections per second, 24 concurrent and 4 per host. A gentle scan of a /24 takes roughly 10 to 60 seconds and sends very little data.

### Why are some devices "unknown"?
Classification uses deterministic rules on what a device reveals: announcements, device descriptions, open ports and neutral words in titles and host names. Devices that reveal nothing stay unknown. Open the component card to see the reasons and rename or re-type the device yourself.

### Why do some traces have no moving dots?
Only measured bytes are animated. Dots appear for this host's own TCP flows, for the internet uplink when a router or switch is connected over SNMP, and for MQTT topic groups. Other traces are dashed, which means "connection known, traffic not measured".

### What leaves my network?
Nothing, except the optional update check, which asks the public release feed for the latest version at most every 6 hours (15 minutes after a failed check) and only while the page is open. Turn it off with `LANLOOM_UPDATE_CHECK=0` or in the settings. There is no telemetry.

### Can Lanloom change anything on my devices?
No. It is read-only by construction. The scanner only probes, the hub connection cannot call services, the MQTT client cannot publish and SNMP uses read operations only.

### Does it support IPv6?
Not in 1.0. Scanning is IPv4 only.

### How many devices can it handle?
It is designed for homes and small offices, up to a few hundred devices. A scanned range can be at most a /22 (1024 addresses).

### Can I use it on my phone?
Yes. The interface is responsive with bottom tabs. Open it in the phone's browser and add it to the home screen. For passkeys and remote access, use HTTPS through a reverse proxy.

### Which languages are available?
English and German.

### Where is my data stored?
In the data volume mounted at `/data`: the inventory database (`lanloom.db`), the key file (`secret.key`), the setup code file and backups. Nothing is sent elsewhere.

### I forgot my password. What now?
There is no remote reset. Stop the container, delete `lanloom.db` in the data volume and start again. This is the only way, and it loses your inventory and settings. A new setup code is printed and the wizard starts over. Back up first if you can (see [INSTALL.md](INSTALL.md)).

### Can I try it without scanning my network?
Yes. Set `LANLOOM_DEMO=1` (optionally with `LANLOOM_DEMO_SEED=1`) to run a fictional network. Nothing is scanned.
