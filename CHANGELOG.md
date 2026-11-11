# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

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
