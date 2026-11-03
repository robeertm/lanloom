# Lanloom

**Your local network, drawn as a circuit board.** Devices and services become components, connections become routed traces, and measured traffic flows along them as moving dots.

![Lanloom map in dark theme](docs/screenshots/hero-map-dark.png)

> ## ⚠️ Please read before installing
>
> - **It scans your network.** Lanloom probes the private address ranges you approve. Only scan networks you own or are explicitly allowed to scan. The optional deep port scan touches many ports per device and may trigger alerts in intrusion-detection or security tools; it only runs after you tick an explicit consent box.
> - **It stores credentials you give it.** Optional integrations (home-automation hub, MQTT broker, router or switch) need secrets. They are stored encrypted (AES-256-GCM) and never shown again, but the key file lives next to the data: whoever controls the host controls them. Use read-only or least-privilege accounts.
> - **Host networking.** To see your network, the container shares the host's network stack. It runs unprivileged, with a read-only root file system and all capabilities dropped, but it can reach whatever the host can reach.
> - **The optional updater sidecar gets the container-runtime socket.** Access to that socket is root-equivalent on the host. Skip the sidecar (`--no-updater`) if you prefer to update by hand.
> - **Values are observations, not guarantees.** Classifications are heuristics, and traffic and liveness figures depend on what could be measured. Do not base security or safety decisions on them.
> - **Provided as is, without warranty.** See [LICENSE](LICENSE).
> - **Written with AI assistance.** Parts of this software were written with the help of AI tools; the code is reviewed and covered by automated tests and release checks.

## What it does

- **Draws the network as a PCB.** Zones for the Internet edge, Network, Servers and services, Computers and phones, Media and cameras, Smart home, Printers and other. Hub rooms and MQTT topic groups appear when those integrations are connected.
- **Routes traces like a board.** Orthogonal and 45° segments, buses and vias.
- **Animates only what is measured.** Each dot stands for a fixed number of bytes ("1 dot ≈ 64 KiB"; the quantum is chosen automatically in powers of two) and bytes are conserved. Traces without measurements stay dashed.
- **Shows how fresh the data is.** Every source and component carries a live grade: realtime (cadence up to 2 s), near (up to 15 s), periodic (up to 10 min), sluggish, interrupted, or unknown.
- **Two views.** *Map* is an overview by kind; *Network* shows the gateway and one zone per subnet.
- **Component card.** Classification with reasons, open ports, announcements, a chart of recent traffic, and options to rename, re-type or hide a component.
- **Device list** with search, light/dark/auto theme, a phone layout with bottom tabs, English and German UI.
- **Read-only by construction.** It observes; it cannot change your devices.

## Screenshots

All screenshots show the built-in demo network with invented names and addresses.

### Setup wizard

<table>
<tr>
<td width="50%"><img src="docs/screenshots/wizard-1-account.png" width="100%" alt="Wizard step 1: account"><br><sub>1. Create the account with the one-time setup code</sub></td>
<td width="50%"><img src="docs/screenshots/wizard-2-networks.png" width="100%" alt="Wizard step 2: networks"><br><sub>2. Choose the networks to map</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/screenshots/wizard-3-scan.png" width="100%" alt="Wizard step 3: scan"><br><sub>3. Pick a gentle or deep scan</sub></td>
<td width="50%"><img src="docs/screenshots/wizard-4-progress.png" width="100%" alt="Wizard scan progress"><br><sub>Scan progress</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/screenshots/wizard-5-review.png" width="100%" alt="Wizard step 4: review"><br><sub>4. Review the automatic classification</sub></td>
<td width="50%"><img src="docs/screenshots/wizard-6-connect.png" width="100%" alt="Wizard step 5: connect"><br><sub>5. Optionally connect integrations</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/screenshots/wizard-7-done.png" width="100%" alt="Wizard step 6: done"><br><sub>6. Done: rescan interval and update check</sub></td>
<td></td>
</tr>
</table>

### The map

![Map, light theme](docs/screenshots/map-light.png)

![Network view, dark theme](docs/screenshots/network-dark.png)

![Network view, light theme](docs/screenshots/network-light.png)

### Component card

![Component card](docs/screenshots/detail-light.png)

![Room detail](docs/screenshots/area-detail-dark.png)

### Live grade

![Live grade](docs/screenshots/live-grade.png)

### Devices and settings

![Device list](docs/screenshots/devices-light.png)

![Settings](docs/screenshots/settings-light.png)

### On the phone

<table>
<tr>
<td><img src="docs/screenshots/phone-map-light.png" width="200" alt="Phone map, light"></td>
<td><img src="docs/screenshots/phone-map-dark.png" width="200" alt="Phone map, dark"></td>
<td><img src="docs/screenshots/phone-detail.png" width="200" alt="Phone component card"></td>
<td><img src="docs/screenshots/phone-devices.png" width="200" alt="Phone device list"></td>
<td><img src="docs/screenshots/phone-wizard-scan.png" width="200" alt="Phone wizard scan step"></td>
</tr>
</table>

