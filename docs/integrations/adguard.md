# AdGuard Home (local API): read adapter

Lanloom reads an AdGuard Home over its local web API with a **user of its own**. It asks for **totals only** and never for single queries: the allow-list of the adapter holds no path of the query log and sends no `POST` at all. Nothing is changed on the filter from Lanloom.

What you get (see [DNS-FILTER.md](../DNS-FILTER.md) for the views): the share of blocked queries over 24 hours on the filter's card, queries and blocked queries per hour and day (blocked includes the safe-browsing and parental-control replacements), the most blocked names, the busiest clients matched to the devices Lanloom knows (the filter does not report blocked counts per client), the upstream resolvers with their answer times, the filter lists with entries and last update, plain findings (protection switched off, server not running, upstream resolvers silent, no queries at all, an unusual blocked share, old lists) and a section in the night report.

Never kept: single queries, which name a device asked for, the addresses of the filter lists, the filter's own rules, passwords.

## Create a separate access

1. Open the AdGuard Home web page and sign in as an administrator.
2. **Settings → General settings → Users** (or the `users:` list in the configuration file): add a user `lanloom-read` with a long random password. AdGuard Home has no roles, so this user can do what every user can; Lanloom's own side is what keeps the access read only: it can send nothing but the three requests below.
3. Make sure the Lanloom host may reach the web interface (port 80 or 3000 by default).

Then in Lanloom: **Settings → Connections → + AdGuard Home (local API)**, or accept the suggestion of the setup assistant.

## Settings

| field | meaning |
|---|---|
| Address | `http://<host>:<port>` of the web interface |
| User name | the user from step 2, for example `lanloom-read` |
| Password | its password; stored encrypted, never shown again |
| Accept a self-signed certificate | needed when the web interface uses its own certificate (`https://`) |

## What Lanloom sends

One code path sends requests, through an allow-list checked before anything leaves the app: `GET /control/status`, `GET /control/stats` and `GET /control/filtering/status` (the last one every 15 minutes), with HTTP basic authentication. One reading every two minutes. After a refused sign-in Lanloom waits 15 minutes, so a wrong password cannot lock the account (AdGuard Home blocks an address after repeated failures).

The filter keeps its statistics for a time you set (default 24 hours); Lanloom shows what the filter reports and keeps its own sums per hour and day from the readings.

## Versions

| release | reads |
|---|---|
| v0.107.x | everything above; adapter tests against the documented answers of the API |

Tested versions are listed in [COMPATIBILITY.md](../COMPATIBILITY.md).

## Troubleshooting

| status | what to do |
|---|---|
| Account was rejected | user name or password wrong; Lanloom pauses for 15 minutes |
| Sign-in paused for a while after repeated attempts | check the password, then wait 15 minutes (the filter may have blocked the address for a while too) |
| Unexpected answer | the address does not lead to the web interface's API |
| Not reachable | check address and port; the Lanloom host must reach the web interface |
| TLS / certificate problem | tick *Accept a self-signed certificate* |
| Top clients show names instead of addresses | the filter reports a client by its name when it has one; such a line cannot be matched to a device |

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.
