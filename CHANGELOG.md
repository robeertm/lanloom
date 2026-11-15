# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [1.7.0] – 2026-11-15

### Added
- Added: read adapter for Proxmox VE hosts and clusters (see [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) and [docs/integrations/proxmox.md](docs/integrations/proxmox.md)). Product names are used only to identify compatible products; no affiliation or endorsement.
- Guests on the map: every virtual machine and container hangs below the node it runs on (link "virtual", with bridge and VLAN), matched by the hardware address of its network card or an address the guest reports. The topology has a new source "hypervisor" right after manual entries; it beats what a switch learns on the node's port and is never refined by the shared-port rules.
- Component card of a node: version, cluster, processor, memory, load, uptime, the guests on it with state, VLAN and newest backup, storage with fill level, bridges and findings. Component card of a guest: node, state, addresses, network cards, newest backup (age, verified, backup job, failed backups), traffic now and during the last hour, its firewall.
- Firewall tab: a hypervisor appears as a live, read-only source with findings, the firewall at datacenter, node and guest level (options, rules, security groups, aliases, IP sets), all guest network cards, and the bridges, bonds and software-defined network of each node.
- Findings in plain words: node offline, storage almost full, running guest without a backup, backup older than seven days, failed backup task, network card on a missing bridge, VLAN tag not allowed on a VLAN-aware bridge, guest firewall without effect, and - with a controller adapter that reads switch ports - a guest VLAN the node's switch port does not carry.
- Night report contributions: a family can register its own section (`report` in the manifest, `integrations.night_sections()`); the hypervisor section lists guests stopped, added or removed since the last report, failed backups and findings, and appears only when the connection exists. In English and German.
- Integration dialog: hypervisor form (address, API token ID, token secret, self-signed certificate) with its own description and hints. Demo mode shows a sample hypervisor.

### Changed
- The neutral configuration model knows hosts, guest network cards, firewall rules and options at every level, bridges, software-defined network, storage, backup jobs and findings.
- The source switch of the Firewall tab scrolls sideways on a phone when there are many sources.

### Security
- The hypervisor adapter only sends `GET` requests to listed endpoints through one allow-listed code path; there is no sign-in, the API token travels in a header. Before a token is used, its own permission list is checked: a token holding anything but audit privileges, and every superuser token, is refused. Guest passwords and keys, notes and storage credentials are never copied; hardware addresses of guest network cards leave the adapter only as a hash.

## [1.6.0] – 2026-11-15

### Added
- **Data paths** (new tab, also in the phone tab bar): a calm world map of where the devices send data on the internet, by country. Arcs run from the home country to every destination country, their width follows the bytes of the chosen period (24 hours, 7 days, 30 days), destinations outside the EU are amber and those in the EU teal. Key figures: data exchanged with the internet, countries, share outside the EU, devices, hints. Lists of countries and devices; clicking a country shows its devices and clicking a device shows its countries, on the map as well. In English and German.
- Full screen for the world map (button at the map): Esc, the close button and the back gesture or key close it without leaving the view; the period switch, legend and selection stay usable, the map is redrawn in the real resolution, and on a phone held upright it is wider than the screen and starts at the home country.
- Calm hints, never alarms and never a rule: a device talks to a country for the first time in 30 days (from 1 MB, after three days of history), a device uploads at least three times its typical day and at least 200 MB in 24 hours, a device in a network listed as "without internet" exchanges data with the internet.
- Settings → Data paths: home country (default from the browser), own networks and tunnels, networks that should not reach the internet.
- Offline country data: the free IP to Country Lite database by DB-IP (CC BY 4.0, attribution "IP Geolocation by DB-IP" in the app, the README and THIRD-PARTY-NOTICES.md) ships in the image in a compact form (1.5 MB, loads in about 0.1 s). No address is ever looked up online; a new month of data comes with a new release (`scripts/build_geodb.py`). The base map is built from Natural Earth (public domain) with `scripts/build_worldmap.py`.
- The self-test checks the country data and the data paths sums.

