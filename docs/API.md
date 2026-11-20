# API

Lanloom has a public, versioned API for scripts and programs, a way to receive **webhooks** when something happens, and an
endpoint for **external reports**. Everything starts with an **API token** that a person makes under *Settings > API*.

- [Overview](#overview)
- [Tokens](#tokens)
- [Authenticating](#authenticating)
- [Reference](#reference)
- [Errors](#errors)
- [Versioning](#versioning)
- [Webhooks](#webhooks)
- [External reports](#external-reports)
- [Audit log and nightly report](#audit-log-and-nightly-report)
- [Security notes](#security-notes)

## Overview

- The API lives under `/api/v1/` and answers JSON. Reading needs a token (or a signed-in session of the web page).
- The full description is an OpenAPI 3.1 document, built from the declarations of the routes themselves, so it cannot
  drift from what the code does: `GET /api/v1/openapi.json`. *Settings > API* shows it as a readable reference and offers
  it as a download.
- There is **no CORS**. The API is meant for programs and scripts on servers, not for other web pages.
- A token opens **only** `/api/v1/...`. Every other route (the pages of the app, users, settings) answers `401` with
  `api.err.v1_only`.

## Tokens

Everybody makes their own tokens under *Settings > API > Tokens*. Making one needs a fresh second confirmation in the web
page (the same confirmation as for other critical actions). The token is shown **once**; copy it then. Its format is
`llt_` followed by 43 characters.

### Scopes

| Scope | Acts like | May |
|---|---|---|
| `read` | a viewer | read everything the public API offers |
| `operate` | an operator | read, and deliver external reports |
| `admin` | an administrator | everything the public API offers to administrators |
| `ingest` | no role at all | **only** deliver external reports (`POST /api/v1/ingest`); reads nothing. The account must be an operator or administrator |

The scope is never above the role of the account, neither when the token is made nor later: if the account is demoted, the
token works with the lower role at once; if the account is disabled or deleted, the token stops working.

### Expiry, limits, revoking

- A token **always expires**: choose 7, 30, 90 or 365 days.
- Each token has its own **request limit** per minute: 60, 300 or 1200. Above it the answer is `429` with a `Retry-After`
  header (seconds).
- At most **25** tokens per person.
- The owner can revoke a token at any time. **Administrators** see everybody's tokens (under *Other people's tokens*) and
  can revoke any of them.
- The list shows name, scope, expiry and when and from which address a token was **last used**.

### What is stored

Only a SHA-256 hash of the token and its first characters (to recognise it in the list). The token itself is never stored,
never written to the log or the audit log, and never in a diagnostic export. Tokens move with a backup as hashes, so a
restored Lanloom accepts the same tokens.

## Authenticating

Send the token in the `Authorization` header. No cookie and no other header are needed.

```sh
curl -H "Authorization: Bearer llt_..." https://lanloom.example/api/v1/status
```

```python
import json
import urllib.request

req = urllib.request.Request(
    "https://lanloom.example/api/v1/devices?status=down",
    headers={"Authorization": "Bearer llt_..."},
)
with urllib.request.urlopen(req, timeout=10) as resp:
    devices = json.load(resp)["devices"]
for d in devices:
    print(d["id"], d["title"] or d["mac"])
```

Read the token from an environment variable or a file with restricted rights, never from the script itself.

## Reference

Times are seconds since 1970-01-01 UTC (floating point). The examples are shortened answers of the sample network.
Authoritative and complete: `GET /api/v1/openapi.json`.

### General

`GET /api/v1` - the index: the API version, the app version, where the description is, and who you are.

```json
{"api": "1", "app": "1.43.0", "openapi": "/api/v1/openapi.json",
 "you": {"username": "probe", "role": "admin", "via": "token", "token": "nightly-script", "scope": "read"}}
```

`GET /api/v1/openapi.json` - the OpenAPI 3.1 description of everything below.

### Status

`GET /api/v1/status` - the short state of the network: devices, the internet line, connections and the latest report.

```json
{"app": "1.43.0", "t": 1795033556.8,
 "devices": {"total": 39, "online": 37, "offline": 0, "new": 2, "hidden": 1},
 "wan": {"state": "unknown", "last_outage": 1794893040.0},
 "sources": {"total": 8, "fresh": 0, "stale": 0, "overall": "unknown"},
 "report": {"latest": "2026-11-18", "level": "danger", "unread": 1}}
```

### Devices

`GET /api/v1/devices` - all devices. Parameters (all optional): `hidden` (`true` also lists devices hidden from the map),
`kind` (only this kind, for example `printer`), `status` (`up`, `down` or `unknown`).

```json
{"count": 41, "devices": [
  {"id": "d-024c4c000001", "title": "Router", "kind": "router", "ip": "192.0.2.1", "mac": "02:4c:4c:00:00:01",
   "ipv6": [], "vendor": "Example Networks", "model": "Home gateway HG-1", "firmware": null, "status": "up",
   "hidden": false, "gateway": true, "hostnames": ["gateway.lan"], "services": ["HTTPS", "DNS"],
   "sources": ["arp", "ssdp", "tcp"], "zone": null, "merged_into": null,
   "first_seen": 1794687954.7, "last_seen": 1795033554.7}]}
```

`GET /api/v1/devices/{device_id}` - one device (the object above); `404` with `api.err.not_found` for an unknown id.

### Topology

`GET /api/v1/topology` - who hangs on whom.

```json
{"links": [{"child": "d-024c4c000028", "parent": "d-024c4c000002", "port": "9", "medium": "wired",
            "band": null, "ssid": null, "source": "manual"}],
 "infra": ["d-024c4c000002"], "unknown": []}
```

### Availability

`GET /api/v1/availability` - the availability of every device and connection over 35 days, with a summary (subjects, open
gaps, wobbly ones, gaps in 7 days, typical repair time).

```json
{"days": 35, "summary": {"subjects": 0, "open": 0, "wobbly": 0, "incidents7": 0, "mttr_s": null, "stable": 0},
 "subjects": []}
```

`GET /api/v1/availability/{subject}` - one subject, named as in the list (for example `dev:d-024c4c000001`): the last 24
hours in 5-minute slots, 35 daily shares and the outages. `404` if nothing is tracked under that name.

```json
{"id": "dev:d-024c4c000001", "title": "Router", "kind": "router", "state": "up", "share24": 1.0, "share7": 1.0,
 "share35": 1.0, "incidents7": 0, "mttr_s": null, "longest_s": 0, "open": false, "wobbly": false,
 "slot_s": 300, "slots24": [2, 2, 2], "bars": [1.0, 1.0, 1.0], "incidents": []}
```

### Data paths

`GET /api/v1/paths` - bytes per country and per device. Parameter `window`: `24h` (default), `7d` or `30d`. Only countries
and byte counts, never an address.

```json
{"window": "24h", "t": 1795033555.7, "home": "NL",
 "totals": {"tx": 2728975173, "rx": 7684175959, "outside_eu_share": 0.476, "countries": 12, "devices": 13},
 "countries": [{"cc": "US", "eu": false, "tx": 143484831, "rx": 4460965801, "devices": 8}],
 "devices": [{"id": "d-024c4c00001e", "title": "Living room TV", "tx": 31998229, "rx": 3660966940}]}
```

### Reports

`GET /api/v1/reports` - the nightly reports that are kept, newest first, and when the next one is due (`next_at`).

```json
{"reports": [{"day": "2026-11-18", "created": 1794983402.0, "level": "danger", "points": 25, "open": 25, "read": false}],
 "next_at": 1795069800.0}
```

`GET /api/v1/reports/{day}` - one report (`day` is `YYYY-MM-DD`) with every line in the language you ask for. Parameter
`lang` (a language code of the app). `404` for a day without a report.

```json
{"day": "2026-11-18", "level": "danger", "points": 25, "open": 25, "headline": "25 things need a look",
 "sections": [{"id": "devices", "title": "Devices", "level": "warn", "summary": "41 devices", "more": 0,
               "items": [{"id": "gone:d-024c4c000032:1794891600", "key": "report.devices.gone", "level": "warn",
                          "point": true, "text": "Front door cam has not been seen since Tue 17 Nov 05:00."}]}]}
```

### Webhooks

`GET /api/v1/events` - the events a webhook can subscribe to. `available` is `false` for events the installation cannot
produce (see [Webhooks](#webhooks)).

```json
{"events": [{"id": "device.new", "group": "devices", "available": true}]}
```

### Backups

`GET /api/v1/backups` - the backup chains of the control room with their state. A status is `ok` only when a success is known
and recent; a chain nobody reports on is `unknown`, never `ok`.

```json
{"summary": {"ok": 1}, "chains": [{"id": "offsite", "name": "Off-site copy", "kind": "manual", "status": "ok", "level": "ok",
                                   "last_ok_t": 1795032261.2, "expect_s": 86400.0, "restore_checked_t": null, "progress": null}]}
```

`POST /api/v1/backups/{chain_id}/run` - a backup script reports that a chain declared in the control room just finished
(`{"ok": true, "size": 4294967296, "duration_s": 900}`; size and duration are optional and teach Lanloom the throughput). Needs a
token of scope `operate` or `admin`.

```json
{"ok": true}
```

### Ingest

`GET /api/v1/ingest` - external reports delivered recently for the request's site, newest first. Parameter `limit` (default 50, at most 200).

```json
{"reports": [{"id": 2, "t": 1795001155.9, "source": "disk-check", "title": "Disk check of the media box",
              "level": "warn", "summary": "One disk reports reallocated sectors",
              "items": [{"text": "Disk 2: 14 reallocated sectors (was 9)", "level": "warn"}], "by": "admin"}]}
```

`POST /api/v1/ingest` - deliver an external report; see [External reports](#external-reports). Needs a token of scope
`ingest`, `operate` or `admin`.

```json
{"id": 17, "accepted": true, "t": 1795032261.2, "items": 1}
```

## Errors

Errors are JSON with a translation key in `error`.

| Status | `error` | Meaning |
|---|---|---|
| 401 | `auth.err.required` | neither a token nor a signed-in session |
| 401 | `api.err.token` | an unknown or revoked token (with a `WWW-Authenticate: Bearer` header) |
| 401 | `api.err.token_expired` | the token expired; make a new one |
| 401 | `api.err.v1_only` | a token was used on a route outside `/api/v1/` |
| 403 | `api.err.scope` | the scope, or the role of the account, is too low; `need` names the scope that would be needed |
| 404 | `api.err.not_found` | no such route, device, subject or report |
| 400 | `api.err.ingest_*` | the body of an external report is wrong (see below) |
| 400 | `site.err.required` | Lanloom keeps several sites and the external report names none (see below) |
| 404 | `site.err.unknown` | the request names a site (header `X-Lanloom-Site`, `?site=` or the field `site`) that is empty, unknown or not released for the account - never answered from another site |
| 409 | `site.err.mismatch` | the header and the field `site` of an external report name different sites |
| 413 | `api.err.too_large` | the body is larger than the limit (`limit` in bytes); refused before it is read |
| 429 | `api.err.rate` | too many requests with this token; wait `Retry-After` seconds |
| 429 | `api.err.ingest_full` | more than 200 external reports in a day |

## Versioning

The version is part of the address: `/api/v1/`. Within v1 fields are **only added**, never renamed, retyped or removed, so
a client must ignore fields it does not know. A change that breaks this would be a new version (`/api/v2/`) next to the old
one. The version of the app is in `x-lanloom-version` of the description and in `GET /api/v1`.

## Webhooks

A webhook tells another program about an event by calling an address an administrator entered. Only **administrators**
add webhooks, under *Settings > API > Webhooks*; adding, changing and making a new secret need the same fresh second
confirmation as making a token. Nothing is sent before one exists.

Each webhook has a name, an address (`https` recommended; `http` is accepted), the events it wants and a **secret** that
only Lanloom and the receiver know. The address and the secret are stored encrypted and never shown again; the list shows
only the scheme and the host. The secret is shown once, when the webhook is made. At most 20 webhooks.

### Events

| Event | When |
|---|---|
| `device.new` | a device appeared for the first time (not during the first scan) |
| `device.gone` | a device was not seen for a day |
| `device.offline` | a device went offline |
| `device.online` | a device is back |
| `wan.down` | the internet line is down |
| `wan.up` | the internet line is back |
| `incident.opened` | an incident was opened (only offered once the installation has incidents) |
| `incident.closed` | an incident was closed (same condition) |
| `report.ready` | a nightly report was made |
| `backup.stale` | the newest backup is too old, or there is none (only offered where backups exist) |

`GET /api/v1/events` tells which events are `available` on this installation. Besides these there is the test event `ping`
(see [Testing](#testing)). Events about the past (catching up, sample data) are not sent.

### The request

One `POST` per event and webhook, with a JSON body:

```
POST /hook HTTP/1.1
Content-Type: application/json
User-Agent: lanloom/1.44.0
X-Lanloom-Event: device.new
X-Lanloom-Delivery: dlv_...
X-Lanloom-Timestamp: 1795032261
X-Lanloom-Signature: v1=<hex>
```

| Header | Value |
|---|---|
| `X-Lanloom-Event` | the event, for example `device.new` |
| `X-Lanloom-Delivery` | the id of the delivery, **the same on every retry**: drop duplicates by it |
| `X-Lanloom-Timestamp` | seconds since 1970 (UTC), new on every attempt |
| `X-Lanloom-Signature` | `v1=` followed by the hex of HMAC-SHA256 with the secret over `timestamp + "." + raw body` |

```json
{"id": "dlv_...", "event": "device.new", "created": 1795032260.1, "lanloom": "1.44.0",
 "data": {"device": {"id": "d-024c4c000020", "title": "Printer", "ip": "192.0.2.20", "kind": "printer", "vendor": "Example Networks"}}}
```

The `data` per event:

| Event | `data` |
|---|---|
| `device.new`, `device.gone`, `device.offline`, `device.online` | `device`: `id`, `title`, `ip`, `kind`, `vendor` (only those that are known); `device.gone` also `offline_since` when known |
| `wan.down`, `wan.up` | `uplink`: the name of the line |
| `incident.opened` | `incident` (id), `title`, `severity` |
| `incident.closed` | `incident`, `title` |
| `report.ready` | `day`, `level`, `points`, `link` (`#/reports/<day>`, relative to your Lanloom) |
| `backup.stale` | `newest_age_s`, `none` (`true` when there is no backup) |

The body that is retried is the body that was made at the moment of the event.

### Verifying the signature

Always verify before you trust a delivery: recompute the signature over the timestamp, a dot and the **raw body** (not a
re-serialised one), compare in **constant time**, and refuse a timestamp older than **5 minutes** so a recorded delivery
cannot be replayed.

```python
import hashlib
import hmac
import time

SECRET = b"whsec_..."          # from the environment in real code
TOLERANCE_S = 300

def verify(raw_body: bytes, timestamp: str, signature: str) -> bool:
    try:
        sent = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - sent) > TOLERANCE_S:
        return False
    expected = "v1=" + hmac.new(SECRET, timestamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

```js
const crypto = require('crypto');

const SECRET = process.env.LANLOOM_WEBHOOK_SECRET; // whsec_...
const TOLERANCE_S = 300;

function verify(rawBody, timestamp, signature) {
  const sent = Number(timestamp);
  if (!Number.isFinite(sent) || Math.abs(Date.now() / 1000 - sent) > TOLERANCE_S) return false;
  const expected = 'v1=' + crypto.createHmac('sha256', SECRET)
    .update(timestamp + '.').update(rawBody).digest('hex');
  const a = Buffer.from(expected);
  const b = Buffer.from(String(signature));
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
```

`rawBody` is the `Buffer` of the request exactly as received. Answer with any `2xx` status once you have verified it.

### Retries

A delivery counts as done on any `2xx` answer. Anything else is retried with a growing pause; after the last attempt it is
marked **failed**.

| Attempt | Waits before it |
|---|---|
| 2 | 30 seconds |
| 3 | 2 minutes |
| 4 | 10 minutes |
| 5 | 30 minutes |
| 6 | 2 hours |
| 7 | 6 hours |

- Anything but `2xx` counts as a failure: another status, no answer within **10 seconds**, or a redirect. **Redirects are
  never followed**; enter the final address.
- `410 Gone` stops the delivery at once (the receiver says it does not want it any more).
- At most **500** deliveries wait per webhook; older waiting ones are dropped beyond that.

### Delivery log

Each webhook has a log of its deliveries (event, time, status code, kind of error, duration, attempts), kept for **30
days**. The body of the receiver's answer is never stored. A **failed** delivery can be sent again with *Send again*.

### Testing

*Test* on a webhook sends the event `ping` to it at once, with the same headers and signature as a real event, and shows the
result. Use it to check the address and your verification code before you rely on a webhook.

### Changing a webhook and rotating the secret

*Change* edits name, address and events. *New secret* makes a new secret (shown once) and the old one stops working at
once; update the receiver first or right after. *Delete* removes the webhook with its log.

## External reports

Another program (a backup job, a check script, a host of its own) can hand Lanloom a short report of its own with
`POST /api/v1/ingest`. It is listed under *Settings > API > External reports* and appears in the next nightly report, in the
section "API". Use a token of scope `ingest` for this: it may deliver and nothing else.

```sh
curl -X POST -H "Authorization: Bearer llt_..." -H "Content-Type: application/json" \
  -d '{"source": "nightly-backup", "title": "Backup of the file server", "level": "ok",
       "summary": "312 GB, 41 min", "items": [{"text": "3 files skipped (in use)", "level": "info"}]}' \
  https://lanloom.example/api/v1/ingest
```

| Field | Rules |
|---|---|
| `source` | required, up to 60 characters: who delivers |
| `title` | required, up to 120 characters |
| `level` | `ok`, `info` (default), `warn` or `danger` |
| `summary` | optional, up to 500 characters |
| `items` | optional, up to 50 lines, each `{"text": "...", "level": "..."}` (up to 300 characters) or just a string |
| `site` | the site the report is for (the id from *All sites*); **required when Lanloom keeps several sites** - or send the header `X-Lanloom-Site` instead |

- **Sites:** a report belongs to the site it names and shows only in that site's list and nightly report. With a single
  site nothing needs to be named. With several, a report that names no site is refused (`400 site.err.required`) - it never
  lands in the default site; a site the account may not see is `404`, a header and a field that disagree are `409`.

- At most **64 KiB** per report (`413` above) and **200 reports a day** (`429` above).
- Kept for **35 days** (at most 1000 reports).
- The text is stored and shown **as text**: it is never read as markup, and links are not taken over.
- The answer is `{"id", "accepted", "t", "items"}`. A wrong body answers `400` with `api.err.ingest_body`,
  `api.err.ingest_fields`, `api.err.ingest_level` or `api.err.ingest_items`.

## Audit log and nightly report

- Every state-changing request made with a token is written to the audit log with the **name of the token** (and never the
  token). Refused ones are written as well. So are making and revoking tokens and every change to a webhook.
- The nightly report has a section **API** once tokens, webhooks or external reports exist: tokens that expire within 14
  days, tokens that expired overnight, failed webhook deliveries, webhooks that keep failing, and each external report
  delivered that night. See [NIGHTLY-REPORT.md](NIGHTLY-REPORT.md).

## Security notes

- **A token is like a password.** Keep it in a secret store or an environment variable, not in a repository or a chat.
  Prefer the shortest expiry that suits the job and the lowest scope: `read` for a dashboard, `ingest` for scripts on
  other machines that only deliver reports.
- For remote use put Lanloom behind a **TLS reverse proxy** and never expose it directly to the internet
  ([INSTALL.md](INSTALL.md#behind-a-reverse-proxy)). Tokens and webhook secrets travel in the clear over plain HTTP.
- A leaked token: revoke it under *Settings > API* (an administrator can revoke anybody's), then look at the audit log.
- Webhooks go only to addresses an administrator entered, are signed, never follow redirects, and are listed in the egress
  allow-list (`security/egress.json`) as a configured channel. See [SECURITY.md](../SECURITY.md) and
  [SECURITY-MODEL.md](SECURITY-MODEL.md).
