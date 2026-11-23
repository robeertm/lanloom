# Security model

This document says what Lanloom protects, against whom, how, and what is left over. It complements
[SECURITY.md](../SECURITY.md) (rules, verification, reporting) and the detail pages it links to. It is reviewed with
every release that adds a way to write, a new outbound connection or a new route without a session.

## What is worth protecting

| Asset | Where it lives | Why it matters |
|---|---|---|
| The map of the network (devices, addresses, names, ports, cabling, traffic) | database in the data volume | a ready-made target list for an attacker |
| Credentials of integrations (read-only accounts, tokens, delivery channels, AI keys) | encrypted in the database, key `secret.key` in the data volume | access to other systems |
| Write credentials of a controller or hypervisor | only in the executor's options file (0600) | can change the network |
| Accounts, sessions, second factors, passkeys | database | control over Lanloom itself |
| The audit log | database, hash chain keyed from `secret.key` | evidence of who changed what |
| The image users run | public registry | code that runs in thousands of networks |

## Who we defend against

### 1. An attacker in the local network

Someone with a device in the same network (a compromised IoT device, a guest, malware on a laptop) who can reach
Lanloom's port and sniff or answer local multicast.

- **Sign-in is mandatory**, the first account needs the one-time setup code from the log, passwords are argon2id,
  sessions are 256-bit random tokens in `HttpOnly`, `SameSite=Strict` cookies.
- **Brute force:** failed sign-ins, second steps, invitation and restore codes lock the client address out (5 tries,
  then 60 s doubling to 1 h; IPv6 per /64); the limiter's memory is bounded without forgetting active lock-outs.
- **Flooding:** every state-changing request passes a flood guard: 120 per minute per client address for the routes
  without a session, 600 per minute per account for everything else (`lanloom/flood.py`). Open passkey sign-in
  ceremonies are capped at 2000 (the oldest goes first), so the routes without a session cannot grow memory without
  bound. A test sends past the limit on every route without a session (`tests/test_security_headers.py`).
- **Cross-site requests from a page the victim opens:** state-changing requests need `X-Lanloom: 1` and a matching
  `Origin`; cookies are `SameSite=Strict`; responses forbid framing.
- **Sniffing:** Lanloom serves plain HTTP. In a network you do not trust, put it behind a TLS reverse proxy; HSTS is
  then sent (only when the request really arrived over HTTPS: directly, or from a proxy in `LANLOOM_TRUSTED_PROXIES`
  that says so). Passkeys need HTTPS or localhost anyway.
- **Forged announcements** (mDNS, SSDP, UPnP, WS-Discovery, NetBIOS, LLDP/SNMP answers): every value read from the
  network is untrusted text. It is size-capped and parsed defensively, shown escaped under a strict Content Security
  Policy, and never decides a write. A description address in an announcement is fetched only when it points into the
  scanned private ranges as an address literal. The scanner only probes private address ranges and never tries to
  sign in.
- **Remaining risk:** without TLS, someone who can read the traffic can steal a session cookie. Use a TLS proxy or a
  trusted network segment for the management port.

### 2. A stolen session

Someone got hold of a session cookie (shared computer, malware in the browser, sniffed plain HTTP).

- Sessions expire after 30 days, after 14 days without use, and on public computers after 8 hours or 30 minutes
  idle. Users see and end their sessions; admins can end anyone's; a password reset ends all.
- **Step-up confirmation:** critical actions (users, connections, sign-in security, AI pairing, own passkeys and
  factors, backup export and restore) need a passkey, app code or recovery code from the last 10 minutes. A stolen
  cookie alone cannot add a user, change a connection or take a backup.
- **Executor approvals** need a passkey signature over the SHA-256 of every plan, every time; the executor checks it
  against keys the operator pinned. A stolen session cannot change the network.
- **Roles on the server** for every route (`lanloom/access.py`, checked by a test against every route); a viewer's
  stolen session can only read.
- The flood guard caps what a stolen session can do per minute, and every state-changing action lands in the
  tamper-evident audit log with origin and user agent.
- **Remaining risk:** a stolen viewer or operator session can read the map until it expires or is ended.

