# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

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
