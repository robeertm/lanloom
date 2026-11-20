# Site separation

One Lanloom can keep several [sites](manual/sites.md). Nothing of one site may ever show up in the view of another -
not in an answer, not in a live frame, not for a split second while a page switches. This page lists the rules the
code follows and the checks that hold it to them. Every new route and every new view has to keep them.

## The server

- **The request names its site, the server decides.** A page names its site in the header `X-Lanloom-Site` on every
  request; plain links and downloads name it in the address (`?site=<id>`), the live socket too. The cookie `ll_site`
  only remembers the last site of the browser. The server takes the named site only when it is released for the person.
- **No fallback.** A request that names a site which is empty, unknown, malformed or not released for the person is
  refused with `404 site.err.unknown` on every site-bound route - it is never answered from another site, not even from
  the person's first one. A trailing slash never redirects to another answer (`/api/devices/` is a plain 404). Only a
  request that names no site at all (an API script) gets the person's first released site. The start state
  (`GET /api/state`) answers such a request with `site.unknown` and the person's first site, without any counts.
- **An empty or broken release never means "all".** Only the exact value `*` releases every site; an empty or broken
  site list releases none.
- **Every answer names the site of the data it carries.** The header `X-Lanloom-Site` of an answer is the site whose core
  the request read - not an echo of the request. An answer that read more than one site names `mixed`; the page throws it
  away and the server logs the fault. Every `/api/` answer carries `Vary: Cookie, X-Lanloom-Site` and
  `Cache-Control: no-store`, so no cache can hand one site's answer to a request for another.
- **Routes are site-bound unless listed.** A new route is answered from the core of the request's site (`state.core`)
  without anything to add. Only routes of the instance (own account, users, backups, updates, API keys, the site list)
  are listed in `sites.GLOBAL_PREFIXES`; the firewall editor, the executor and the AI assistant are home-only
  (`sites.PRIMARY_PREFIXES`). A route never reads site data through `state.real_core` or `state.store` - the answer
  would name both sites and be thrown away.
- **Every live frame names its site** (`"site"` in the frame). A socket that names a site it may not see is closed with
  code 4409 before any frame.
- **Ways in stay in their site.** A host agent reports only for the site it paired with (the pairing code belongs to a
  site; a report that names another site is refused with `409 site.err.agent_site`). An external report
  (`POST /api/v1/ingest`) lands in the site it names in the header or the field `site`; with several sites a report
  that names none is refused (`400 site.err.required`), a field that disagrees with the header too (`409`). It is never
  booked for the default site.
- **Links that leave the app name their site.** Notifications, delivered nightly reports and webhook events of a further
  site link to `#/...?site=<id>`, so they open that site whatever the browser showed last.

## The web page

- **One page, one site.** A page shows one site for its whole life; switching cancels every running request, closes the
  live socket quietly and reloads. Nothing of the site that is left settles or draws afterwards.
- **The address decides.** A link with `?site=<id>` asks for that site from the very first request of the page - a cold
  start never draws the remembered site first and then switches. A switch takes only the view along, never a part, a
  filter or a sheet of the site that is left (`LL.viewOnly`), and drops what a view remembered for the next page of the
  tab (a card to open, a step, a focus: `LL.dropTabContext`).
- **Compare after every request and every frame.** Every request goes through `LL.api`, which adds the page's site and
  throws away an answer that names another one. A request that cannot go through `LL.api` (a file, an upload) takes
  `LL.siteHeaders()` and passes its answer through `LL.siteChecked()`. The live feed drops a frame of another site. Both
  are counted in `LL.siteStats` (`discarded`, `frames`) - in a working installation both stay 0.
- **Remembered per site.** Browser storage that holds anything of a site (recent hits, folds, hidden outages, routes of
  the board, chats) uses `LL.siteKey(base)`; plain settings of the browser (theme, language, motion) do not.
- **A site that disappears.** When the server says the page's site is gone or no longer released, the page follows the
  server's first site once (with a short notice) - never in a loop.

## Behind a reverse proxy

The header `X-Lanloom-Site` has to pass the proxy in both directions, and `Vary` must reach the browser. Proxies that
forward every header (the usual setup of the common reverse proxies) need nothing. A proxy with a list of allowed
headers needs `X-Lanloom-Site` and `X-Lanloom` on the request side and `X-Lanloom-Site` and `Vary` on the answer side. A
caching proxy must not cache `/api/` (Lanloom sends `Cache-Control: no-store`) or must respect `Vary`. See
[INSTALL.md](INSTALL.md#behind-a-reverse-proxy).

## The checks

- `tests/test_sites.py`: every route a viewer or operator can call, the live socket and every static file, asked as a
  person who may see one site only - no name or address of the other site in any answer.
- `tests/test_site_split.py`: a person who sees both sites asks every site-bound route for each site, with the shared
  cookie of the other one and with the other site's ids in the address - the answer names the asked site and carries
  nothing of the other one; empty, unknown, malformed and not released sites are 404 through header, address and cookie;
  no trailing-slash redirect; external reports, host agents, links and live frames stay in their site.
- A browser test bench walks every view of both sites at 1512 px and on a phone (A to B to A, a change of the
  view's face after the switch, 30 fast switches while the live feed runs, a cold start with a link into the other site)
  with an observer that reports every piece of the other site the page shows, even for a moment, also in drawn text.