### Changed
- Own networks and tunnels (the address range used by overlay VPNs, special ranges and every network listed in the settings) are counted apart and never as "without a country"; the share outside the EU refers only to traffic with a known country and shows "no data" instead of 0 % when there is none. Without loaded country data nothing is booked at all.
- The phone tab bar holds six tabs; labels are a little smaller and long German labels use a short form.

### Security
- Only countries, device ids and byte counts per hour (49 hours) and per day (31 days) are stored, plus the first and last day a device talked to a country. No peer addresses, host names or visited sites. The data paths API needs a session like every other view.

### Known limits
- The first version counts the connections of the Lanloom host itself (host networking). Flow sources from the gateway follow in a later release.

## [1.5.0] – 2026-11-15

### Added
- Added: read adapter for UniFi Network controllers (see [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) and [docs/integrations/unifi.md](docs/integrations/unifi.md)). Product names are used only to identify compatible products; no affiliation or endorsement.
- Firewall tab: a configuration read by a device-family adapter appears as a **live, read-only source** next to the simulation or the executor (and opens by default while the editor runs the simulation). It shows the real zones, networks and clients, a zone matrix, the firewall rules in the controller's order with their filters and hit counters, port forwarding, wireless networks, and the network equipment with every switch port (link, speed, power over Ethernet, native VLAN, uplink).
- Integration dialog: device-family adapters are offered with their own form (address, API key, optional view-only account, site), built from the adapter's manifest. In English and German.
- The physical topology takes clients and devices from a device-family adapter with the same rank as the switch controller, merged with manual entries, LLDP and the address table.
- Demo mode shows a sample controller configuration in the Firewall tab.

### Changed
- The registry of built-in families collects the family packages shipped in its own folder; nothing else can be loaded (still no plugin mechanism).
- Zone verdicts derived from rules ignore rules with a connection-state filter or a schedule, which never decide a whole zone pair.
- The generic switch controller source and the adapters share one mapping of client and device lists to topology observations.

### Security
- A read adapter only sends `GET` requests to listed endpoints, plus the sign-in and sign-out of a view-only account, through one allow-listed code path. It refuses an account whose role on the site can change settings, caps sign-ins (six per hour per controller and account) and pauses for 15 minutes after a refused sign-in. Wireless passphrases, pre-shared keys and RADIUS settings are never copied; every write capability a read adapter might report is stripped by the app.

## [1.4.1] – 2026-11-15

### Added
- Setting **Settings → Appearance → Traffic unit**: show every traffic rate in bytes per second (kB/s, MB/s) or in bits per second (kbit/s, Mbit/s, Gbit/s), including the internet and router cards and the component details. Available in English and German; the default stays bytes per second.
- The switch controller form offers the existing *Login style* option (Automatic, Console, Classic).

### Changed
- Clients behind an access point whose link type no source knows are now shown as wireless (dashed) instead of "Unknown" drawn as a cable. A link type reported by a source, and every manual entry, keeps its value; switches and routers are not assumed to be wireless.

### Fixed
- Switch controller: a self-hosted controller that answers the console sign-in with HTTP 401 is no longer reported as "Credentials were rejected". With automatic login style the classic sign-in is tried next, and the credentials count as rejected only when both ways refuse. A forced style never falls back.
- Documentation: the switch controller is added under **Settings → Connections**, not "Integrations".

## [1.4.0] – 2026-11-15

