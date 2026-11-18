# Pi-hole (local API): read adapter

Lanloom reads a Pi-hole (version 6 and later) over its local web API. It asks for **totals only** and never for single queries: the allow-list of the adapter holds no path of the query log, and the only request that is not a plain read is the sign-in. Nothing is changed on the filter from Lanloom.

What you get (see [DNS-FILTER.md](../DNS-FILTER.md) for the views): the share of blocked queries over 24 hours on the filter's card, queries and blocked queries per hour and day, the most blocked names, the busiest clients matched to the devices Lanloom knows, the upstream resolvers with their answer times, the block lists with entries and last update, plain findings (blocking switched off, upstream resolvers silent, no queries at all, an unusual blocked share, old block lists) and a section in the night report.

Never kept: single queries, which name a device asked for, passwords. Per hour and per day only two numbers are stored (queries, blocked). The top lists and the per-client counts are held in memory as the filter reports them for the last 24 hours.

## Create a separate access

1. Open the Pi-hole web page and sign in as an administrator.
2. **Settings → Web interface / API → Configure app password**: create an application password. Use it for Lanloom only; it can be revoked there at any time. (Pi-hole offers no read-only role, so Lanloom's own side is what keeps the access read only: it can send nothing but the requests listed below.)
3. If the web interface only listens on a certain address, make sure the Lanloom host may reach it.

Then in Lanloom: **Settings → Connections → + Pi-hole (local API)**, or accept the suggestion of the setup assistant (it recognises the web page).

## Settings

| field | meaning |
|---|---|
| Address | `http://<host>` (or `https://<host>` when the web interface uses TLS; a port after the host if it is not 80/443) |
| Password | the application password; stored encrypted, never shown again. Empty is only for an API without a password |
| Accept a self-signed certificate | needed for the filter's own certificate (`https://`) |

## What Lanloom sends

One code path sends requests, through an allow-list that is checked before anything leaves the app: `GET` of `/api/stats/summary`, `/api/history`, `/api/stats/top_domains` (blocked, 10), `/api/stats/top_clients` (50, all and blocked), `/api/stats/upstreams`, `/api/dns/blocking`, `/api/lists`, `/api/info/version`, and one `POST` to `/api/auth` with exactly the password. The session is reused until the filter ends it; one reading every two minutes (block lists and version every 15 minutes). After a refused sign-in Lanloom waits 15 minutes before it tries again, so a wrong password cannot lock the account.

## Versions

| release | reads |
|---|---|
| 6.x | everything above; adapter tests against the documented answers of the API |
| 5.x | not supported (a different API) |

Tested versions are listed in [COMPATIBILITY.md](../COMPATIBILITY.md).

## Troubleshooting

| status | what to do |
|---|---|
| Account was rejected | the application password is wrong or was revoked; Lanloom pauses for 15 minutes. The *Test* button tells it at once |
| Sign-in paused for a while after repeated attempts | the filter limited the sign-ins (or Lanloom waits after a refusal); check the password, then wait 15 minutes |
| Unexpected answer | the address does not lead to a Pi-hole version 6 API; version 5 is not supported |
| Not reachable | check address and port; the Lanloom host must reach the web interface |
| TLS / certificate problem | tick *Accept a self-signed certificate* |
| The card shows *No recent reading* | the filter did not answer for 15 minutes; Lanloom keeps trying |

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.