### 3. A malicious counterpart

A device or service Lanloom talks to answers with crafted data: a hub, MQTT broker, SNMP agent, switch controller,
zone firewall, hypervisor, a web page on a device, a cloud AI service, a webhook receiver.

- **Read-only by construction:** each integration has one send path that refuses everything but reads (allow-lists
  per protocol, checked by tests); write halves exist only in the separate executor process.
- **Untrusted input:** answers are size-capped, parsed with the standard library or strict parsers, and shown
  escaped. Certificates are checked unless an administrator allows a device's own certificate for that connection
  (opt-in); device-family adapters can bind the connection to the certificate's SHA-256 instead.
- **AI answers** are text only; the AI never writes configuration and its suggestions go through the same draft,
  preview and passkey as a person's. Cloud services get placeholders instead of addresses, names and serial numbers.
- **The executor's connection guard** allows connections only to the app and to the controller resolved at start;
  everything else is refused before it is made.
- **Remaining risk:** a compromised counterpart can feed false data into the map (wrong names, fake devices). Lanloom
  shows observations, not guarantees.

### 4. The supply chain

Someone tampers with what users install: a dependency, the base image, the build, the registry, the update path.

- **Pinned dependencies:** every Python package (also transitive ones) is pinned with `==` in `requirements.txt`;
  the image is built from the official Python slim image of the current stable release of its distribution, with the
  distribution's security updates applied in every build.
- **Dependency scan** (`scripts/dep_scan.py`): the pinned Python packages against the OSV database, and the built
  image (operating system and Python packages) with grype, in CI before an image is built for publishing, again on the
  published image, and in the release gate. A critical or high finding stops the release unless a valid exception
  covers it (`security/vulnerability-policy.json`: advisory id, packages, kind, a reason that says why, checked date
  and a review date at most 183 days later; an expired one counts as missing). The policy file cannot lower the rule
  for critical or high. A self-check proves the scanner still reports a known critical and a known high package as
  red; a scan that cannot run is never green. See [DEPENDENCY-SCAN.md](DEPENDENCY-SCAN.md).
- **No set-user-ID files:** the build removes every set-user-ID and set-group-ID bit from the image and the self-test
  checks it, so a local flaw in a tool such as mount cannot be used to gain rights, whatever options the container
  is started with.
- **Pinned tools and actions:** the scanner, SBOM and signing tools are pinned by version and SHA-256
  (`security/tools.json`); the CI actions of the build and test workflows are pinned to commit hashes.
- **Signed images, no key anywhere:** each published image is signed keyless with Sigstore in the project's CI. The
  certificate names the publishing workflow on the main branch, the signature is in the public transparency log, and
  there is no private key on any computer that could be stolen. `:latest` and `:<major>` point at the same digest, so
  they carry the same signature.
- **SBOM:** one SPDX document per platform is attached to the image as a signed attestation and to the release page
  as a file (with checksums).
- **Release gate:** an image is published under its version only after tests, a leak check, the scan and an end-to-end
  run against exactly that published image; `:latest` moves only after that. Version tags never move.
- **Updater:** the one-click updater pulls the new version, runs a self-test without network on a read-only
  file system with all capabilities dropped, starts a trial on a copy of the data, backs up and keeps the old image,
  and rolls back on failure. Before it pulls, it checks the signature of the new image against the publishing workflow
  and the transparency log and pulls exactly the checked digest. After a healthy update it renews itself from that
  same checked image (no second pull), so its own checks never lag behind because nobody renewed it by hand; if the
  renewed sidecar does not report, the old one is started again.
- **No secrets in the repository:** a secret scanner reads every tracked file and the whole git history in CI.
- **Remaining risk:** the trust anchor is the maintainer's account on the code host and the build workflow. A compromised maintainer account
  could publish a signed image; the transparency log would show it. Verify the signature (SECURITY.md) if you pull
  images through mirrors.

### DNS filters

