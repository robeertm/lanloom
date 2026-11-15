# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

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