## Quick start

One command (installs into `~/lanloom`, port 7380, with the optional updater):

```sh
curl -fsSL https://raw.githubusercontent.com/robeertm/lanloom/main/deploy/install.sh | sh
```

Or with the compose file from this repository:

```sh
docker compose up -d                      # app only
docker compose --profile updater up -d    # app + one-click updater
```

Then open `http://<host>:7380`. On first start Lanloom prints a one-time **setup code** to its log:

```sh
docker logs lanloom 2>&1 | grep -i "setup code"
```

The code is also stored in `setup-code.txt` in the data volume. Enter it in the wizard to create your account. See [docs/INSTALL.md](docs/INSTALL.md) for all platforms and options.

## How the scan works

The default **gentle scan** needs no privileges and sends very little:

- It reads the host's kernel neighbour table and nudges addresses with a single UDP datagram to port 9, so the kernel resolves them. No raw sockets, no ICMP.
- It browses mDNS/DNS-SD and sends an SSDP M-SEARCH. Device descriptions are fetched only from the responding private address, capped at 64 KiB, and XML with DOCTYPE or ENTITY declarations is refused.
- It tries a TCP connect to 26 well-known service ports and reads the page title and `Server` header of web services (`GET /` only).
- Limits: about 40 new connections per second, 24 concurrent, 4 per host. A /24 takes roughly 10 to 60 seconds.

The **deep scan** checks about 218 common ports with the same limits and only runs with your explicit consent. Lanloom never scans public ranges, never reaches into the internet, and never tries logins. Only private ranges up to a /22 (1024 addresses) are accepted.

A presence check runs every 2 minutes (one TCP connect to a known open port, or a look at the neighbour table). A gentle rescan runs automatically every N hours (default 6).

Devices are classified by deterministic rules (gateway or self role, DNS-SD types, UPnP device types, port combinations, neutral words in titles and host names). A score of 0.6 or more is "sure", 0.3 or more "probable", otherwise "unknown".

## Live data and integrations

Dots appear only where bytes are actually measured:

- **This host's own TCP flows** come from the kernel's socket diagnostics (netlink) when running with host networking. No extra privileges needed.
- **Router or switch (SNMP v2c or v3)** gives interface counters, which animate the internet uplink. Only Get, GetNext and GetBulk are used.
- **MQTT broker** topics are grouped; Lanloom only counts messages and lengths per group. It connects, subscribes to `#`, and cannot publish. Payloads are never stored. TLS is optional.
- **Home-automation hub (WebSocket API)** adds rooms, devices and entities and state-change events, using a long-lived access token (a non-admin user is enough). A strict allow-list limits it to read commands, and it cannot call services.

All integrations are optional. Credentials are encrypted at rest with a key file (`secret.key`, mode 0600) in the data volume, never displayed again and never logged.

## Security and privacy

Sign-in is mandatory; there is no open mode. The first account can only be created with the setup code. Passwords use argon2id, sessions use random 256-bit tokens in `HttpOnly`, `SameSite=Strict` cookies, and state-changing requests are protected against cross-site use. The container runs as an unprivileged user with a read-only root file system and no capabilities. There is no telemetry. The only outbound connection is the optional update check against the public release feed. Put Lanloom behind a TLS reverse proxy for passkeys and remote access (list it in `LANLOOM_TRUSTED_PROXIES`, see [docs/INSTALL.md](docs/INSTALL.md#behind-a-reverse-proxy)), and never expose it directly to the internet. Details and how to report a vulnerability: [SECURITY.md](SECURITY.md).

## Updating

- **With the updater sidecar:** an "Update" button appears when a new version is available. It pulls the image, runs a self-test without network, starts a trial on a copy of your data, backs up the database and tags the previous image, swaps the container, checks health, and rolls back automatically on failure.
- **Without it:** `docker compose pull && docker compose up -d`.

Image tags: `:1.0.0` is immutable; `:1` and `:latest` move only after the published image has been tested; `:edge` is the latest build of the main branch.

## Requirements

- A container runtime with compose v2 (details in [docs/INSTALL.md](docs/INSTALL.md))
- amd64 or arm64
- About 300 MB for the image and about 60 MB RAM

## Documentation

- [docs/INSTALL.md](docs/INSTALL.md): installation on each platform, configuration, backup, uninstall
- [docs/FAQ.md](docs/FAQ.md): frequently asked questions
- [SECURITY.md](SECURITY.md): security model and reporting
- [CHANGELOG.md](CHANGELOG.md): release notes

## License

Lanloom is **source-available**, not open source. You may run it for personal or internal use on machines you own or control and read the source. Modification and redistribution require prior written permission. It is provided without warranty of any kind. See [LICENSE](LICENSE). Third-party components and their licenses are listed in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

© 2026 Robert Manuwald