A [DNS filter](DNS-FILTER.md) connection is a device-family read half like the others (one send path, GET-only allow-list, secrets wrapped, a sign-in pause after a refusal) with one more promise: **no query log**. Neither family's allow-list holds a path that returns single queries, a static test fails if one appears, and the browser tests check that the page never asks for one. What the core keeps is two numbers per hour and day; the top lists and per-client counts stay in memory. A client the filter reports is matched to a device by its address only.

### Host agents and container sources

The optional [host agent](HOST-AGENT.md) is a separate program in the same signed image on another host, so it is part of
the model:

- **Token and code.** An administrator (with a recent second confirmation) creates a one-time code that is valid for
  30 minutes. The agent trades it for a token of its own; Lanloom stores only the hash of the code and of the token. Removing
  the entry revokes the token; the agent then stops sending and waits quietly.
- **Metadata only, filtered twice.** Positive lists on the agent, a value filter on both ends, and a check for
  forbidden field names; a test feeds known secrets through a container and requires that none arrives.
- **Read-only clients.** The container client sends `GET` and nothing else, the bus client can only list units and read
  properties, the overlay client only `GET`. Tests check the code for any other method. A read-only mount of a socket is
  not a barrier by itself; use a socket proxy where the separation must be enforced outside the agent.
- **No inbound.** The agent opens no port and the answer to a report cannot carry a command; it never loads code.
- **What the container needs.** The agent runs as the host's super user id with every capability dropped, a read-only
  file system and `no-new-privileges`, and shares the host's network and process view (this is the one place where the
  [container principles](#container-principles) differ, on purpose and only for this optional container). Hosts that
  should not run it can be read through a container source.
- **Egress.** The agent's connections are in `security/egress.json` (category `agent-link`, plus the local sockets) and checked
  like all others; it connects only to the Lanloom it was paired with, optionally pinned to a certificate.

## API tokens and webhooks

- **API tokens** let a script use the public API without a session. Only a SHA-256 hash is stored, the token is shown once and
  is never in a log, a diagnostic export or the audit log (a backup carries the hashes). The scope (read, operate, admin or
  ingest) is never above the role of the account, also later: a demoted account weakens its tokens at once, a disabled or
  deleted account stops them. Every token expires and has its own request limit per minute. A token opens only `/api/v1/...`
  (everything else answers 401), needs no cookie and so cannot be used by another web page (there is no CORS). Making a
  token needs a fresh second confirmation; every state-changing request made with a token is audited with its name.
  Remaining risk: a stolen token works until it expires or is revoked; keep it like a password and use TLS.
- **Webhooks** can be added only by administrators, and only to the address they enter. Deliveries are signed with a
  per-webhook secret (HMAC-SHA-256 over timestamp and raw body) so the receiver can reject forgeries and replays older than
  five minutes. Redirects are never followed, the answer body is never stored, address and secret are encrypted, and the
  outgoing connection is listed in the egress allow-list (`security/egress.json`, category configured channel). Nothing is
  sent before a webhook exists. The payload carries the event's data (for example the name and address of a device), so
  use an address you trust.

## No telemetry, checked

Lanloom does not phone home. The only outbound connection of its own is the optional update check against the public
release feed. Everything else connects only when someone sets it up: integrations to the addresses an administrator
entered, delivery channels, web push to a browser's push service, the heartbeat, the MQTT output (only Lanloom's own
topics, counts only), a cloud AI service, the optional internet check of the control room (a name lookup and a TCP
connection to targets an administrator picks; off until targets are set, never an address of the project), and one TLS
handshake per morning to the connections set up (certificate expiry). As an add-on, Lanloom asks the add-on system's API for its MQTT broker only after an administrator picks it,
and it never asks the release feed.

This is a checked rule, not a promise: `scripts/egress_check.py` reads the shipped code (without running it) and lists
every place that can open a connection and every fixed public host name. Each one must be listed in
`security/egress.json` with a category and a purpose; a new one without an entry fails the tests, CI and the release
gate, and so does a stale entry. The web page itself can only talk to its own server (`connect-src 'self'` in the
Content Security Policy, pinned by a test). The web framework's built-in OpenTelemetry hooks are switched off and no
telemetry SDK or exporter is installed (also tested).

## Container principles