### Added
- Foundation for device-family adapters (no vendor adapter yet): a fixed registry of built-in families, each with a read half for the web app and a write half for the executor that share one translation module. Described in [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md); supported families are listed in [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md).
- Capability lists: every adapter declares what it can read and which changes it may make (stage B, or B* with its own confirmation); the executor's gate refuses everything else, and the firewall tab only shows controls the connected controller supports. `GET /api/integrations` also lists the families with their capabilities.
- Neutral configuration model version 2: network equipment, switch ports, firewall policies, wireless networks, virtualization guests and bridges, address reservations and port forwards, next to the version 1 parts, which keep their meaning.
- Plan steps version 2: client settings, networks, zones, policies (including order), switch ports, wireless networks, reservations, forwards and guests, each with exactly the fields it may change and its way back. Firmware, restarts, accounts, keys, remote access, VPN and WAN settings, port mirroring and factory resets are no step kind at all. Mass and structural changes stay unavailable.
- New lockout rules for the new kinds: uplink ports, built-in rules, the management network and objects Lanloom did not create are never changed or deleted.
- One send path per adapter with an allow-list below the adapter: read halves can only read; write halves name the exact body fields and allowed values of every request.
- Separate credentials: read-only accounts in the app (an account that reports it can write is refused where that is checkable), write credentials only in the executor's options file, which must be mode 0600, belong to the executor's user and not be a symbolic link. Secrets never appear in logs, exception texts or `repr()`.
- [CONTRIBUTING.md](CONTRIBUTING.md): ideas, bug reports and adapter requests are welcome as issues; pull requests are read but not merged, accepted ideas are re-implemented.

### Changed
- The executor loads only built-in adapters: `LANLOOM_EXECUTOR_ADAPTER` now takes a family id from the registry instead of a module path. There is no way to load adapter code from elsewhere.
- Plans keep the lowest version that can express them, so plans of moves only stay identical to earlier releases and an executor that has not been updated yet keeps taking them. A plan of a newer version is refused with a clear message instead of being half-understood; the executor logs when it is older than the app, and the executor dialog shows it.
- The executor's state report is checked against the neutral model (opaque ids, no hardware addresses as ids).
- The simulation is unchanged: same sample network, same preview, same plans.
- The setup wizard screenshots are new.

### Fixed
- Firewall editor: a change is reported as finished only once the executor is free again, so approving the next change (for example the undo) right afterwards no longer meets "another change is still running" on a busy machine.
- Setup wizard, networks step: the hint below "Add a network range" no longer overlaps the field label; it sits below the input field.

## [1.3.0] – 2026-11-15

### Added
- Physical topology in the Network view: which device hangs on which switch port, which clients use which access point, and which guests run on a virtualization host. Switches and access points get their own band below the router, guests their own zone next to their host. Cables are solid traces, wireless links dashed with a radio mark (band and network name at the device), guests dotted; the switch port number sits at the device end of the trace. Without topology data the view stays as before.
- Four read-only sources with a fixed order of precedence: a manual "connected via" entry on the component card, a switch controller, LLDP neighbours over SNMP, and the switch address table over SNMP. Within one source the more specific observation wins; agreeing sources fill in missing details, and links that would close a loop are skipped.
- Component card: a Connection section (connected via, port, link type, source and age, why a device was placed behind another one, which sources agree, what disagreeing sources report and the order of precedence), the devices on each port of a switch or access point, addresses that are not in the device list, and a "Connected via (manual)" editor with device, port and link type (cable, wireless, virtual).
- New integration "Switch controller": signs in with a local view-only account and reads the client and device lists every minute (two common API styles, detected automatically, optional self-signed certificate). One code path makes every request and refuses anything but the sign-in and the two lists; a static test checks this. The password is stored encrypted.
- The SNMP integration also reads LLDP neighbours (LLDP-MIB, with management addresses and capabilities) and the forwarding database (Q-BRIDGE-MIB, BRIDGE-MIB fallback, bridge port to interface name) about once a minute. Agents without these tables contribute nothing and the counters keep working.
- Shared switch ports are read sensibly: devices behind exactly one switch or access point (or behind a device set by hand) hang below it; virtual network cards next to exactly one ordinary device become guests of that host; a port with very many addresses and no switch is treated as a trunk and not assigned from the address table.
- This host's measured traffic travels along the physical links in the Network view (up to the switch and down to the peer, or up to the router for internet traffic).
- `GET /api/topology` (signed in) returns the resolved links for bug reports.
- The demo network has a switch, two access points (one with a wireless uplink) with wireless clients, a virtualization host with two guests and one manual entry that overrides the controller.
- Documentation: [docs/TOPOLOGY.md](docs/TOPOLOGY.md) (sources, view-only account, LLDP and SNMP, shared ports, troubleshooting).

