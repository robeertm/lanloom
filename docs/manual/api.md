# API

[Manual](README.md) · [Overview](overview.md) · [Map](map.md) · [Network](network.md) · [Devices](devices.md) · [Inventory](inventory.md) · [Vulnerabilities](vulnerabilities.md) · [Firewall](firewall.md) · [Data paths](data-paths.md) · [Nightly report](nightly-reports.md) · [Automations](automations.md) · [Running](running.md) · [Sites](sites.md) · [Updating](updating.md) · [Settings](settings.md) · API · [AI](ai.md)

## Purpose

Lets scripts and other programs talk to Lanloom without a person signing in: they read the state of the network through a
public, versioned API, receive a signed message (a webhook) when something happens, and hand in short reports of their own
(a backup job, a check script). The page is *Settings > API*.

## What you see

- **Tokens:** your own API tokens with name, scope, expiry, request limit and when and from where each was last used,
  and the button to make a new one. A token is shown once, when it is made.
- **Other people's tokens** (administrators only): everybody's tokens, each with a *Revoke* button.
- **Webhooks** (administrators only): the addresses that get a message, with the events they want, a *Test* button,
  *Change*, *New secret* and *Delete*; the list shows only the scheme and host of an address.
- **Delivery log:** every attempt per webhook (event, status, duration) for 30 days, with *Send again* for failed ones.
- **External reports:** what other programs delivered recently.
- **Reference:** every route of the API, built from the OpenAPI description, with a download button for the description.

## How to use it

1. Open *Settings > API*, make a token, choose its scope (`read`, `operate`, `admin`, or `ingest` for scripts that only
   deliver reports) and how long it lives (7, 30, 90 or 365 days). Confirm again when asked, then copy the token.
2. Send it as `Authorization: Bearer llt_...` to `/api/v1/...`. Examples in curl and Python are in
   [API.md](../API.md#authenticating).
3. Administrators add a webhook with an address, a name and the events it wants, keep the secret it shows, press *Test*,
   and verify the signature in the receiver ([API.md](../API.md#webhooks)).
4. A script that only reports sends `POST /api/v1/ingest` with an `ingest` token; the report shows here and in the next
   nightly report ([API.md](../API.md#external-reports)).

Everybody makes their own tokens, but a token never has more rights than its owner's role. Administrators manage the
webhooks and see and revoke every token.

## Limits

- A token opens only `/api/v1/...`; every other route answers 401. It always expires and never outlives its account: a
  demoted account makes its tokens weaker, a disabled or deleted account stops them.
- At most 25 tokens per person. Each token has a request limit of 60, 300 or 1200 per minute; above it the answer is 429
  with `Retry-After`.
- There is no CORS: the API is for programs on servers, not for other web pages.
- Webhooks: 10 seconds to answer, retried after 30 seconds, 2 minutes, 10 minutes, 30 minutes, 2 hours and 6 hours, then
  marked failed; redirects are not followed; 410 stops at once; the log keeps 30 days.
- External reports: 64 KiB each, 200 a day, kept 35 days.

## Privacy

- **Stored on the host:** only a SHA-256 hash of each token (never the token), the last use (time and address), the
  webhooks with address and secret encrypted, the delivery log (status and duration, never the answer of the receiver) and
  the external reports. Tokens move with a backup as hashes.
- **Sent:** nothing before an administrator adds a webhook. Then only to that address, signed, with the data of the event
  (for example the name and address of a device). Use `https` and a receiver you trust.
- **Audit:** state-changing requests made with a token appear in the audit log with the name of the token.
- **Roles:** everybody makes their own tokens (scope up to their role); administrators manage webhooks and see all tokens.

## See also

- [../API.md](../API.md) - the complete guide: routes, errors, webhooks, external reports
- [../NIGHTLY-REPORT.md](../NIGHTLY-REPORT.md) - the section "API" of the nightly report
- [../SECURITY-MODEL.md](../SECURITY-MODEL.md) - how tokens and webhooks are protected
- [../ACCESS.md](../ACCESS.md) - roles, sessions, the audit log
- [Settings](settings.md) - the other settings pages