Checked by `tests/test_container_hardening.py` against the Dockerfile, `compose.yaml`, the compose file the installer
writes, compose snippets in the documentation and the containers the updater creates:

- the image runs as uid/gid 10001, numeric, and nothing runs as root after the switch;
- the app and the executor run with a read-only root file system, all capabilities dropped and `no-new-privileges`;
- nothing is privileged, adds capabilities, maps devices or shares the host's PID or IPC namespace - with one exception: the optional host agent of another host shares the host's process view (see [Host agents and container sources](#host-agents-and-container-sources)), still with every capability dropped, a read-only file system and `no-new-privileges`;
- the self-test and trial containers of an update are read-only, drop all capabilities and set `no-new-privileges`;
- the only exception is the optional updater sidecar: it runs as root because it needs the container runtime socket
  (root-equivalent on the host), is opt-in through its own profile, and still has a read-only file system and
  `no-new-privileges`. Leave it out if you do not want that.
- the short-lived helper with which the sidecar renews itself runs from the image the sidecar has just checked, without
  network, read-only, with every capability dropped, `no-new-privileges` and without root: as the owner of the data,
  with the group of the runtime socket added. It has the socket (and so the same reach on the host as the sidecar), but
  only for the seconds of the switch, and it removes itself when done. Where the socket is open to root only, the
  sidecar does not start it and the page shows the manual command instead.
- the add-on image (`addon/lanloom`, add-on mode) starts as root only to create `/data/lanloom` in the add-on volume and
  give it to uid 10001; the start script then drops every group and privilege before Lanloom's code runs (checked by
  `tests/test_addon.py`). The add-on asks for host networking and ingress only: no added capabilities, no devices, no
  privileged mode, no access to the add-on system's API beyond the MQTT service lookup. The add-on system decides the
  rest of the container settings (it offers no read-only root file system), so the dropped privileges are what keeps
  Lanloom unprivileged there.

## Web interface

| Header | Value | Why |
|---|---|---|
| Content-Security-Policy | `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'` | no inline or foreign script, the page talks only to its own server; inline styles stay allowed for layout values the board computes (they cannot run code) |
| Strict-Transport-Security | `max-age=31536000` | only over real HTTPS; no `includeSubDomains`, so other hosts of a local domain are not affected |
| Referrer-Policy | `no-referrer` | addresses in the URL never leak |
| Permissions-Policy | camera, microphone, geolocation, USB, serial, HID, MIDI, payment, screen capture and motion sensors off | the page needs none of them; passkeys and clipboard stay available |
| X-Frame-Options, Cross-Origin-Opener-Policy, Cross-Origin-Resource-Policy | `DENY`, `same-origin`, `same-origin` | no framing, no cross-origin window access, no embedding of responses |
| X-Content-Type-Options, Cache-Control (API) | `nosniff`, `no-store` | no content sniffing, no cached API answers |

Every answer carries these headers, also refusals of the guard (401, 403, 429) and static files; a test checks each
kind of answer.

### Writing endpoints (review)

| Kind | Routes | Protection |
|---|---|---|
| Without a session | first account, sign-in, second step, passkey sign-in, invitation check and acceptance, AI pairing (3), restore before the first account (3) | `X-Lanloom` + `Origin`, flood guard per client (120/min), failure lock-out per client (and per account for the second step), one-time codes stored as hashes; pairing and restore need the setup code, pairing accepts only private peers |
| Signed in | everything else that changes state | role per route (central table, test against every route), step-up for critical actions, flood guard per account (600/min), audit log |
| Host agent | pairing (`/api/agent/pair`) and report (`/api/agent/report`) | pairing: one-time code (30 minutes, stored as a hash), flood guard, failure lock-out per client; report: the token of one agent (stored as a hash, revoked by deleting the entry), body limit 4 MB, 60 reports per call, every report filtered again on arrival |
| Executor | plan hand-over and result | shared token, no session, passkey signature per plan checked by the executor |
| Uploads | backup restore | size limit, one slot per purpose, decrypted only with the passphrase, rate-limited like wrong passphrases |

## How to report

See [SECURITY.md](../SECURITY.md#reporting-a-vulnerability).