### Changed
- Screenshots of the map, the Network view and the component card are new.

## [1.2.2] – 2026-11-14

### Changed
- Internal quality gate only, no behaviour change: the check for third-party product and vendor names now also accepts an optional private list kept outside the repository, uses word boundaries and ignores case, adds roughly seventy more common home-network, hosting and database names, treats package names in requirements files as technical identifiers, and has its own self-tests that run before the leak check. A full run over every tracked file, interface text in all languages, file names and screenshots found nothing to reword.

## [1.2.1] – 2026-11-11

### Changed
- The demo data uses a neutral UPnP device-type namespace for its sample access points, and a scrubber test fixture uses a generic printer name. No behaviour changes.
- Internal quality gate: the check for third-party names now covers every tracked file (documentation, interface texts in all languages, comments, tests, file names) against a much longer list, and exceptions need an individual written justification.

## [1.2.0] – 2026-11-10

### Added
- Firewall editor: a new Firewall tab shows zones, networks and devices, and what each zone may reach. In Change mode a device can be dragged to another network, or tapped (keyboard: Enter) to choose "Move to…", which is also the way on a phone. Moves collect in a draft that is kept on the server for 24 hours and can be taken back one by one.
- Plain-language preview: what changes (new address), what the device may do afterwards and what it may no longer do, and what to watch out for (wireless outage while access points are reconfigured, private hardware addresses, lost printer and speaker discovery, things that use the device by its address, your own device, quiet hours). Blocked moves are named with the reason.
- Simulation mode as the default: changes run through the real executor pipeline (checks, backup, change, watch, rollback) against a built-in sample network and write nothing anywhere. The sample can be reset. Recent changes are listed and can be undone for 24 hours.
- Optional executor service (`python -m lanloom.executor`, compose profile `executor`) with a generic controller adapter interface. Plans name only opaque ids, are canonical JSON and are approved with a passkey signature over their SHA-256 hash; the executor verifies the signature against keys only it holds, checks expiry, nonce and a fresh controller state, refuses more than 10 objects and moves that could lock you out, backs up every touched object, watches afterwards and rolls back on its own, also after a crash. Real writes stay off until the operator provides an adapter, sets an arming phrase and creates an arming file by hand. Documentation with warnings: [docs/FIREWALL-EDITOR.md](docs/FIREWALL-EDITOR.md).
- Tests for the gate, the signature checks, backup, partial and full rollback, crash recovery, link attacks on the backup and the complete signed path from the browser API to the executor, all against in-memory stand-ins.

### Changed
- The navigation has a fifth tab, Firewall. Screenshots are new.
- The container image prepares `/state` for the optional executor.

## [1.1.2] – 2026-11-05

### Fixed
- End marks and port marks no longer cover zone titles. A host can report areas to keep clear (`host.keepClear(rect)`); marks avoid them when placed, and a highlighted port mark only falls back onto a title while it is hovered.
- Long mark labels are shortened at a word boundary (space, middle dot, hyphen, comma) instead of in the middle of a word. If no whole word fits, the mark shows the count. The tooltip keeps the full text.

## [1.1.1] – 2026-11-04

### Fixed
- Dragging the mouse across the board or double-clicking a tile no longer selects the labels of the tiles. Text in the details panel, dialogs and forms stays selectable.

## [1.1.0] – 2026-11-03

### Added
- Bundled traces: traces that end at the same component and port share one trunk there, and their moving dots add up on it. Ports that differ stay separate.
- End marks: where a trace leaves the board towards a peer outside the local network, a label names the peer (or "3 sources" with a drop-down list and shares). When the board scrolls, marks at the top and bottom edge point to components outside the view.
- Port marks: the destination port or protocol at the end of a live trace (from observed connections, otherwise from the scan). Unknown ports are left out.
- The board engine is now a documented core with versioned interfaces (`core-manifest.json`, [docs/ARCHITECTURE-CORE.md](docs/ARCHITECTURE-CORE.md)): router, bundled traces, end marks and port marks take everything from a host object, texts from a translation callback and colours from `--ll-*` custom properties.
- `LANLOOM_TRUSTED_PROXIES`: addresses or networks of your reverse proxies. Only these may set `X-Forwarded-For`, `X-Real-IP` and `X-Forwarded-Proto`, and the sign-in rate limit then counts the real client. A new "Behind a reverse proxy" section in the installation guide explains the setup.
- Free text found on the network (page titles, server headers, discovery names and fields) no longer carries passwords from URLs or long access tokens; they are shown as "[hidden]".
- New tests prove the read-only promise from the source code, check that every route needs a session unless it is explicitly public, and check that demo data and fixtures hold no secrets.
- A hang guard for the test suite and job timeouts in CI.

### Changed
- The trace router uses the tuned costs of the newer engine: fewer bends, fewer crossings, buses that stay together. Routes are cached under a new key, so the board is routed once again after the update.
- The update check reads the list of releases and offers the highest stable version. A later bug-fix release for an older line no longer hides a newer version.
- After a failed update check, the next try comes after 15 minutes instead of 6 hours.
- The updater only trusts a candidate image whose self-test exits 0 and reports "selftest: ok" with the requested version.
- IPv6 clients are rate limited per /64.

### Fixed
- Two update requests at nearly the same time (double click, second tab) could fail with a server error. Now one is accepted, and the other is refused as "busy" while a request waits or an update runs.
- A repeated request after a successful update showed "failed: already current". Refusals now leave the finished status in place or show a notice instead of an error.
- The update dialog could stop early because it read the status of an earlier run.
- Behind a reverse proxy, one person typing wrong passwords locked everybody out. Flooding the rate limiter with many addresses could reset an active lock-out.
- A forwarded protocol header from any client could mark the session cookie `Secure`.
- The trial start of an update candidate could be made to scan the network. It now only answers the health check.
- SNMP counter jumps larger than the link speed allows are discarded instead of showing up as traffic bursts.

## [1.0.0] – 2026-11-01

First public release.

### Added
- Circuit-board map of the local network: components in zones, traces routed orthogonally and at 45° with buses and vias.
- Two views: Map (overview by kind) and Network (gateway plus one zone per subnet).
- Moving dots for measured bytes with byte conservation and an automatically chosen quantum (powers of two, default 1 dot ≈ 64 KiB); unmeasured traces are dashed.
- Live grade per source and component: realtime, near, periodic, sluggish, interrupted, unknown.
- Gentle scan (neighbour table, mDNS/DNS-SD, SSDP, TCP connect to 26 well-known ports, HTTP(S) title and header) and consent-gated deep scan (about 218 ports), both rate-limited and private-ranges only.
- Presence check every 2 minutes and automatic gentle rescan (default every 6 hours).
- Deterministic device classifier with confidence levels (sure, probable, unknown) and reasons.
- Component card with classification, open ports, announcements, recent traffic chart, rename, re-type and hide.
- Device list with search; light, dark and auto theme; phone layout with bottom tabs; English and German UI.
- Setup wizard: account, networks, scan, review, connect, done.
- Optional read-only integrations: home-automation hub (WebSocket API), MQTT broker, router or switch (SNMP v2c and v3). Credentials are encrypted at rest.
- Live byte counters for this host's TCP flows via kernel socket diagnostics.
- Mandatory sign-in with one-time setup code, argon2id passwords, optional passkeys, session cookies, login rate limiting, cross-site request protection and a strict content security policy.
- Optional one-click updater sidecar with self-test, trial start, database backup, previous-image tag, health check and automatic rollback; optional update check that can be disabled.
- One-line installer, compose file, multi-architecture images (amd64, arm64) and a built-in demo mode with a fictional network.
