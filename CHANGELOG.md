# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.56.0] – 2026-11-20

1.44.0, 1.44.1, 1.45.0, 1.46.0, 1.46.1, 1.47.0, 1.48.0, 1.49.0, 1.50.0, 1.50.1, 1.51.0, 1.52.0, 1.53.0, 1.54.0 and 1.55.0 below were not released on their own; they are part of this release.

### Added
- **Real cables, a view of the data paths map** (`#/paths?view=cables`): the sea and land telecommunication cables of OpenStreetMap as fine lines under the map, and the traffic of each country running as dots over its measured network routes - over land and between the places of the hops dashed, at each sea crossing over the cable Lanloom estimated for it, which lights up; a country without a measured route gets a dashed arc (*path estimated*). Received data runs towards home, sent data away from it, number and speed by bytes. **Which cable** is always an estimate, never a measurement: from the places of the hops, their round-trip times (light in glass, about 200 km per ms) and where the cables land, with the length along the cable itself; the routes list says *Probably via …*, *Possibly via … or …* or *Cable unknown*, with the reason and the note *estimated route, not a measurement*; nothing of it is stored (computed in memory from the stored route sections). Hover, tap (zoomed in on a phone) or Tab shows a cable's name, length and operator. The country sheet gets the same view: every bubble's traffic arrives over the route that reaches the country. Settings > General > *Cable data* shows source, date, counts and licence; administrators can import an Overpass answer of their own (off until used, nothing is fetched for it, audit entries `paths.cables.import` and `paths.cables.reset`) and go back to the bundled data. New routes `GET /api/paths/cables`, `GET /api/paths/cables/info`, `POST /api/paths/cables/import`, `POST /api/paths/cables/reset`; routes in the data paths answers carry `crossings`, `cables` and `path`. Command palette entries *Data paths: Real cables* and *Data paths: Rain*, all six languages, sample network routes across the North Sea, the Atlantic and the Irish Sea, manual section and screenshots ([docs/DATA-PATHS.md](docs/DATA-PATHS.md#real-cables-and-rain-two-views)).
- **Cable data from OpenStreetMap as a separate file under the Open Database License** (`lanloom/data/cables-osm.json.gz`, © OpenStreetMap contributors, ODbL 1.0; 779 sea cables, 18 land cables), with its notice in the file, `lanloom/data/CABLES-LICENSE.md` (what was changed, derivative and collective database) and an entry in THIRD-PARTY-NOTICES. Built at build time only by `scripts/build_cables.py` (three Overpass queries with pauses; working telecommunication cables only - power cables, pipelines, disused and retired systems left out; checks for licence header, minimum count, geometry, known samples, size budget and checksum before anything is replaced); the app never downloads cable data. The view always names the source and says *incomplete, community-mapped*. Data of commercial cable maps is not used (their databases are for paying subscribers only).
- **Now and Focus on the phone** (`#/now`, `#/focus/<device>`). *Now* is the phone's start into the network: a health ring with the share of devices online, offline and without a reading and the overall word, how many sources are fresh and incidents open, the sites (with several), what changed in the last six hours and the last 24 hours as a strip of five-minute bars with incidents marked - a tap on a moment replays the map from there. *Focus* puts one device in the middle and its partners in a ring around it (internet line, cable, radio, virtual, each in the colour of its trace, a dashed line where Lanloom assumes the link, a small live light on every device), with the same in words below; a tap on a partner moves the focus. Details open in **sheets you can drag**: up for the whole sheet, down to half, further down to close, or a tap on the handle. A link from one sheet to another keeps the way back - the address carries the sheet, the back key returns to the sheet before, forward opens it again, and a reload opens it again. **Pull down to refresh** at the top of *Now*. The **phone bar** keeps *Now*, *Map*, *Control room* and *Devices*, adds the open view and puts every other view behind *More*. **Installed on the home screen** Lanloom opens on *Now*, the status bar takes the colour of the theme, the manifest has shortcuts and symbols for round and square masks. Palette entries *Go to Now* and *Go to Focus*, *About this view* texts, all six languages, the sample network shows it, manual page [docs/manual/now.md](docs/manual/now.md) and screenshots.
- **Energy with cost and CO₂ per lane** (`#/energy`, only with a home-automation hub that has power sensors). Grid, solar and battery feed the home, the home feeds its consumers, drawn like the network: the dots on a trace carry watts instead of bytes (one per 150 W, all at the same pace, still with reduced motion). Every power sensor that is not grid, solar, battery or a home total becomes a lane with its watts, share of the home, cost per hour and CO₂ per hour - counted only for the share the grid supplies right now, so power from the roof or the battery costs nothing; a consumer that is also a network device links to its card. Key figures for the home now, the cost now, today's energy and cost (from a daily counter of the hub or added up by Lanloom) and today's CO₂. Price and CO₂ intensity come from a sensor of the hub (a price per kWh, a CO₂ intensity) or a number an operator enters under *Price and CO₂* (with a feed-in tariff); without anything CO₂ uses 400 g per kWh and no cost is shown. A control-room block *Energy*, a nightly-report section with yesterday's energy, cost, CO₂ and solar share, cards on the start page and on *Now*, a palette entry, new routes `GET /api/energy` (everybody who sees the map) and `PUT /api/energy/settings` (operators, written to the audit log). Nothing leaves the device. The sample house has solar, a battery, a price and a CO₂ sensor and six consumers. See [docs/manual/energy.md](docs/manual/energy.md) and [docs/ENERGY.md](docs/ENERGY.md).

### Changed
- **The data paths map opens in the Real cables view by default**; a two-part switch *Real cables | Rain* above the map, in the full screen and in the country sheet returns to the arcs (*Rain*, unchanged). The choice is remembered per device like the period and travels in the address (`?view=cables|rain`; a link shows its view once without changing the stored choice), the map draws the same sum in both views, and with *reduce motion* no dots run in either view. In the Real cables view the measured routes are the paths the dots run on, so their separate quiet lines are drawn in the Rain view only.

## [1.55.0] – 2026-11-19

1.44.0, 1.44.1, 1.45.0, 1.46.0, 1.46.1, 1.47.0, 1.48.0, 1.49.0, 1.50.0, 1.50.1, 1.51.0, 1.52.0, 1.53.0 and 1.54.0 below were not released on their own; they are part of this release.

### Added
- **MikroTik RouterOS family, read only** (stage A; product names identify compatible products only; no affiliation or endorsement; guide [docs/integrations/mikrotik.md](docs/integrations/mikrotik.md), entry in [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)). Lanloom reads a router with RouterOS 7.1 or later through its REST interface with a user whose group only reads: the router and the neighbouring equipment, ports (link, speed, power over Ethernet, untagged and tagged VLANs), networks with DHCP range and VLAN, clients from leases, the address table, the bridge host table and the wireless registrations, static leases, port forwarding, the internet connections and the filter rules (shown, never written). The adapter sends `GET` requests on fixed paths and nothing else - the router's REST interface also takes changes and commands through other methods, and none of them can leave the app - and refuses a user whose group can change anything where the router lets it check. The setup assistant suggests it for a router it recognises, with a guide for the read-only user; the demo has a sample switching router.
- **Checks and night report section for the switching router family:** an internet connection is down, no enabled rule on the input chain drops unwanted traffic, filter rules are switched off; since the last report: the router started again, a new release, neighbouring equipment appeared or vanished, an internet connection went down or came back, filter rules and port forwarding added, removed, switched or acting differently, and devices the router had not known. All texts in the six languages of the app.

### Fixed
- **Spanish and Italian numbers read alike everywhere.** A four-digit whole number is written without the grouping separator in both languages (1234, but 12.345); the page now applies that rule itself instead of relying on the browser's number data, so older browsers and the server-side report texts agree.

## [1.54.0] – 2026-11-19

1.44.0, 1.44.1, 1.45.0, 1.46.0, 1.46.1, 1.47.0, 1.48.0, 1.49.0, 1.50.0, 1.50.1, 1.51.0, 1.52.0 and 1.53.0 below were not released on their own; they are part of this release.

- **Plain wording in the translations and one screenshot cropped to a screen.** Dutch, French and Spanish texts say *wireless network* instead of the trademarked spelling, a few labels name the signal strength instead of the bare word, and the control room picture in the README is cut to a real screen height.

### Added
- **The network in 3D**, a third face of the Network view next to the board and the tree (*Board | Tree | 3D*, `#/network?v=3d`): the physical chain as a stack of circuit boards - Internet on top, then the gateway, switches and access points, devices at the bottom - with every component as a chip and status light on its board, cables as copper traces with vias down to the board below (wireless dashed, guests dotted) and the live traffic running along them as dots, like on the flat board. Devices in no chain yet get a board of their own. Drag or one finger turns, right drag or *Shift* moves, the wheel or two fingers zoom; buttons zoom, turn and reset. Keyboard: one tab stop, arrows turn and tilt, `+`/`-` zoom, `0` resets, `Page Down`/`Page Up` walk the chain with the camera following and the element read out, a letter jumps by name, `Enter` opens the card; pointer and focus light the chain up to the Internet. It draws with the graphics card only: a software renderer or a browser without WebGL gets a note and the flat board (*Try anyway* is remembered per browser and then draws fewer frames and dots). Frame budget as on the board: nothing is drawn while nothing changes, the motion setting caps the frame rate, it steps down on its own when frames take too long (also when the time is spent outside the script) and rests while hidden or out of view; reduced motion shows only the glow. Plain WebGL, no new library and no new connection. Command palette entry, keyboard help, all six languages, the sample network shows it, manual section and screenshots ([docs/manual/network.md](docs/manual/network.md#the-network-in-3d)).

## [1.53.0] – 2026-11-19

1.44.0, 1.44.1, 1.45.0, 1.46.0, 1.46.1, 1.47.0, 1.48.0, 1.49.0, 1.50.0, 1.50.1, 1.51.0 and 1.52.0 below were not released on their own; they are part of this release.

### Added
- **Resilience: risk lens, weak spots and emergency scenarios.** A new page under the control room (`#/resilience`) shows how well the network copes when something fails: a score with a grade from A to F and its last 90 days, the ten weak spots with the one step that would help most and its size, emergency scenarios and the layers everything rests on. Every part is rated on seven axes - impact (what fails with it, from the failure chain), spare, backup (chains of the control room and when a restore was last tried), power, up to date (vulnerability hints, end of updates, certificates), watch (reachability and a second source) and health (availability of 35 days, early warnings); an axis without a source counts as half weak, never as fine. Scenarios: a *power cut* with a slider from 0 to 240 minutes over the battery backup's live runtime (minus a reserve for a clean shutdown), with what is off at once, what runs on the battery until when, when the home automation hub stops and which power sources nobody noted; *internet down* with the reserve line and the automations that keep running locally; the gateway, storage boxes, servers and the hub host *off*. *Show on the map* opens a scenario in the failure bar with several roots. The **risk lens** colours the tiles of both maps by risk level, with the number from *elevated* up (chip above the map, key `B`, palette). Power source (UPS, mains, own battery, PoE from its switch) and spare (second unit, partly, spare on the shelf, none) are new fields per part on the component card; PoE follows the switch, guests and containers their host, and the device a UPS reports through counts as on the UPS. A dependency entered by hand can now point at the *Internet*, so the scenario names the services that need it. Palette: *Go to Resilience*, *Risk lens on/off*, *Emergency scenarios*, *Simulate a power cut*, *Simulate the internet going down*, the hit action *Risk and resilience*; incident files get *Risk of the root*; the overview gets a resilience card; the nightly report a section when the score moved by five points or a part is critical. Changing the fields or the battery figures needs an operator and is written to the audit log. Everything is a simulation: nothing is switched and nothing is sent anywhere. See [docs/RESILIENCE.md](docs/RESILIENCE.md) and [the manual page](docs/manual/resilience.md).
- **Four more languages: French, Spanish, Dutch and Italian**, complete (about 6,900 texts each, the app and the map), chosen like English and German: the browser's language is the first guess, *Settings > Appearance* (the language picker) and the account preference change it. Numbers, dates and times follow the language (French narrow-space grouping, Spanish and Italian without a separator in four-digit numbers, day order per language), plurals follow the language's rule (French counts zero as singular), the AI features answer in the chosen language, the delivered night report writes its dates and weekdays in it, and the PDF export carries the characters of all six languages. New checks: the existing completeness checks run over every language, texts equal to the English original must be listed per language with a reason, short labels may not balloon, search words are lowercase word lists, and a browser check reads the main views in every added language at 1512 and 390 px for raw keys, page errors and cut-off text. See [CONTRIBUTING.md](CONTRIBUTING.md#translations) for how corrections arrive.
- **Home router family, read only** (stage A; product names identify compatible products only; no affiliation or endorsement; guide [docs/integrations/fritzbox.md](docs/integrations/fritzbox.md), entry in [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)). Lanloom reads a FRITZ!Box through its documented local interface (TR-064) with an account made only for Lanloom: the host list (name, address, wired or wireless, active now), the mesh topology (which repeater, which LAN port, which radio band each device hangs on), the box and its mesh devices with model, firmware and state, the home network with its DHCP range and the state of the internet connection. Hosts and mesh feed the topology and the inventory every minute, everything else every five minutes; behind a router that Lanloom does not otherwise see into, the network view now shows the real tree. The device log and the serial number the box hands out are read and dropped, no hardware address leaves the translation module. The setup assistant suggests the family after a gentle scan (page title and device announcement) with a step-by-step guide; texts in English and German; a *Sample home router* in the demo.
- **Checks and night report section for the home router family:** the internet connection is down, a mesh device is not connected; since the last report: mesh devices gone offline, back, joined or left, firmware changes of the box or a mesh device, and devices the router had not known at the last report (a note with the first names). The section appears only when the connection exists.
- **Read-only SOAP in the integration transport:** a read allow-list can hold a *soap* rule per control path - a table of the `Get` calls it may make with their argument names and value checks; the transport builds the envelope from the checked row (a call that is not a row, or any non-`Get` action, cannot be expressed or sent) and answers the device's HTTP digest challenge itself, reusing it. A refused sign-in pauses the family for 15 minutes; sign-ins from a fresh challenge are capped per hour.
- **Network routes, an option that is off by default** (Settings > General > Network routes; administrators change it, operators can measure at once): per destination group of the data paths (the 5 with the most bytes by default, 1 to 20, each at most every 6 hours) the measured chain home, provider, exchange point, transit, destination network and destination with the time each section adds, in the country sheet and on the world map (quiet lines, a list, a click frames the route; estimated parts dashed). One probe per hop from a worker thread with the lowest priority, never while the machine is busy; UDP and TCP Paris-style probes over ordinary sockets, no extra container rights. Places come from router names (the new table `lanloom/data/route-places.json`, built from OurAirports by `scripts/build_route_places.py`), exchange points and the city data, each checked against the round-trip time. Stored for 30 days: networks, operators, cities and time bands, never an address or a host name. A calm *route changed* note (also in the night report), keyboard and phone layout, sample network routes, command palette entry. Optional *Exchange points from PeeringDB* (off by default; three public lists, at most one check a day, kept only for this instance and deleted when switched off).

### Changed
- The country sheet places traffic without a city at a measured place when a route knows it: with network routes on, the approximate bubble of an operator is drawn at the place of its destination network in that country instead of the middle of the country (still approximate, the sum rule is unchanged).

### Fixed
- **Switching sites never mixes two sites.** Every request of a page carries the site the page shows (header `X-Lanloom-Site`; the cookie that remembers the last site is shared by all tabs of a browser and could not tell), every answer of a person's session names the site it was made for in the same header, and `Vary: Cookie, X-Lanloom-Site` keeps any cache from handing one site's answer to a request for another. An answer that names another site than the page shows is thrown away, never drawn; the page follows the server once (no reload loop). A tab keeps its own site while another tab switches, in its requests, in the live feed (the socket now carries `?site=`; the server closes it with code 4409 when that site is not, or no longer, released for the person) and in what it draws.
- **Leaving a site is quiet.** When the page switches, every running request is cancelled and never settles (no error, no stale band, no "Disconnected" in the live indicator) and the live socket closes without a message. Browser storage that belongs to one site (the hidden outage, the unfolded hosts, recent palette choices, the assistant's talk, section folds) is kept per site.
- **Deep links into another site** (`#/devices/<id>?site=<id>`) switch before anything is drawn, also from a cold start (signed out, sign in, then the link); a link into a site the person may not see, or that is gone, opens the current site with a quiet notice.
- **A request cut off half way is a failed request, not an empty answer.** The page's request helper treated every error while reading an answer as "empty body" and handed the views `{}` as if it were a complete answer; a page that was left in the middle of an answer (or a dropped link) then threw script errors such as reading `items` of the outage list or `moves` of the firewall draft. Only a truly empty body counts as an answer without data now; a body that breaks off fails the request and the view keeps what it shows and asks again. The outage list also ignores an answer that lacks its lists.

## [1.52.0] – 2026-11-19

1.44.0, 1.44.1, 1.45.0, 1.46.0, 1.46.1, 1.47.0, 1.48.0, 1.49.0, 1.50.0, 1.50.1 and 1.51.0 below were not released on their own; they are part of this release.

### Added
- **Wireless networks and blocked devices in the firewall editor** (stage B, through the executor like every change; simulation by default). A *Wireless networks (n)* card lists every SSID with its network, bands, security and the devices that use it; in Change mode *New SSID* and *Change...* open a dialog for name, network, bands (2.4, 5, 6 GHz), security (open, WPA2 personal, WPA2/WPA3 personal, WPA3 personal) and hidden; an SSID can be switched off and on and deleted when Lanloom created it. Never written: radio settings (channel, width, transmit power), enterprise (RADIUS) security, the network or security of an SSID with private pre-shared keys. Passwords are never part of a plan, a preview, the history, a snapshot or a log: a new protected SSID (or an open one that becomes protected) gets a random password of 24 characters from the executor, to be read, shown as a QR code or replaced in the controller's own app. Protection, checked in the preview and again by the executor with its own fresh read: a change that drops clients (off, delete, rename, another network, a dropped band, another security) is refused while a protected device, the approver's device, the executor, the controller or Lanloom's host is connected over that SSID; hiding never drops anyone. An open SSID only on a guest network and only with its own confirmation; the management network on an SSID needs its own confirmation and is never open there; 6 GHz only with WPA3 personal; names are unique; one change per SSID and device and plan; a warning when more than 4 SSIDs share a band.
- **Block and unblock a device:** a *Blocked devices (n)* card, a *Block a device...* picker and a block switch in the device sheet. Always its own confirmation; never a protected device, the approver's own device, the executor, the controller or Lanloom's host; unblocking goes back on the way back (undo). Disconnecting a device (a kick) is never offered because it comes back at once.
- **UniFi family writes wireless networks and blocks** (product names identify compatible products only; no affiliation or endorsement): the executor's write half creates, changes and deletes SSIDs over the classic API (`rest/wlanconf`) with a field allow-list and blocks or unblocks a device with `cmd/stamgr` (`block-sta`, `unblock-sta`; `kick-sta` is refused), with the same local account as the other stage-B changes. Capability keys `ssid.create`, `ssid.update.<field>`, `ssid.delete` and `client.block` (confirmed separately). Tested against a recorded test fake; real writes only after acceptance at a tester. See [docs/integrations/unifi.md](docs/integrations/unifi.md#wireless-networks-and-blocked-devices-stage-b).
- **Night report section *Wireless networks*** (only when a connection reads wireless networks): a baseline the first night, then new, gone and renamed SSIDs, a changed network (point), a changed security, a password removed (danger point), an SSID switched off (point), hidden or visible again and devices blocked or unblocked; names only. Texts in English and German.
- Command palette: *Change a wireless network* (`#/firewall/wireless`) and *Block a device* (`#/firewall/block`).
- Sample network: 6 wireless networks and one blocked device (*Visitor phone*) to rehearse with.
- **OpenWrt family, read and rule changes** (stage A, and stage B through the executor; product names identify compatible products only; no affiliation or endorsement; guide [docs/integrations/openwrt.md](docs/integrations/openwrt.md), entry in [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)). The read half signs in to the router's ubus endpoint with a login whose access list only reads, and every JSON-RPC call passes a check of object, method and arguments below the adapter (sign-in, board, `uci get` of four configurations, interface and device state, host hints and leases, wireless state); one session of 15 minutes for many polls, at most four sign-ins an hour, a refused password pauses for 15 minutes, a call the access list leaves out is skipped for an hour without a new sign-in. Firewall zones become zones and the router itself the zone *This router*; rules appear in configuration order, followed by the zone forwardings and each zone's input policy, pairs nothing decides follow the global forward policy; port forwards, fixed addresses, wireless networks (band, security, hidden, on/off, clients on air), clients (leases, host hints, wireless clients, which hang below the router in the topology), WAN links, plain checks (management open on the internet side, open rules from there, any to any, forward policy *accept*, rules switched off, an open wireless network, a WAN link down) and a night report section *Router*. Options that hold a secret (wireless keys, dial-up passwords, tunnel keys, PINs, certificates) are dropped on arrival; a login whose access list allows any change is refused. Unnamed UCI sections change their name when a section before them is added or removed, so rules are referred to by their name and position among rules of that name; rules without a name are shown, never changed.
- Router rule changes through the executor (stage B, a login of its own): add a rule from one zone to another, to the router itself, change and switch simple rules, remove Lanloom's own rules (named sections `lanloom_…` marked `[lanloom]`). Changes are staged in the session, then put live with `uci apply` and the router's own revert timer (90 seconds); the executor confirms only after reading every changed rule back, rolls back at once on trouble, and the router reverts by itself if the executor is cut off. A field-exact allow-list of calls sits below the adapter (only `lanloom_…` sections can be deleted as a whole). Checked against the real API of the 22.03, 23.05 and 24.10 root file systems in a test container, and in the adapter tests against a stateful stand-in (session changes, the timer, renamed sections; also a firewall3 configuration).
- Demo: a *Sample router* with zones, rules, wireless networks, clients and a night report section.
- **Control room** (tab *Control room*, `#/controlroom`, also `#/wan`, `#/nas`, `#/backups`; command palette entries). One page with a block each for the gateway, the internet, switches, storage boxes, battery backup, home automation, the DNS filter and backups, a list of what needs a look, and the freshness of every connection. Each block says how old its picture is; a reading that went quiet for 15 minutes is dimmed and counts as unknown, never as fine. Everything only reads. See `docs/CONTROL-ROOM.md` and the manual page.
- **Internet watch with two readings.** What the gateway says (the `wans` list of a controller family, or the uplink port over SNMP) and what Lanloom measures (an optional probe: a name lookup and up to four TCP connections to targets an administrator picks, every 20 seconds; nothing is probed until targets are set). A fresh measurement wins over a disagreeing gateway; a flap guard (3 equal results to go down, 2 to come back, four changes in ten minutes = flapping) writes one history line instead of twenty; the history is 15-minute slots for 35 days plus a log; the gateway's own event stream is merged where an adapter has one. A reserve line (role backup) shows whether it carries the traffic and how often the line switched.
- **Mobile-router card.** Signal quality from RSRP, RSRQ and SINR (one threshold table, the worst measure counts), bands with duplex mode, centre frequency and role (coverage or capacity), technology, data volume against its limit; read from the new `cellular` list of an adapter or from the entities of a home hub. Cell id, tracking area and position are administrator data.
- **Storage box card.** A ring for the fullest volume, every volume (HOST-RESOURCES-MIB, host agent, hypervisor), processor, memory, temperature, containers, virtual machines, the backups of the box and the battery backup that carries it.
- **Battery backup (UPS).** A new connection type for the Network UPS Tools protocol (read only: `LIST UPS`, `LIST VAR`, `VERSION`, `LOGOUT`, no login) and the UPS-MIB of an SNMP agent; state, charge, load, voltages, the UPS's own runtime estimate (about X min if the power fails now / left on battery), a state log for the nightly report. The device class *UPS* of the connect catalog is available.
- **Backup chains.** One list for hypervisor guests, the backup of a home hub, host-agent timers whose names say backup, and chains you declare. *Current* only with a recent success and a fresh source; *Cannot be judged* when nobody reports (never *current*); *No backup* for a guest outside every job; progress as a bar instead of a stopwatch, estimated from the throughput learned from earlier runs or reported by the script; *Restore last checked on ...* (a date you set, with a hint when missing or older than 180 days).
- **Public API:** `GET /api/v1/backups` and `POST /api/v1/backups/{chain}/run` (scope operate) so a backup script can report its run.
- Nightly report section *Control room* (internet outage or flapping, switch to the reserve line, battery backup on battery, backup failed, overdue or not judged).
- Sample network: two internet lines with a mobile reserve, a UPS, a storage box with a volume at 87 %, a week of history with an outage and a night on the reserve line, backup chains in every state.
- Telemetry list: two new places that open connections (`own-internet-check`, `configured-source`) with their purpose; there is still no telemetry.

### Changed
- Wireless networks show more security kinds in plain words (encrypted open, WEP, WPA personal and enterprise) in every language.
- Setup assistant: a device recognised by its plain web page on port 80 is pre-filled with an `http://` address.
- Wireless network security in the neutral step contract uses the names the adapters read: `open`, `wpa2_personal`, `wpa2_wpa3_personal`, `wpa3_personal`.
- Neutral model: optional lists `wans` (now with `up`, `active`, `role`, `kind`, rates, latency), `cellular`, `wan_events` and `ids`; the first gateway family fills the line state, rates and roles of its internet lines from the gateway's own record.
- SNMP host values carry every fixed disk as a volume (`volumes`) next to the fullest one.

### Fixed
- **Requests have a deadline.** Every request through `LL.api` ends after 20 s (120 s for the ways that really work long: AI, update, backup, export and import, the vulnerability feed, clean-up, scans and tests of connections; a caller can set its own) with a calm error ("The request took too long and was stopped") instead of waiting for ever. A change that ran into its deadline is never sent a second time. Before, a server, reverse proxy or network that never answered left a view stuck until the page was reloaded.
- **No view is blocked by a request that never ends.** The "one request at a time" locks of the map, the data paths view and its country sheet, and the port front now give a request up after max(30 s, three cadences) and ask again; a late answer of the abandoned request is not drawn. The firewall view keeps its state and asks again after a failed request instead of staying on the error.
- **A stale view says so.** Every view that shows its own data remembers the age of its last good state; when it is older than three cadences (at least three minutes) a calm band "Data stale since HH:MM - refreshing by itself" appears over the view (all languages), the content is dimmed, and the view is asked again by itself every 15 s. It is checked every second against the clock, so it also appears when no answer arrives at all, while the live channel still says *Live*; the next good state removes it.
- **Back from sleep or after a network change, views fetch again at once** (the tab becomes visible, `online`): every visible view whose state is older than 30 s asks immediately; a state of a few seconds is left alone so that open parts and focus stay. A page that could not start (no state) starts again by itself.

## [1.51.0] – 2026-11-19

1.44.0, 1.44.1, 1.45.0, 1.46.0, 1.46.1, 1.47.0, 1.48.0, 1.49.0, 1.50.0 and 1.50.1 below were not released on their own; they are part of this release.

### Added
- **Country detail in the data paths view.** A click on a country (map or list) opens a large sheet (a full screen on a phone; Esc, ×, a click beside it and the back gesture close it; deep link `#/paths/DE`, command palette *Data paths of …*): the country zoomed on a finer map (the base map 1:50m and places for orientation, public-domain data, loaded from the app's own files when the sheet opens, never from another server; the map zooms up to 64 times, lines and bubbles keep their screen size, smaller on small maps), key figures, history per hour or day, cities, network operators, devices and destinations (*operator · city*). Bubbles show the places the devices talk to; **the bubbles, the device list and the header always add up to the same number**: traffic without a known city is drawn as a dashed, approximate bubble at the operator's largest known city in that country or in the middle of the country, never dropped. The number of operators in the header equals the list (*Show all* above 40); sizes use the unit that fits (B to TB) in the language's number format. Route `GET /api/paths/country/<code>`.
- **Cities and network operators, an option that is off by default** (Settings > General, administrators; `paths_detail`). The city list and the operator list (*IP to City Lite* and *IP to ASN Lite* by DB-IP, CC BY 4.0) are not part of the image (about 90 MB to download). With the option on Lanloom fetches exactly these two files from their public source, checks at most once a day and loads a new file once a month; the files are read as a stream (nothing is held whole in memory), condensed into one file of about 90 MB in the data folder (a real build of the 2026-11 files: 194,000 cities, 83,000 operators, peak of 41 MB of Python heap) that is mapped read-only, and replace the old one only after the checks (size window, complete gzip, enough rows, ascending ranges, known samples; free space first); the previous file stays as the fallback; a failure keeps the old file or says *not available*, never 0. The settings card states what is fetched and what is stored, shows state, month, counts, size and the last problem, and has *Check now* and *Delete files and stored figures*. Stored (only with the option on): per hour and day the country, the city of the peer (name and region of the centre, never a position of the user), the operator and the device with the bytes; at most 250 cities and 250 operators per country and day, more are condensed into *other*; anycast, own and unassigned traffic never get a city; no addresses. New tables are part of backups, the storage page and the retention. The download is the single entry `lanloom/geocity.py::http_download::http` of the new category *optional-download* in `security/egress.json`; the nightly report notes a new file or a failed fetch. Attribution *IP Geolocation by DB-IP* in the sheet, the settings, the README and the third-party notices.
- Sample network: cities and (invented) network operators with approximate bubbles for the data paths of the sample countries.
- Tests: the streamed build with a check file (RFC 5737 and RFC 3849 ranges, invented names): merge of ranges, gaps, IPv6, fallback and pruning of files, daily limit also across a restart, cut-off and damaged sources, size window, free space, only the source host; booking with the option off and on, caps, the sum rule over all periods, no address in the database, retention; browser checks of the sheet at 1512 and 390 px.

### Changed
- `LL.fmtBytes` writes terabytes (TB) for sizes from 1 TB on (everywhere in the app).
- `scripts/build_worldmap.py` takes the populated places file as an optional third argument and then also writes `worldmap_fine.js` and `worldplaces.js`; `worldmap.js` stays byte for byte the same; `--exclude-places FILE` leaves out places whose names are listed in a file outside the repository.

### Fixed
- Sample network: the three dropouts of the flapping speaker were placed by hours before now, so depending on the minute the history was created they could land in adjacent slots and merge into one outage; the sample incident test failed at random (it broke the image build of a release train). They are now whole slots with one good slot between, and a test covers every phase of the clock.
- Plan export (SVG, PDF, draw.io): a line between zones in different rows of the plan went up or down the column gap beside its start pad all the way to the other row and could run across the pads of the zones in the rows between. It now stays inside the two zones, runs along the gap between the rows and, when other rows lie between them, along the left margin of the plan, which no zone reaches; a test with 8 generated layouts (adjacent rows, rows between, a full-width zone on top) covers it. The plan screenshots are renewed.
- English texts of the incident files: no curly quotation marks around the name of a maintenance window, and the wording around the maintenance window (incidents page, About this view, nightly report docs, manual) follows the product's style rules; incident screenshots renewed.

## [1.50.1] – 2026-11-19

1.44.0, 1.44.1, 1.45.0, 1.46.0, 1.46.1, 1.47.0, 1.48.0, 1.49.0 and 1.50.0 below were not released on their own; they are part of this release.

### Fixed
- Notification history: a condition that clears on its own and is reported at every reading (an unstable switch port) wrote a new history row every few seconds when its rule was *Nightly report* (or nobody could be told right away), until the history showed nothing else. It is now one row while the condition lasts, marked as resolved when it clears; an all-clear still goes only to those who were told.
- Network plan export (SVG, draw.io, PDF): a line between two devices of one zone that are not neighbours ran straight across the devices between them (seen with a switch and two access points in one row). It now goes around them through the gaps between the rows.
- Test suite: the hang guard watches progress instead of the clock, so the performance budget (minutes of measuring on a busy machine, in one test) no longer ends the whole run; a test that is really stuck still ends it after the limit.

## [1.50.0] – 2026-11-19

1.44.0, 1.44.1, 1.45.0, 1.46.0, 1.46.1, 1.47.0, 1.48.0 and 1.49.0 below were not released on their own; they are part of this release.

### Added
- **Rewind:** the last 24 hours (or 7 days) played back on the Map and Network boards. A bar above the board plays, pauses and steps, at 60x, 300x, 900x or 3600x; the tiles take the state of that moment (down, answered only part of the time, not checked), the dots carry the traffic of that minute (7 days: of those five minutes); a time line shows the activity, the incidents as coloured bands and the stretches when Lanloom itself was not running. Keys `Space`, `←` `→` (with `Shift` an hour or a day), `1`-`4`, `W`, `R`, `Esc`; `R` on a map opens it. Command palette: *Rewind the last 24 hours*, *Rewind the last 7 days* and *Rewind to …* with a typed time (`14:30`, `yesterday 9:15`, `mon 8:00`, `3 h`; German words too). On the phone the bar is a sheet with *At this moment* open.
- **Incident files** (`#/incidents`): faults bundled into incidents from the availability slots and the event journal. Devices that go down within ten minutes of each other are looked at together; a part that hangs on another one in the failure chain is listed under it (sure or probably), and several parts behind a part that kept answering name it as the probable root. Kinds: outage, flapping (three or more drop-outs in a row), blip (one short drop-out) and maintenance (a maintenance window, Lanloom's own update, or marked by an operator - never a fault). Phones and computers never start an incident on their own. A file shows the probable root with its reason, what changed before (new devices, own updates, controller changes, a failed connection, a changed internet line, firmware jumps of the day before), the timeline, the availability around it, the affected parts and the healing, with *Play in rewind* and *Failure chain*. Rows are kept 120 days.
- **Calendar of 90 days, heatmap of the last 7 days by hour, the event chronicle and *Timeline & data state*** (firmware and software jumps, own updates, new devices and outages longer than an hour, collected by Lanloom once a day and kept 400 days).
- **What happened?** on the card of a device that is down or was down in the last 24 hours: since when, the probable root, how many parts went with it and the latest change before.
- Start page: the key figure *Incidents* and the card *Incidents of the night* (also on the phone), a line in *Noteworthy* for a running incident that took other parts with it. Nightly report section *Incidents of the night*. *About this view* and a manual page ([docs/manual/incidents.md](docs/manual/incidents.md)); screenshots.
- Operators can mark an incident as maintenance or add a note (`PUT /api/incidents/<id>`, audit action `incident.mark`); reading (`/api/rewind`, `/api/incidents/...`) is for every role. The sample network has incidents to look at: the core switch took eleven parts with it three days ago (a firmware jump the evening before), the same switch inside its maintenance window, a flapping speaker, two devices behind an access point that kept answering, the garden access point down right now, and 90 days of history for the calendar.
- Performance budget: `rewind_ms` and `incidents_ms` in `scripts/benchmark.py` and `tests/test_budget.py` (finished days are kept encoded; the running day only adds its new steps).

### Fixed
- Firewall editor: an answer of the tab could show a finished change next to the state from before it, when the change ended while the page was asking (seen under load as a new port forward or guest setting missing after *done*). The job list is now read before the state, so *done* always comes with what the change wrote.
- The front panel of a switch, router or access point follows the light theme (a light plate instead of a dark one on a light page; the dark theme is unchanged), and redrawing it when the page is resized no longer makes the browser report a resize loop. The light screenshots of the front panel and the switch port editor were taken again.
- Long section titles of the nightly report (a source name such as a wireless controller's) wrap on a phone instead of running out of the screen.
- Overview: a device that a controller reports as disconnected before Lanloom saw it go read *offline for {age}*; without a known start the line now just says *offline*.

## [1.49.0] – 2026-11-19

1.44.0, 1.44.1, 1.45.0, 1.46.0, 1.46.1, 1.47.0 and 1.48.0 below were not released on their own; they are part of this release.

### Added
- **OPNsense family, read and rule changes** (stage A, and stage B through the executor; product names identify compatible products only; no affiliation or endorsement; guide [docs/integrations/opnsense.md](docs/integrations/opnsense.md), entry in [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)). The read half uses an API key of a user made for Lanloom and sends `GET` requests to a fixed list of paths only: interfaces become zones (with network, VLAN and address), interface groups zones of their own, the firewall itself the zone *This firewall*; rules appear in the firewall's evaluation order (floating, group, interface; from release 25.1 also the classic rules, read only), with aliases, source NAT, NPT and 1:1 NAT, destination NAT (26.1 and later), fixed addresses, static routes, gateways and VPN instances. Fields that carry keys, secrets, passwords or certificates are dropped as the reply arrives.
- **Rule changes for this family through the executor** (a key of its own): add a rule between two interfaces or to the firewall itself (marked as Lanloom's own), change or switch simple rules, remove own rules; every field checked by the executor's allow-list; written into the configuration and put live with one apply. Way back: Lanloom's backup on every release, plus the firewall's savepoint and 60 second revert timer on releases 23.1 to 26.1 (the executor confirms only after reading every changed rule back; release 26.7 dropped the savepoint, the capability list then says so).
- **Checks and night report section for gateway firewalls with rules on interfaces:** management open on a WAN interface, everything from a WAN interface into an inside network, any to any, rules switched off, a gateway that is down; since the last report: rules added, removed, switched or acting differently, port forwarding added or removed.
- **Setup assistant:** a device whose web page on port 443 carries the firewall's login title is suggested with a step-by-step guide of its own (key of a user with only the pages Lanloom reads). Family manifests can name their guide (`guide`).
- Sample network: a *Sample gateway firewall* with five interfaces, an interface group, thirteen rules, a forwarding, a reservation, a route and two VPN instances; during the last demo night a rule from the internet side to the lab arrived and a rule was switched off.
- **Switch ports in the firewall editor** (stage B, through the executor like every change; simulation by default). A *Switch ports* card shows every switch as its faceplate; a port opens its panel and, in Change mode, *Change port ...*: name, port profile, untagged network, tagged networks (none, all networks or a choice) and power over Ethernet on or off, each only when the executor's family can write it, plus *Power cycle* (the device behind the port restarts; a one-off command). Ports with a change carry an accent ring and the panel says *Planned*. Lockout protection, checked in the preview and again by the executor with its own fresh read: only the name may change on the uplink of a switch, the internet port of a gateway, a port with network equipment or an LLDP neighbour behind it and mirror or aggregation ports; a port that carries a protected device, the approver's device, the executor or the controller keeps its networks and power; only local networks are used. Every change of power, every power cycle and the management network on a port need their own confirmation; one change per port and plan. The preview names the devices that change network, keep a fixed address, are used by address, restart or switch off, and the tagged networks that change. The sample network has a sample switch with 16 copper ports, a fibre cage, two access points on power over Ethernet and four port profiles; ports of protected sample devices show the lock. Command palette: *Change a switch port* (`#/firewall/ports`). Nightly report section *Port settings*: a baseline the first night, then profile, network and power changes per port (port and network names only). New capability keys `port.set.name`, `port.set.profile`, `port.set.native_network`, `port.set.tagged_mode`, `port.set.tagged`, `port.set.poe`, `port.cycle`. The read-only *Switch front panels* now show the port profile, the tagged networks and power switched off. Docs: [manual](docs/manual/firewall.md#switch-ports), [docs/FIREWALL-EDITOR.md](docs/FIREWALL-EDITOR.md#switch-ports); screenshots.
- **UniFi family writes switch ports** (product names identify compatible products only; no affiliation or endorsement): the executor's write half changes the port overrides of a switch over the classic API with the same site-administrator account as the other stage-B changes, and runs the power cycle command. It sends the override list exactly as read and changes only the planned entry and fields (name, profile, untagged network, tagged mode and choice, power mode automatic or off); any other difference refuses the write before it is sent. Everything is read back after writing and the way back restores the saved entries. Releases that keep tagged networks as a mode plus an excluded list support a choice; older ones refuse it. Passive and pass-through power modes are left alone. Tested against the adapter tests (console and self-hosted layout) and a switch record written through the real API of a self-hosted Network application 10.0.162. See [docs/integrations/unifi.md](docs/integrations/unifi.md#switch-ports-stage-b).


### Changed
- The neutral port model: `profile` is the id of the port profile and `profile_name` its display name (it was the name); ports carry `tagged_mode` and `tagged` (a list of network ids); power is written as on or off. The name of a port may now change even on an uplink.

### Fixed
- **A link to a page now survives the sign-in.** Opening a device, a map or network card, a nightly report or a bookmark while signed out used to end on the overview after signing in, and a session that ended in the middle of the work lost the view as well. The browser tab now remembers the address it was sent away from (only the part after the `#` of Lanloom's own pages, only a known view, at most 200 characters, ten minutes, usable once; it never reaches the server) and opens it after the sign-in with a password, with a password and a code, with a passkey and after the required set-up of a second factor. A page the role may not open, or one that does not exist any more, leads to the overview with a short note (in every language of the app). Invitation and reset links, the first set-up and the sample network work as before; signing out, ticking *Public or shared computer* and an invitation delete the remembered address. Browser tests start every case cold (no session, real address) at 1512 and 390 px, plus one cold start per main view.
- **Image build no longer cancelled by a later push:** the publish workflow used to cancel a running build as soon as another change reached the main branch, so the image of a release candidate could end as cancelled and the release gate went red without any real defect. A running build now finishes; a newer push waits behind it.

## [1.48.0] – 2026-11-19

1.44.0, 1.44.1, 1.45.0, 1.46.0, 1.46.1 and 1.47.0 below were not released on their own; they are part of this release.

### Added
- **Several sites in one Lanloom.** A site is the level above networks, connections, devices, history and nightly reports: a holiday flat, an office or a relative's home next to your own. Every site keeps its data in a database of its own; an existing installation becomes the site *Home* without moving or losing anything.
- **Reaching another site only over ways that are already there:** one more site of a controller that manages several (*All sites > A site of your controller* lists its sites read-only and copies the connection with the other site name; nothing is written to the controller), or the host agent on a machine there (a pairing code belongs to the site it was made in). A remote site never uses the interfaces of the Lanloom host: no scan, presence check or flow receiver there; a scan of a remote range needs the consent for that range.
- **Site switcher** next to the logo, remembered per browser (cookie `ll_site`, header `X-Lanloom-Site` for scripts), on the phone too; **All sites** (`#/sites`) with one card per site (state, devices online, new today, internet, what stands out, how the site is seen) and an *All sites* card on the overview; palette commands *Switch site: …* and *Open Sites*; *Settings > Sites*.
- **Who sees which site:** administrators release sites per person (*Settings > Users > Sites …*, also in an invitation): all sites (also later ones, the default) or a list. The server picks the site of every request only among the released ones, and names and addresses of other sites never reach an answer, the live feed or a report (tested against every route, the live socket and every static file). API keys follow the sites of their account.
- **Nightly report per site or combined:** each site writes its own report, delivered through the channels set up at Home with the site name in the title; or one combined report from Home with a *Sites* section (other sites only for readers who may see them). Notifications of a site go only to people who may see it; personal rules apply at every site, each maintenance window belongs to its site. Webhook events of a remote site carry a `site` object.
- Backups contain every site; a restore brings back the site list and each site's data. Adding, renaming, removing sites, the report mode and site releases are written to the audit log.
- Demo: a sample branch office (controller site and host agent, one switched-off desk). Docs: manual page *Sites*, screenshots.
- **Failure chain: what fails if a part fails, and what it depends on.** For every device, network part, container, connection, hub device, entity and automation Lanloom follows one dependency graph built from what it already knows: the topology (switch ports, radio, guests on a host), the gateway, the containers of a host, its own connections, and with a home-automation hub its devices (and the radio coordinator they are reached through), entities and automations (what starts, checks, reads and switches them). Over a *sure* link a part fails with the one before it and the chain goes on; over a *likely* link it is only impaired and the chain ends there. The bar above the board gives the numbers by kind, switches between *Fails with it* and *Depends on* and opens a list by stages with the reason of every part ("Cable to Core switch, port 5", "Started by Hallway motion"); the board frames the root, puts a stage mark on every part and keeps only the traces of the chain lit. Manual in [docs/manual/failure-chain.md](docs/manual/failure-chain.md).
- **Simulate maintenance on the map.** The same chain coloured by what would happen - what stops red, what is only impaired yellow - with a frame around the view and an *End simulation* button; only shown, nothing is switched and no state or message changes. A hub device drawn as one tile with its network device counts as impaired when only the hub fails.
- **Outage automatic.** When a device is down that was up before, a note above the board names it and how many parts probably depend on it (names wrap, never cut off), the parts get a calm dashed outline, their cards say *Probably affected: … is down*, and the *Automations* tab marks the automations it reaches. The note can be hidden until the outage changes. Phones and computers never start it.
- **Dependencies on the card.** A section *Dependencies* lists what a part depends on directly (with reason, *required* or *partly*), how many parts depend on it, and dependencies entered by hand: *hard* (fails with it), *soft* (impaired) or *none* (Lanloom's own guess is wrong). An entry decides the pair; entering or removing one needs the operator role and is written to the audit log (`failure.dependency`).
- **Hovering a tile shows only the traces it needs:** its way up through cables, radio, guests and hub rooms to the gateway, plus the dependencies entered by hand.
- **Search and commands:** *Show failure chain …*, *What does … depend on?* and *Simulate maintenance …* ask for the part in a second step; devices, automations and entities get the actions *Show failure chain* and *What does it depend on?*.
- **Nightly report section *Failure chains*:** per device that went offline in the night how many parts and automations depended on it (still down: a point to look at), and dependencies entered by hand that point at something Lanloom no longer knows.
- Sample network: the garden access point went down an hour ago (its controller lists it as disconnected), so the outage automatic shows what hangs on it, down to an automation of the vacuum behind it; two dependencies are entered by hand. Texts in English and German.

### Changed
- The firewall editor, the executor and the AI assistant work on the Home site; at a remote site their parts are hidden and the server answers `409 site.err.primary_only`.
- The read-only controller source may also read the controller's site list (`GET /api/self/sites`); the allow-list test names it.
- *Simulate maintenance* in the device card lists what would stop and what would only be impaired (devices, automations, Lanloom's own connections) from the failure chain, with *Show on the map*.
- The *Device offline* notification adds how many parts probably depend on the device, among them how many automations.
- Performance budget: the failure chain (graph from scratch, the chain of the gateway and the outage automatic) is measured at 250 and 1000 devices.

### Fixed
- A device that a switch controller still lists but reports as disconnected (or whose heartbeat is missing) no longer counts as a sign of life, so a dead access point shows as down instead of online.

## [1.47.0] – 2026-11-19

1.44.0, 1.44.1, 1.45.0, 1.46.0 and 1.46.1 below were not released on their own; they are part of this release.

### Added
- **Export and import** ([docs/EXPORT.md](docs/EXPORT.md), manual page [docs/manual/export.md](docs/manual/export.md), page `#/export`, button *Export* on the inventory page). **Network plan** as a **PDF** (A4 or A3 landscape; both views and the inventory list; one sheet when it stays readable, else cut between rows of zones; real text, standard fonts, no dependency added), as an **SVG** (vector, texts as text, no bitmap or script) and as a **draw.io** file (a box for every zone and device, lines that follow the boxes). Looks *light*, *dark* and *print-friendly* (white, state also in words). **Inventory** as **CSV** (UTF-8 with byte order mark, formula cells neutralised and restored on import) and **JSON** (`lanloom-inventory/1`): the found facts, your own fields, the network, the device and port a device hangs on, state and dates. **Import of own fields** from such a file (name, purpose, room, person, shop, note, serial number, dates, own firmware, "connected via" with port and medium): matched by id, hardware address or address, an empty cell changes nothing, a trial run reports rows read, would change, unchanged, not found and every wrong row, a wrong row is skipped as a whole. **Redact** for every file: names, addresses (one pseudonym network per real network), hardware addresses, ids, persons and rooms become neutral ones, serial numbers, notes, purpose, shop and dates are left out; a redacted file cannot be imported.
- Downloads need the viewer role (everybody who is signed in), the import the operator role; each download and import is an audit entry (`export.download`, `export.import`) with the kind of file and whether it was redacted, never the content. Search commands *Download PDF*, *Download network plan as SVG*, *Download network plan for draw.io*, *Download inventory as CSV*, *Download inventory as JSON*, *Open Export and import*. The sample network can download; its import stays read only. The export adds no outgoing connection. Texts in English and German.
- **Omada family, read only** (stage A; product names identify compatible products only; no affiliation or endorsement; guide [docs/integrations/omada.md](docs/integrations/omada.md), entry in [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)). Lanloom reads a controller through its Open API with an application of its own whose role only views (client credentials; one token exchange, then reads; an exchange is capped per hour and a refused one pauses for 15 minutes). It reads the site's gateway, switches and access points with uplinks, state and client counts, the clients (switch and port or access point, wireless network and band, fixed addresses, blocked and guest), switch ports with link, speed, power over Ethernet, port profile and the device behind the port, LAN networks and VLANs with DHCP, wireless networks (bands, security, VLAN, hidden, guest) and the access rules of gateway, switch and access point (shown, never written). Devices and clients feed the topology and the inventory every minute, everything else every five minutes. The detail of a wireless network (the only reply that carries its passphrase) is never requested, and no hardware address, serial number or secret leaves the translation module. The setup assistant suggests the family after a gentle scan (default web port and page title) with a step-by-step guide.
- **Checks and night report section for the Omada family:** a device is not connected, an internet connection is down, a wireless network has no encryption (a guest network only as a note), access rules are switched off; since the last report: devices gone offline or back, new or removed devices, firmware changes, wireless networks that appeared, vanished or changed, access rules that were added, removed, switched on or off, or act differently. The section appears only when the connection exists. Texts in English and German.
- Sample network: a *Sample wireless controller* with a gateway, a switch, two access points, three networks, three wireless networks and three access rules; one access point went offline and a rule arrived during the last demo night. The sample controller of the firewall editor is unchanged.

## [1.46.1] – 2026-11-19

### Fixed
- Names, notes and sub-lines in lists (overview devices, device list, data-path rows, firewall tiles, inventory, connection peers, sensors, AI suggestions, session ids) wrap instead of being cut off with an ellipsis, so a very long name stays readable on a phone. Fixed-geometry board tiles and chips keep their short form; their card shows the full text.
- The Escape key now closes the open component card, like the close button (it did nothing before).
- Keyboard focus stays where it was when the live rhythm redraws the overview page or the map's area filter (it was dropped to the page before).

### Changed
- Browser guard tests (private test repository) cover three recurrences: no visible text cut off in any view at phone and desktop widths in both colour themes with very long names; open parts, filters, scroll position and focus survive the live rhythm; changing a view by anchor or with the back key leaves no sheet or full screen standing.

## [1.46.0] – 2026-11-19

1.44.0, 1.44.1 and 1.45.0 below were not released on their own; they are part of this release.

### Added
- **Services and web interfaces per device.** The *Services* section of a device card lists what the device offers: name, port, protocol, the version where the device names it itself (a `Server` header, a DNS-SD text record, the UPnP product line, never guessed from a port) and whether it is a web interface. Sources: the scan and its banners, mDNS, UPnP and the ports a host agent reports as listening.
- **Links view** (*Devices › Links*, `#/devices?view=links`, palette *Go to Links*): every web interface with the state of its probe and a button that opens it in a new tab. A probe runs every 3 minutes on **known** services only (one plain `GET /`, no sweeping, never a login). 2xx, 3xx and other non-error answers are alive, **401/403 is alive**, **5xx is red** and still a link; **two misses in a row switch the link off** (greyed out, not clickable) and the next good answer switches it back on by itself, without a message. Services without a web page get one TCP connection, closed at once, as the functional probe. Filters *All*, *Problems*, *Certificates*; the services without a web page sit under *Other services*.
- **Certificate clock.** The expiry date of every TLS service is read (also of a self-signed certificate; at most every six hours): a hint under 30 days, yellow under 14, red under 3, *expired* after. Issuer or *Self-signed* in the tooltip. The certificates also feed the nightly report (section *Certificates*, a point to look at under 14 days) and the notification rule *certificate expiring*.
- **Nightly report section *Web interfaces and services*:** interfaces that do not answer, answer with a server error, or are marked *Removed?*.
- **Removed? instead of a fault.** A service that stayed away for 24 hours of probing while its device answered is shown as *Removed?*: no longer booked as an outage, looked at only once an hour so it comes back by itself, and an operator can *Forget* it (audit entry `service.forget`; a newer scan that lists it brings it back).
- **Availability of services.** Every look is booked into the 5-minute slots as `svc:<device>|<port>`; the *Stability* list shows the services that have a history (a 5xx counts as a failed look, a 401/403 as a good one). A device that does not answer is the device's business: nothing is probed and booked for its services. When none of three or more services answers at all, nothing is booked (Lanloom's own network is the likelier cause).
- Settings › General: **Look at known services and web interfaces every 3 minutes** (administrators, on by default). Devices of the scan protection list keep their rules (printers never on the raw printing ports, industrial hosts only on 80 and 443, passive hosts not at all); only private addresses are probed. Lanloom only observes, it never repairs anything.
- Sample network: every case appears once (answers, redirect, login required, error 503, not answering, *Removed?*, certificates from valid to expired). Texts in English and German; manual in [docs/SERVICES.md](docs/SERVICES.md).
- **Host health on the card of every host** Lanloom reads: a health ring with the worst value and round gauges for processor, memory, disk, temperature, swap, load per core and the processor clock (where throttling is measurable). Green below 70 %, yellow 70–90 %, red above 90 % (temperature 70 / 85 °C, load per core 70 / 100 %). Every source with the age of its last reading; a source that stopped for 15 minutes is dimmed as *stale · x min ago*, never red.
- **Host values from every source:** the host agent (now also processor use, processor temperature, the clock share and the firmware's throttling flag), SNMP host tables (HOST-RESOURCES-MIB once a minute, with load and temperature from the UCD and LM-SENSORS tables where served), the hypervisor (now with swap), the network controller (processor and memory of routers, switches and access points) and the system sensors of a home hub merged with the device. One sample per source every five minutes, 35 days (storage area *Availability*, part of a backup with history).
- **Early warnings:** memory, swap, disk, load and temperature as a trend over 6 hours and 7 days (median support points, a steady trailing run, Theil–Sen slope). A limit reached in less than 72 hours with at least three support points is a warning: a calm yellow row on the host card, the overview card *Early warnings*, the nightly report section *Early warnings* (a point to tick off) and the notification event *Host value heading for its limit*. A daily rhythm or a sawtooth never warns; a short-window trend needs the long window's consent.
- **Waiting on someone:** a row of the overview that only a person can end (an infrastructure device that is off, a connection whose sign-in is refused, new devices nobody named, batteries to replace - new row *A battery needs replacing*) and that has been unchanged for more than 24 hours moves to its own card. It no longer colours the banner, says how long it has been waiting, and any change brings it back at once. The memory survives a restart.
- Routes `GET /api/hosthealth` and `GET /api/hosthealth/{device}` (read only, viewer role).
- Command palette: *Go to Early warnings*.
- Demo: seven days of host samples - a storage box whose big volume will be full in about two days, a backup server with slowly growing swap, a quiet SNMP source shown as stale, a battery that has been waiting for two days.
- Docs: [docs/HOST-HEALTH.md](docs/HOST-HEALTH.md), manual pages *Devices*, *Overview* and *Nightly report*, notifications, host agent, palette, compatibility; screenshots.

## [1.45.0] – 2026-11-19

1.44.0 and 1.44.1 below were not released on their own; they are part of this release.

### Added
- **Firewall board.** For a zone firewall that is read, the Firewall tab opens with three figures (*Blocked today*, *Own rules*, *Checks*), the rules that blocked most, and the zones as pads on a board with one line per pair that may talk: thin for the built-in defaults, thick for rules of your own or a busy pair (with the hits of the last hour), red dashed where traffic was blocked. A zone and a second one, or a line, filter the rule list to the rules between them; *Show as list* is the table form for keyboard and screen reader.
- **May one reach the other?** Two places (the internet, a zone, a network, a device or an address), optionally a protocol and a port, and the rules answer in their order: allowed, blocked or rejected, by which rule, as a sentence. A rule that depends on chosen devices, applications, regions, schedules or port groups makes the answer *Depends* and is listed with the reason. A palette command opens it. Read only, nothing is sent.
- **Rules in plain words** under every rule name, with *N in the last hour* and *N blocked today* where the firewall counts hits. Counters are kept per day and per hour in Lanloom's own store (a counter that starts over after a restart counts from zero).
- **Checks with a suggestion:** every check says what you can do. New checks: a rule that an earlier rule always overrides, a guest zone that reaches the inside with every service, and an intrusion-detection suppression list longer than 30 entries (the size is read, never the entries).
- **Switch front panels** in the equipment card of the Firewall tab and on the card of a switch, access point or router: ports as on the hardware (odd above, even below, SFP cages apart) with link, speed, duplex, power over Ethernet, uplink, VLAN, the devices behind each port, and link drops and error episodes of the last 24 hours (none: fine, one or two: some trouble, three or more: unstable). Access points show radios with channel and clients, and how often devices moved to or from them. Keyboard: one tab stop, arrows, *Home*, *End*, *Esc*.
- **Exact traffic on cables.** Where one device sits alone on a switch port, its cable follows the counters of that port instead of the host's own flows (no double counting). Optional setting *Take the background noise out of exact port counters* (off by default): the smallest sent rate among at least three measured ports of a network is subtracted from each.
- **Unstable switch ports** are a notification event (*Switch port unstable*, with an all-clear) and a section of the nightly report (ports with events in the period, devices that moved between access points).
- **Network view:** a mode switch *Live | Plan | Firewall | Data paths* (also at the top of the Firewall tab and of Data paths); networks with more than 12 devices fold their quiet devices (smart devices, printers, media players, online and fine) into one *N quiet devices* tile that unfolds on a click; the legend holds line styles and networks in one strip (a network in the legend filters like its chip); a full-screen button opens the board over the whole window (compact on a phone; *Esc*, the close button and the back gesture close it).
- **Devices list:** network and wireless-network tags on every row and a filter menu (a network, a wireless network, or wired only; `?net=` in the address).
- Demo: counters that keep counting, three hours of hit history, two ports with link drops and errors, two access points with radios and roaming, a long suppression list.
- Docs: manual pages *Firewall*, *Network*, *Devices*, *Settings*, the nightly report, notifications, palette, compatibility and topology; screenshots.
- **Zone firewall in the firewall editor** (stage B, through the executor like every change; simulation by default). *Zones* card: every zone with its networks and how many networks and rules refer to it; *New zone*, *Rename* (zones that are not built in), *Remove* only for zones Lanloom created and only when no network and no rule is in them any more (the controller would delete the rules of a removed zone without a word, Lanloom refuses instead), and per local network *Move to zone …* into a zone of its own or back into the internal zone - never the management network, never into another system zone. Built-in zones carry a *built-in* chip, zones Lanloom created *created by Lanloom*.
- **Rule tools on the rule list** in Change mode: *on* checkbox (switch a rule off or on), *Change* (name, action allow, block or reject, from zone and optionally one network of it, to zone and optionally one network, protocol, ports, allow answers back), move up or down within the own rules of a zone pair, *Remove* for rules Lanloom created, and *New rule* above the list; planned new rules are shown above the list marked *planned*. Only rules whose every setting Lanloom can carry over are changeable (no schedules, connection-state filters, address, region, app or domain matches, source ports, IPsec match or ICMP type); the others stay read-only, built-in rules of the controller are never changed.
- ***May one reach the other?* in the editor too** (simulation or executor): the same card answers from the editor's rules and adds the answer *after your draft*, so a new rule, a rule switched off, a new order or a network moved into another zone can be checked before the approval. API: `GET /api/firewall/ask?source=editor` (field `after`).
- **Preview for zone changes:** new, changed and removed zones and rules field by field, old → new; warnings which zone pairs open or close, a network that moves zone and a rule switched off; a rule that lets traffic in from the internet or from another zone into the management zone needs its own confirmation tick.
- **Nightly report section *Zone matrix*** (only when a connection reads zones and rules): the first night a baseline, then zone pairs that opened since the last report (a point to check), pairs that closed (a note) and whole zones that take traffic from the internet (a point, danger level). Zone names only.
- Command palette: *Zones of the firewall*, *New firewall rule*; deep links `#/firewall/zones`, `#/firewall/rules/new`, `#/firewall/ask` (*May one reach the other?* opens the editor's card when no firewall is read).
- New plan step capability keys: `network.update.zone`, `zone.create`, `zone.update.name`, `zone.delete`, `policy.create`, `policy.update`, `policy.delete`, `policy.reorder`. An executor of an earlier release does not declare them, so the controls are not offered until the executor is updated by hand.
- **UniFi family** (product names identify compatible products only; no affiliation or endorsement): the executor's write half creates, renames and deletes own zones, moves networks between zones, and creates, changes, switches, reorders and deletes own rules over the classic zone-firewall API, with the same local site-administrator account as the other stage-B changes. Every rule body is the complete checked set of fields, every request goes through a field-exact allow-list, everything is read back after writing, own objects are remembered in `unifi-own.json`, and the way back restores names, network membership, rule settings and the order per zone pair. Tested against a self-hosted Network application 10.0.162 switched to the zone-based firewall, including refused lockout scenarios, a partial write rolled back and a crash after creating a zone.

### Changed
- The read half of the controller family also reads port counters and errors, the radios of access points, the port and wireless network name of each client and (every five minutes, one extra read-only request) the size of the suppression list. The light poll carries the ports every minute.
- The *more radio devices* tile on the map now shows its count (the variable was not passed on to the text).
- The rule list in Change mode gains the rule tools described above; rules of the read-only views are unchanged.
- The sample network has zone rules (exactly its allowed zone pairs plus one rule of its own, *Printing for guests*) and its internal zone is marked as the system zone, so zones, networks in zones, rules and their order can be rehearsed in the simulation and the demo.
- Preview: labelled fields for zones and rules; *Answers back* is shown as *Return traffic*.
- *May one reach the other?*: two places in the same zone are allowed when no rule decides (as on the board), and a rule narrowed to one network of a zone (the shape of a rule Lanloom can change) is judged and worded like a network match.

### Security
- **Lockout protection for zone and rule changes**, checked in the app's preview and again by the executor right before it writes, against its own fresh read: never a blocking rule whose source or target is the management network, the management zone or the gateway; after the plan the device of the person who approves, Lanloom itself and the executor must still reach the gateway (name service) and the controller if they could before - a plan that would cut such a path is refused and the path is named; zones are deleted only when Lanloom created them and they are empty; the management network never moves zone. A refusal stops the plan and nothing is sent.

## [1.44.1] – 2026-11-18

1.44.0 below was not released on its own; it is part of this release.

### Changed
- API: a request with a token to a path under `/api/v1` that does not exist answers `404 api.err.not_found`, guesses with unknown tokens count against the per-client request limit of the routes without a session, and the bearer tokens of the host agents and the executor service are never taken for API tokens. The first group of the API reference is called *General*.

## [1.44.0] – 2026-11-18

### Added
- **API tokens.** *Settings › API* lets every person make tokens for scripts and other programs: a name, a scope (*Read*, *Operate*, *Admin*, or *Deliver only* for a script that only hands in reports and reads nothing), a lifetime of 7, 30, 90 or 365 days and a request limit of 60, 300 or 1200 per minute. The scope can never be more than the person's role - neither when the token is made nor later: a demoted account weakens its tokens at once, a disabled or deleted one stops them. Only a SHA-256 hash is stored; the token is shown once with a copy button and a ready `curl` line, and the list shows name, scope, expiry and when and from which address it was last used. Making a token needs a fresh second confirmation. Administrators see everybody's tokens and can revoke any of them. A token opens only the public API, never the routes of the web page.
- **Public API v1 with an OpenAPI description.** `/api/v1/…` offers status, devices, one device, topology, availability (list and one subject), data paths per window, nightly reports (with every line in the language you ask for), the webhook events and external reports. Every route is declared once in the code; the same declaration registers the route and becomes the OpenAPI 3.1 description (`GET /api/v1/openapi.json`) and the reference in *Settings › API* (method, path, scope, parameters, an example, download of the description). Within v1 fields are only added. Guide with examples: `docs/API.md`.
- **Webhooks.** Administrators add webhooks for new devices, devices gone for a day, devices going offline and coming back, the internet line going down and coming back, incidents opened and closed (once the installation has incidents), a finished nightly report and a backup that is too old. Each call is one `POST` with a JSON body, signed with HMAC-SHA256 over a timestamp and the body (`X-Lanloom-Signature`, `X-Lanloom-Timestamp`, a delivery id that stays the same on retries), so a receiver can refuse forgeries and replays. Failed calls are retried after 30 s, 2 min, 10 min, 30 min, 2 h and 6 h; `410 Gone` stops at once; redirects are never followed. A delivery log shows every attempt (status, error kind, duration, never the answer's body) with *Send again* for failed ones; *Test* sends a `ping`, *New secret* rotates the secret. Address and secret are stored encrypted, the list shows only the host. Nothing is sent before a webhook exists; the connection is listed in the no-telemetry allow-list.
- **External reports.** Another program delivers a short report of its own (`POST /api/v1/ingest`: source, title, level, summary, up to 50 lines; at most 64 KiB and 200 a day). It is listed under *Settings › API* and appears in the next nightly report.
- **Nightly report section "API":** tokens that expire within 14 days or expired overnight, webhook calls that could not be delivered, webhooks that keep failing, and the external reports of the night.
- Every change made with a token is in the audit log with the token's name (never the token); making and revoking tokens and every webhook change are audited as well.
- Demo: two sample webhooks with a delivery log, three external reports and a token of the sample operator; nothing is ever sent from the demo.
- Command palette: *Go to Settings › API*. Texts in English and German.

### Changed
- Settings › Storage counts the webhook log with the audit log and the external reports with the reports. Tokens move with a backup (as hashes), external reports with the history part of a backup; the delivery log stays with the instance.
- Docs: `docs/API.md` (new), manual page *API*, README section *API*, security model, nightly report sections.

## [1.43.0] – 2026-11-18

### Added
- **The legend filters the board.** On the map and the network view every line style in the legend (Uplink, LAN, Flow, Integration, Cable, Wireless, Virtual, and *Not measured*) is a switch: a click hides that kind of line on the whole board, a second click shows it again, *Show all* brings every kind back. A hidden kind is greyed and struck through. It leaves the drawing completely (traces, pads, vias, the exit to the board edge), its traffic dots stop at once, a selected component no longer lights neighbours over it, and its port and end marks go as well. The live redraw keeps the filter, so hidden lines never flicker back. Each board remembers its own choice in the browser (`lanloom.board.hide.<view>`), so the map and the network view keep separate filters across reloads. The switches are real buttons with a pressed state, keyboard focus stays on a switch while it is used, and on phones they are finger-sized and show no sticky hover after a tap. Texts in English and German. The dashed *Talks to* lines of the host agent have a switch of their own. Suggested in issue 6.

### Changed
- Live frames only update the dot size text of the legend instead of rebuilding it, so a focused switch keeps its focus.
- The legend is a labelled group instead of a note, since it now holds controls.
- Docs: manual pages *Map* and *Network* (legend switches, what is stored in the browser).

## [1.42.0] – 2026-11-18

1.41.0 and 1.41.1 below were not released on their own; they are part of this release.

### Added
- **Anycast networks get their own line in the data paths instead of a far-away country.** The offline country database only knows the country an address block is registered in. Anycast networks answer from the nearest of many locations worldwide, so a download from a content delivery server in the next city, the time service of a smart plug or a public DNS resolver was booked under the country of registration (a large content delivery network showed up as "Canada", its resolver as "Australia") and inflated the share outside the EU. Lanloom now ships a small list of well-known anycast networks with the image (the published ranges of the largest content delivery network, also its time and DNS services, and the networks of three public DNS resolvers) and books matching traffic as **Anycast (nearest location)**: its own line in the countries list (click it to see which devices use it), its own line under *Where does it talk to?* on a device card, a note under the key figures, never a country, never a "new country" hint, and never part of the share outside the EU. The bytes still count for the device and for *data exchanged with the internet*. A network listed under *Own networks and tunnels* wins. API: `GET /api/paths` totals `anycast`, `anycast_tx`, `anycast_rx`, `anycast_devices`, matrix bucket `~anycast`; `GET /api/paths/device/<id>` field `anycast`. Reported by a tester (issue #4).
- Demo: some anycast traffic on the laptop, the phone and the inverter.

### Changed
- Traffic already booked under a country before the update is not re-sorted (only country, device and bytes are stored, never addresses); it drops out as the periods roll over.

## [1.41.1] – 2026-11-18

1.41.0 below was not released on its own; it is part of this release.

### Fixed
- **A scan no longer stops with "The scan stopped with an internal error." when a device's hardware address is already known.** A device seen once without its hardware address (the neighbour entry was missing at that moment) is stored by its address; when the hardware address came back and a row with it already existed, the address row was renamed onto it and the database refused the duplicate. Now the address row is merged into the existing row: the row with the hardware address stays, a name or type set on the address row fills only what it lacks, a hidden mark is not carried over, its manual link wins, links that pointed to the address row move to it, and the leftovers of the address row (inventory entry, signs of life, data paths, availability) are dropped.
- **One device can no longer cost the whole scan.** Each scanned host is stored on its own: a host that cannot be stored is written to the log and skipped, the rest of the scan result is kept (before, the whole result was discarded).
- Troubleshooting: an entry for the internal error of a scan.
## [1.41.0] – 2026-11-18

### Added
- **Scan progress in Settings.** *Settings › General › Network scan* shows a progress bar next to the button while a scan runs, and *Scan now* turns into *Cancel* (stops the scan, then it is *Scan now* again). The status line adds the phase's progress and the percentage (`Scanning: Service ports 120/500 · 14 found · 58 %`). A scan that already runs when the page opens - started in the setup assistant or by the automatic rescan - is picked up with its bar and *Cancel*; leaving the page stops the polling. Suggested and tried out by a tester (issue #7).

### Fixed
- The scan progress bar (setup assistant and Settings) never moves backwards within one scan: the mDNS and SSDP listeners run at the same time and their reports interleave, which made the bar jump back. Both pages now share one meter.
- *Last scan* in Settings names the last finished scan; a stopped, failed or still running scan no longer shows up there as a scan with 0 devices (`GET /api/scan` field `last`).

## [1.40.0] – 2026-11-18

### Added
- **Lanloom as an add-on of a home-automation hub.** The folder `addon/lanloom` packages the released, signed image of the same version (amd64 and aarch64) with a small start script for add-on systems with ingress: Lanloom opens in the hub's side panel and signs people in with their hub account. The user header is believed only from the add-on system's ingress proxy (its fixed address in the add-on network), never from the network; the role comes from the option `ingress_role` (default admin, only administrators of the hub see the panel). Option `direct_access` (default off) also answers the network on port 7380 with Lanloom's own accounts; without it everybody except the ingress proxy and the host's own health check gets a short refusal. Host networking is the only extra it asks for: no added capabilities, no devices, no privileged mode. The start script runs as root only to create `/data/lanloom` in the add-on volume and drops every privilege before Lanloom starts. The folder `addon/` is laid out as an add-on repository; installing as a local add-on works today. See [docs/ADDON.md](docs/ADDON.md).
- **Add-on mode** (`LANLOOM_ADDON=1`, set by the start script): the built-in updater and the release check are off (the add-on system updates; *Settings › Updates* says so and still opens *What's new?*, the notification *Update available* is not offered), the data lives in the add-on volume (the hub's backups include it), and the page may be framed by the same origin only in this mode (`frame-ancestors 'self'`); a plain install still refuses every frame. The origin check of changing requests accepts the hub's address that the ingress proxy forwards, from that proxy only.
- **MQTT output, off by default.** *Settings › MQTT output* publishes ten figures as sensors of a hub through MQTT discovery, grouped under one device *Lanloom* and named in the instance language: devices, devices online, devices offline, new devices (24 h), the internet line (connectivity), open issues, the network health, open vulnerability hints, the last nightly report (timestamp) and its open points. Only counts, a level, a flag and a time - never a device name, address, hardware address, person or place. Values as plain text, retained, sent when they change and every ten minutes; availability with a last will. Switching off or changing the node id or a prefix removes the sensors again (empty retained messages on exactly the topics used before).
- **Only its own topics.** The output has its own small MQTT client: it sends only CONNECT, PUBLISH, PINGREQ and DISCONNECT (never SUBSCRIBE - nothing comes back), and every topic must be one of the topics computed for its own node; both are checked when a packet is built and again right before it is written, and tests read the source for any other write path. Lanloom still never writes to a device. In the add-on it can use the broker the add-on system offers (asked on each connect, credentials not stored); otherwise address, port, user name, password (encrypted) and optional TLS. *Test connection* publishes nothing.
- When the output cannot connect for ten minutes: a note on the overview and the notification event **MQTT output not connected** (with an all-clear). Audit entries `mqtt_out.change` and `mqtt_out.test` (never the password). API `GET`/`PUT /api/mqtt-out`, `POST /api/mqtt-out/test` (administrators).
- Command palette: *Open Settings › MQTT output* (deep link `#/settings/mqtt`). Demo: the output switched on towards a sample broker with every sensor and its value (the demo never connects).
- Documentation: [Add-on](docs/ADDON.md), [MQTT output](docs/MQTT-OUTPUT.md), manual page [Add-on and MQTT output](docs/manual/add-on.md), settings, updating, access, install, troubleshooting, compatibility, security model, README; screenshots.

### Changed
- The no-telemetry allow-list names the MQTT output (configured channel) and the add-on system's broker lookup (new category *add-on system*, add-on mode only).

## [1.39.1] – 2026-11-18

This is the first release since 1.19.0; everything in 1.20.0 to 1.39.0 below is part of it: the overview as the start page, one component with two sides, calm notifications, changing clients, networks and VLANs and guests through the executor, inventory and life cycle, availability over time, signed images, the Automations tab, a manual with one page per view, forgetting a device, data paths for devices, update channels with a way back, the tree view and accessibility, the optional host agent and container source, gateway changes in the firewall editor, read-only DNS filters and vulnerability hints from an offline feed.

### Fixed
- Tests: the guest-change test waits for the editor's picture to follow a finished job (it read it a moment too early on a busy test runner; 1.38.0 and 1.39.0 were not released on their own).

## [1.39.0] – 2026-11-18

### Added
- **Vulnerability hints from an offline feed.** A new view *Vulnerabilities* (the *Devices | Inventory | Vulnerabilities* switch, `#/vulnerabilities`) lists published vulnerabilities that match the maker, model and firmware Lanloom knows for a device, most serious first: severity with score, a mark for **exploited in the wild** (and ransomware use), the certainty, why Lanloom thinks so, and what to do - update the firmware to the first fixed version, look for an update, or disconnect or replace the device when no fixed firmware is known. Never phrased as an alarm.
- **Strict matching, better no hint than a wrong one.** The model must match exactly (no partial matches, no model without a digit), the maker must be the catalog's maker (whole words, never part of a word), and the firmware must lie inside an affected range and be comparable (another hardware branch or a pre-release is never compared). *Certain* needs firmware from a person (inventory), a connected controller, the hub or SNMP; firmware read from an announcement, a maker known only from the hardware address, or a catalog entry for every version gives *possible*. Devices that cannot be checked are listed with the reason (maker, model or firmware unknown, firmware not comparable) and a link to enter the firmware in the inventory.
- **The feed** (`lanloom/data/vulnfeed.json.gz`, about 0.3 MB) is built by `scripts/build_vulnfeed.py` from the public catalog of known exploited vulnerabilities and the affected-version ranges of the national vulnerability database: every catalog entry that names device firmware plus the critical device-firmware records since 2020 (3,717 entries, 250 of them exploited in the wild, as of 2026-11-18). It is read offline and renewed with every release; administrators can import a newer feed file built with the same script, and go back to the bundled one.
- Device card: a *Vulnerabilities* block with the open hints of the device, or why it was not checked, or that its firmware is outside every affected range.
- *Does not apply …* (operators) sets a hint aside with a note; it stays under *Set aside* and can be brought back. Audit entries `vulns.dismiss`, `vulns.restore`, `vulns.feed_import`, `vulns.feed_remove` (the length of a note, never its text).
- Nightly report section **Vulnerability hints** (hints first seen that night with what to do, the open total, a note when the feed is older than 180 days) and the notification event **Vulnerability hint** (once per device and hint; certain or exploited: warning, otherwise note).
- Command palette: *Open Vulnerabilities*, *Show hints exploited in the wild*, *Show devices not checked for vulnerabilities*.
- Demo: a sample feed with invented entries and every case (certain and exploited with a fixed firmware, certain from the hub's firmware, possible for every version, a hint set aside, a listed model with unknown firmware, devices not checked).
- Documentation: [Vulnerability hints](docs/VULNERABILITIES.md) (feed, building and importing a feed file, matching rules, API and roles, limits), manual page [Vulnerabilities](docs/manual/vulnerabilities.md), nightly report, notifications, command palette, inventory, README; screenshots; the sources and licences in THIRD-PARTY-NOTICES.

### Changed
- The sample gateway of the demo reports a model number (*Home gateway HG-1*).
- The no-telemetry allow-list names the public vulnerability record as a link (opened only when clicked).

## [1.38.0] – 2026-11-18

### Added
- **DNS filters, read only** (product names identify compatible products only; no affiliation or endorsement): read adapters for Pi-hole (version 6 and later) and AdGuard Home. They read **totals only** - queries and blocked queries per hour, the most blocked names, the busiest clients, the upstream resolvers with their answer times, the block lists with entries and last update, whether blocking is on - and never single queries: no path of a query log is on either allow-list (a static test checks it), the only request that is not a plain `GET` is the sign-in with the password, and after a refused sign-in the half waits 15 minutes. One reading every two minutes (block lists and version every 15 minutes). Guides: [Pi-hole](docs/integrations/pihole.md), [AdGuard Home](docs/integrations/adguard.md); overview: [DNS-FILTER.md](docs/DNS-FILTER.md).
- **The filter on the map.** The device the filter runs on gets a *DNS* mark; a filter on an address no scan has found is a component of its own with the blocked share under its name and the live grade of the connection. Its card has the section **DNS filter**: the blocked share of 24 hours as the big figure, 24 hours as bars (queries, the blocked part inside), plain findings, the facts, the most blocked names, the busiest clients (each linked to its device card), the resolvers, the block lists and the last days; a table below the bars for screen readers. A device that is among the busiest clients gets the section **DNS** (queries, share of all queries, blocked share where the filter tells) - never which names it asked for.
- **What is kept.** Per filter and hour two numbers (queries, blocked) for 35 days and per day the same sums for 400 days, in two small tables that count with the *Availability* area of the storage page; the top lists and per-client counts live in memory only. Deleting the connection deletes its sums.
- **Findings and the night report.** The filter does not answer, blocking is switched off, the name server is not running, the upstream resolvers do not answer, no query at all for three hours although there usually are some, an unusual blocked share (24 hours against the median of the days before, needs five days), block lists older than three weeks. The night report has the section *DNS filter* while a filter is connected (one calm line otherwise); the card shows the same findings.
- **Setup assistant and catalog.** The scan recognises a filter's web page (page title) and suggests the connection with the address filled in (`http://`, no TLS assumed), a four-step guide for the separate read access and the form; the catalog class *DNS filter* is now available instead of planned. A connection test shows version, queries, blocked queries and clients.
- **For dashboards.** `GET /api/dns` (every filter in short: totals, blocked share, health, findings, the 24 hours, the map component) and `GET /api/dns/<connection>` (one filter in full), for every signed-in role; command palette *Show the DNS filter*; the app state tells whether a filter exists. Everything in German and English.
- Demo: a sample filter with 35 days of sums, ten clients matched to sample devices, resolvers, block lists and one finding. Documentation: [DNS-FILTER.md](docs/DNS-FILTER.md), the two guides, compatibility matrix, troubleshooting, night report, palette, performance, security model, manual pages; screenshots.

### Changed
- The family manifest may name its catalog class and the address scheme the setup assistant pre-fills; the capability list knows the read area *dns*.

## [1.37.1] – 2026-11-18

### Fixed
- Tests: the host agent's check for leaked environment values accepts the host name the report names on purpose (inside a container the environment carries it as well), so the test passes on container test runners too.
- 1.37.0 was not released on its own; 1.37.1 contains all of it.

## [1.37.0] – 2026-11-18

### Added
- **Gateway changes in the firewall editor** (stage B, through the executor like every change): three cards in Change mode when the executor can change the gateway. **Networks** - every network with address range, VLAN, address service, zone and what uses it; *New network* from a template (IoT, Cameras, Work, LAN) with a free private /24, a VLAN no other network uses and an address service filled in; rename a network or change its address service; delete only networks Lanloom created that nothing uses any more. **Fixed addresses** - reserve an address for a known device inside its own network, release one. **Port forwarding** - add, change, switch on or off, delete forwards Lanloom created; every new forward and every change other than its name needs its own tick in the preview, switching one off never does. New plan steps `network.create|update|delete`, `dhcp.reserve|release`, `forward.create|update|delete` with before/after, inverse steps for undo and the same lockout rules in the preview and again in the executor right before it writes (private ranges /16 to /29 without overlap, free VLAN 2 to 4094, range inside the network without the gateway, no range or VLAN change of a network in use, no address service off while devices take dynamic addresses, management network, internet uplinks, VPN and guest-portal networks untouched, forwards only to private addresses of a local network and never to a gateway, the controller or network equipment, one forward per outside port).
- **UniFi family** (product names identify compatible products only; no affiliation or endorsement): the executor's write half creates internal VLAN networks, renames them and changes their address service, deletes networks it created itself (remembered in `unifi-own.json`) when nothing refers to them, reserves and releases fixed addresses and creates, changes, switches and deletes port forwards - field-exact allow-list per request (complete bodies for new objects), read back after every write, rollback of exactly the changed fields. Tested against a self-hosted Network application 10.0.162 (30 of 30 checks, including partial writes and a crash after creating a network).
- **Network plan** (segmentation plan): per device the network it should end up in - an existing one or the name of one still to come (*IoT*); progress *X of Y moved*, one row per target with its devices, a mark *→ IoT* on every device tile that still has to move, *Create this network* (opens *New network* with the name filled in) and *Draft N moves* for what is possible now (one plan's worth at a time). Lanloom's own note, stored by the devices' opaque ids, works with a read-only connection too. Route `PUT /api/firewall/segplan` (operators, in the audit log), deep links `#/firewall/plan` and `#/firewall/plan/edit`.
- **What uses a device by its address**, from what Lanloom already knows: hub integrations whose device page points to the address, presence trackers that follow it, automations and scripts with the address written in their configuration (the hub bridge now keeps literal private addresses found in a configuration, never the text around them) and Lanloom's own sources. Shown on the device sheet (*Uses it by its address*) and as a warning of its own in the preview when a device moves or loses its fixed address.
- Nightly report: section **Network plan** with the progress, one line per target and the devices moved since the last report (only when a plan exists).
- Command palette: *Networks and VLANs*, *Port forwarding*, *Fixed addresses*, *Network plan*; deep links `#/firewall/networks`, `#/firewall/forwards`, `#/firewall/reservations`.
- Demo and simulation: the sample gateway has an address service per network, two port forwards and fixed addresses; the demo has a network plan (four smart devices for an IoT network that does not exist yet, two devices for Media) and sample dependencies of every kind.

### Changed
- Preview: values of a new object are listed once (no struck-out empty value), a removed or released one is shown struck out once; the address service reads as a range.
- Docs: firewall editor (gateway changes, network plan), manual page *Firewall*, the family guide (what the write half changes and how it was tested), compatibility matrix, nightly report, command palette, README, screenshots.

## [1.36.0] – 2026-11-18

### Added
- **Host agent (optional).** A small agent for the other hosts of the network, started from the same signed image (`python -m lanloom.agent run`, standard library only). It reports every 10 seconds, only to the Lanloom it was paired with: bytes per TCP conversation (kernel socket diagnostics), listening services with the short process name, load, memory and disks, failed services and the results of timers (a read-only client of the system message bus that can only list and read properties), the containers of the host's container runtime and, when its status socket is there, the peers of an overlay VPN. Metadata only: never contents, environment variables, command lines, labels (except the compose project and service), mounts or health check output - a positive list on the agent and a second filter in Lanloom. It opens no port and takes no commands.
- **Pairing with a one-time code.** Settings › Agents › *Pair a host agent* shows a code (30 minutes, once) with a ready compose file and a `docker run` line for the host. The agent exchanges the code for a token of its own; Lanloom keeps only the token's hash, and *Remove* revokes it at once (the agent then stops sending and waits quietly). Reports wait in a buffer of about two hours when Lanloom cannot be reached (kept across restarts) and go out oldest first. The agent never loads code: when its version differs, Lanloom names the exact image to pull.
- **Container source without an agent.** Settings › Agents › *Add container source*: a container runtime API behind a socket proxy (`unix://`, `http://`, `tcp://`), read every 30 seconds with GET requests only; *Test* shows runtime, version and the number of containers.
- **Running view.** A new tab per host: system values with a 24-hour trend, containers (state, health, image, uptime, ports, live rate), listening services, timers with last and next run and their result, failed units, overlay VPN peers; *Problems only* and a search. Containers that lived less than 10 minutes (one-off jobs) are only counted, a known, restarting or unhealthy one always shows.
- **Containers on the map.** In the Network view the containers of a host are sub-parts in a zone right after it, folded until opened (chip *Containers*, the host's card, palette *Show containers on the map* / *Hide containers on the map*; remembered per host). A container stopped on purpose is grey, a failed, dead or restarting one red. Cards for a container and a *Host agent* section on the card of a host.
- **Who talks to whom across hosts.** Conversations an agent measures between two known devices become pairs of the data paths (the map's dashed lines and *Where does it talk to?* on the cards); when both ends run an agent, only one side counts them. Bytes to the internet go into the data paths of that host.
- Nightly report section *Hosts and containers* (silent agents, failed services and timers, unhealthy containers, full disks, agents waiting for an update) and notification events *Host agent silent*, *Service or timer failed*, *Container unhealthy*. 5-minute samples of load, memory, disk and failures per host are kept for 35 days (host health for the forecast feature). Demo with two agents and a container source.
- Documentation: [Host agent](docs/HOST-AGENT.md) (reference and installation), manual page [Running](docs/manual/running.md), compatibility, troubleshooting, security model, `deploy/agent-compose.yaml`; screenshots.

### Security
- New routes in the rights table: reading for every role, renaming for operators, codes, container sources and removal for administrators (with a fresh confirmation for codes and removal), the report route only with an agent token. The agent's outbound connection is a category of its own in the egress allow-list (`agent-link`); the runtime and bus clients are checked to send nothing but GET requests and listing/reading calls.

### Tests
- Secret filter with known secrets in environment, command line, labels, mounts and health output (on the agent, in the database and in every answer), a stand-in container runtime that records every request (GET only), the bus client's method list and wire format, pairing, one-time codes, token hashes, revocation, buffer and restart, noise filter, conversations counted once, samples, events, nightly section, demo; a browser check of the Running view, the pairing dialog, the folded containers and the cards at desktop and phone width.

## [1.35.0] – 2026-11-18

### Added
- **Tree view of the network.** The Network view has a switch **Board | Tree**: Internet, gateway, switches and access points, devices and guests as a tree that folds, each line with the link (cable and port on both ends, wireless band and network name, virtual), the live traffic as a number and the state as a word. Open and closed branches are remembered, also while the live data redraws the tree; devices without a known link are listed apart; the area chips and *Problems only* keep the way up to the Internet. On a phone the lines wrap. `#/network?v=tree` opens it directly. The tree is also the text alternative of the board for screen readers.
- **Keyboard everywhere.** Every clickable surface can be reached and used with `Tab`, `Enter` and `Space`; the focus is always visible and stays on its element when a view is drawn again; `Esc` closes a card and gives the focus back to the tile or line it was opened from; dialogs keep the focus inside and give it back. Key `?` (and the command *Keyboard help*) lists every key. A *Skip to content* link is the first stop. In the tree: arrows move and fold, `Home` and `End` jump, `*` opens a level, a letter jumps to the next name. A focused tile or line lights the chain of its partners as the pointer does.
- **Screen readers.** The tree uses the tree pattern (levels, position, expanded). Names of tiles, lines, buttons and tabs start with their visible text. The page is no longer one big live region; one polite status region speaks the important changes (a device stopped talking or is back, overall grade, live connection, change of the view), throttled: one announcement every few seconds, three items at most, a device again after a minute at the earliest, nothing for the first data. The page title follows the view.
- **Table alternatives.** The traffic plot and the availability bars of a card have a closed *Show as table* part with their numbers.
- Documentation: [Accessibility](docs/ACCESSIBILITY.md), the tree in the manual page of the Network view, the key list in the manual, screenshots of the tree (wide and phone) and of the keyboard help.

### Changed
- Calmer states (a configured connection, a past maintenance window, the switches of a quiet rule) are drawn in a quieter colour instead of being made see-through, so their text keeps the contrast of WCAG 2.2 AA in every theme.
- The marks at the edge of the board (traces that lead out of view or to the outside) are named by the words they show.

### Tests
- An automated accessibility check (axe-core) over every view in the light and the dark theme and both colour themes, at desktop and phone width, allows no serious or critical finding; plus a keyboard tour of the tree, a name check for every control, a check that nothing clickable is out of reach of the keyboard and a test of the throttled status.

## [1.34.0] – 2026-11-18

### Added
- **Update channels.** Settings › Updates has a channel: *Stable* (the default) offers the newest stable release; *Preview* also offers a pre-release from the release feed when it is newer and went through the same release path (same gate, same signed image). Only plain version tags are offered, never `:edge` or another moving tag. Without such a pre-release the page says that no preview is available.
- **Hold this version.** Administrators can keep the running version: no update notice and no update until the hold is released; the nightly report says when a newer version waits. Palette commands hold and release.
- **Back to the previous version.** A button *Back to X.Y.Z* returns to the version that ran before the last update, with a fresh confirmation and the same checks as an update (signature, self-test, trial start, backup, health). The version it goes back to is held afterwards. Versions published before images were signed (before 1.29.0) are not offered; the manual describes the way back by hand.
- **Signature check before the swap.** The updater sidecar reads the signature of an official update candidate from the image registry, checks it against the publishing workflow and the public transparency log, and pulls exactly the checked digest. A missing or wrong signature means no swap and a clear message; nothing changes. `python -m updater --verify IMAGE` checks a published image by hand. An older sidecar updates without the check until it is renewed; the page shows the command.
- **What's new?** The version number in the header (on phones at the end of the page) opens the changes of each version, read from the changelog inside the image; nothing is fetched. A dot marks versions you have not looked at yet. After an update the open page loads the new version by itself.
- Notifications and the nightly report know a failed update or rollback; the nightly report names a version that was gone back to.
- Manual page [Updating](docs/manual/updating.md).

### Changed
- The heartbeat of the updater sidecar names its features and the version it keeps as the previous one. Older apps ignore the new fields, and the new app works with older sidecars.

## [1.33.0] – 2026-11-18

### Added
- **Where does it talk to?** on every device card: the countries the device talks to on the internet (with EU or outside, own networks and tunnels and traffic without a country as their own lines) and the devices of your network it talks to, with the bytes in each direction, for 24 hours, 7 days or 30 days. The card of the **Internet** tile lists the devices that talk to the internet, with their sums and the number of countries. Routes `GET /api/paths/device/{id}` (`internet` for the tile) and `GET /api/paths/lan`, both for every role.
- **Who talks to whom** on the map and the network board (first chip, `#/map?talk=1`, also in the command palette): dashed lines in the air between devices that talk to each other in the last 24 hours, width by bytes, the lines of the open card light up. Conversations come from the flows of your router (NetFlow / IPFIX) whose two ends are both known devices; they are not data paths and are not counted in the internet figures or the EU share. The board engine got `setTalk(pairs)`.
- Demo network: nine conversations with a month of history, so the lines, the card section and the Internet tile show real figures.

### Changed
- The NetFlow receiver books flows between two local devices as conversations (two device ids and the bytes per direction, hourly and daily, with the retention of the data paths) instead of dropping them; the receiver status counts them. They are part of the storage area *Data paths*, the clean-up, forgetting a device and the backup of the history.
- Docs: what the controller adapters' statistics can and cannot do for the data paths (byte totals per client, no destination or country; the gateway's NetFlow / IPFIX is the source), the manual pages *Data paths* and *Map*, README, command palette list.

### Notes
- Devices that talk inside one switch never reach the router, so no flow exists for them and they do not show as a line.

## [1.32.2] – 2026-11-17

### Fixed
- Device list: the card is placed where the list is scrolled to only when it opens; it no longer moves along with every scroll, which could pull a button away from under the pointer.
- 1.32.0 and 1.32.1 were not released on their own; 1.32.2 contains all of it.

## [1.32.1] – 2026-11-17

### Changed
- Texts about repeaters say "wireless repeater".
- 1.32.0 was not released on its own; 1.32.1 contains all of it.

## [1.32.0] – 2026-11-17

### Added
- **Forget device** on the card of a hidden or offline device (operators): removes the entry, its history and what you entered for it, after a confirmation in the app's own dialog. This host and the gateway cannot be forgotten; a device that still answers is found again by the next scan, as a new entry. The function existed on the server, now the card offers it.
- **Devices behind a wireless repeater that rewrites addresses**: when one of two entries has a private hardware address and both end in the same three bytes, the card offers *Same device* if a second fact fits: the same IP address, or the other address inside the device's host or mDNS name. At most 50 %, so it is only ever a suggestion, never an automatic merge; *Different devices* keeps them apart.
- Maker behind such a repeater: if a device with a private address carries its real, registered address in its own host or mDNS name (same last three bytes), the maker of that address is shown, marked *address in its name, register*.
- Classification: the DNS-SD service type announced by a common family of smart plugs and relays counts as a smart-home device.

### Fixed
- The card of a **hidden device** opens again. It said "This element is no longer on the map", so a hidden device could not be shown again from its card. Hidden devices and second interfaces now open their card without connections.
- A hidden entry never becomes the main entry of a joined group, so joining an old hidden entry with the live device no longer hides both from the map.
- Device list: a card opened from a row far down the list opens in view (it opened at the top of the list, partly outside the window, on the desktop and on the phone).
- A card that is closed or replaced by a new view stops asking the server for its data.

### Changed
- The buttons at the foot of a card have a gap and wrap on narrow screens.
- Docs: FAQ *A device appears twice behind a wireless repeater*, DISCOVERY.md (repeater evidence, maker from the name), manual page *Devices* (hidden devices, Forget device), TROUBLESHOOTING.md, README.
- CHANGELOG: the 1.31.0 section stands in its place above 1.30.1.

## [1.31.0] – 2026-11-17

### Added
- **Manual with one page per view** under `docs/manual/` (overview, map, network, data paths, devices, inventory, firewall, nightly reports, automations, settings, AI), every page built the same way: purpose, what you see, how to use it, limits, privacy, see also. `docs/manual/README.md` is the index; `views.md` keeps the rails, keys, live state and deep links. The *About this view* rail now links to the page of the open view instead of one long page. The Automations tab now has its *About this view* text (English and German) as well.
- **Troubleshooting** (`docs/TROUBLESHOOTING.md`): how to read the state of a source, a table of every reason with its usual cause and fix, one section for each source (this host, SNMP, MQTT, switch controller, home-automation hub), a shared table for the device-family adapters (each guide keeps its own product table), AI services, data paths and NetFlow, scanning and devices, sign-in, install, update and backup.
- **Diagnostic export** (*Settings > General*, administrators): one JSON file with the version, the kind of installation, counts, the state and reason key of every connection and the last scans, for attaching to an issue. It holds no addresses, names, credentials or notes (connections are numbered, not named); a second pass replaces anything that still looks like a hardware address, IP address, mail address or web address, and the number of replacements is in the file. Each download is written to the audit log. Route `POST /api/diagnostics` (admin).
- **Compatibility matrix** in `docs/COMPATIBILITY.md`: family by stage (read, write) by checked firmware, with how each version was checked and the "no affiliation or endorsement" line.
- **Issue forms** (`.github/ISSUE_TEMPLATE/`): bug, wish, adapter request (which data, which firmware) and test report; each starts with the notice never to post secrets; blank issues are off and no foreign channel is linked.
- FAQ: answers for stale or failing connections, reporting a problem, an empty scan, missing devices, supported firmware, read-only accounts, reverse proxies and removing devices.
- Tests: a link checker over every Markdown file (relative links, images, anchors), checks that every view has its manual page and that the rail points to it, that every adapter family has its matrix row and troubleshooting section, and that the issue forms are complete; tests of the diagnostic export against every value of the demo inventory.

## [1.30.1] – 2026-11-17

### Changed
- Day plan: the German texts speak of the rise and set of the sun in other words; the plan's data calls the time ranges of conditions `spans`.
- 1.30.0 was not released on its own; 1.30.1 contains all of it.

## [1.30.0] – 2026-11-17

### Added
- **Automations tab** (with a home-automation hub connected, also on the phone): every automation and script of the hub, read only. Guide: [docs/AUTOMATIONS.md](docs/AUTOMATIONS.md).
  - **List** with five tiles (automations on/off, with a finding, conflicts in 24 hours, hub health, scripts), search over names, rooms and every entity an automation uses, filters for room, state and finding.
  - **In plain words**: each automation as one sentence plus *When* (one of them is enough), *Only if* (all must hold) and *Then*, with nested choices, waits and repeats - written by fixed rules from the configuration, without AI, in every language of the app.
  - **Flow** in three columns (When → Only if → Then) with the action called and the live state of every entity, **findings**, runs per hour over 24 hours and, in admin mode, the last runs from the hub.
  - **Findings** from Lanloom's own rules: a missing entity (renamed or removed), an unavailable entity, a runaway (60 runs within one hour), a conflict (two automations setting one entity to opposite states within 60 seconds), a script loop, an automation that switches what starts it, a `single` automation that drops starts while it waits, one that has not run for 30 days, a paused lookup.
  - **Dependency board** of any entity: sources on the left (automations and scripts with the verb they use, by hand, by itself), consumers on the right; for an automation what starts and checks it and what it switches. A dot travels along the traces whenever an automation runs (live frame `autoruns`; a flash with reduced motion).
  - **Who switches what**: per entity the causes of the last 24 hours (an automation or script, a person - never who - or the device itself) and the automations configured to switch it; conflicts on top.
  - **Timeline** of the last 24 hours (filters for automations, people, devices), **day plan** from 00:00 to 24:00 with a moving now line (time triggers, time helpers, sun triggers with their offset placed with the hub's next sunrise and sunset, time spans of time and sun conditions, night shaded, runs as ticks; an agenda on the phone), **rooms** with the devices and their live state, and **hub health**: integrations that cannot start or keep retrying, unavailable entities per domain and per integration, silent radio devices - bundled, never healed.
- Nightly report section **Automations** (one per hub connection, only when there is something to judge): missing entities, conflicts of the night, integrations with errors and unavailable entities per domain as points; self-starts, dropped starts and long silence as notes. The AI features see it only as its summary.
- Command palette: *Go to Automations*, typed *Who switches what*, *Timeline*, *Day plan*, *Rooms*, *Hub health*, *Show automations with findings*; *Open in Automations* on an automation hit, *Show dependencies* on automation and entity hits.
- Routes `GET /api/hub/{id}/auto`, `…/auto/item/{entity_id}`, `…/auto/board/{entity_id}`, `…/auto/who`, `…/auto/timeline`, `…/auto/plan`, `…/auto/rooms`, `…/auto/health` (all read only, viewer role).
- Demo network: 17 automations and 3 scripts on the sample hub that show every part (a conflict on one plug, a runaway that starts itself, missing and unavailable entities, a script loop, choices, waits, time, helper, sun and rhythm triggers), two integrations with errors and a day of switching with causes.

### Changed
- The bridge keeps the **structure of each flow** besides the entity ids per role: per step its kind, entity ids (never private ones), state words from a fixed list (anything else becomes "some value"), times of day, thresholds, durations, weekdays, sun events with offset and the name of the action called. Aliases, descriptions, trigger ids, templates, service data, message targets, event data and variables are still dropped before anything is stored; a trigger on a person becomes "someone arrives or leaves" without an id.
- The bridge counts the integration entries per domain and state (titles are not kept) and keeps the sun entity's next rise and set; the sun times never leave Lanloom (export, diagnostics and AI drop them).
- Header with a hub connected (ten tabs): labels only up to 2000 px, icons only up to 1640 px; the phone's bottom bar shows icons only instead of labels cut to a few letters.
- More hub states read in plain words (paused, standby, heating, cooling, above/below the horizon, opening, closing, jammed, detected, clear).
- Performance budget: `automations_ms` (list, who switches what, timeline and day plan with as many automations as devices and 20,000 state changes).

## [1.29.1] – 2026-11-17

### Fixed
- Asking for the availability of something that is not tracked (a hidden device, a map tile that is not a device) is no longer a 404 error in the browser console: the route answers `{"known": false}` and the card or hover window simply leaves the part out.
- 1.28.0 (availability over time, Stability list, traffic plots, card sections) was not released on its own; 1.29.1 contains all of it together with 1.29.0.

## [1.29.0] – 2026-11-17

### Security
- **Signed images without a key**: every published image is signed keyless with Sigstore in the project's CI (OIDC identity of the publishing workflow on the main branch, recorded in the public transparency log; no signing key exists anywhere). `:latest` and `:1` carry the same signature because they point at the same digest. How to verify: [SECURITY.md](SECURITY.md#verifying-an-image).
- **SBOM per image**: one SPDX document per platform, attached to the image as a signed attestation and to the release page as files with a checksum file.
- **Dependency scan**: the pinned Python packages (OSV database) and the image's operating-system and Python packages (grype) are scanned in CI before an image is built for publishing, again on the published image, and in the release gate. A critical finding stops the release unless an exception with a reason and a review date covers it (`security/vulnerability-policy.json`); a high finding is listed with its reason. A self-check proves the scanner still reports a known critical package; a scan that cannot run is never green.
- **No telemetry, checked**: a static check (`scripts/egress_check.py`) lists every place in the shipped code that can open a connection and every fixed public host against an allow-list with purposes (`security/egress.json`); anything new without an entry fails the tests, CI and the release gate. The web framework's built-in telemetry hooks are switched off, and no telemetry library is installed (tested).
- **Secret scanner** over every tracked file and the whole git history (CI); reviewed fakes in the history are kept as hashes only.
- **Container principles as tests**: unprivileged numeric user, read-only root file system, all capabilities dropped, `no-new-privileges`, nothing privileged - checked against the Dockerfile, both compose files, documentation snippets and the containers the updater creates. The updater's self-test and trial containers now drop all capabilities and set `no-new-privileges` too.
- **Headers on every answer**, refusals of the guard (401/403/429) included: wider Permissions-Policy (camera, microphone, location, USB, serial, HID, MIDI, payment, screen capture, motion sensors off), Cross-Origin-Opener-Policy and Cross-Origin-Resource-Policy `same-origin`, `object-src 'none'` in the Content Security Policy, and HSTS when the request arrived over HTTPS (directly or from a trusted proxy; never over plain HTTP).
- **Flood guard** for state-changing requests: 120 per minute per client address on the routes without a session, 600 per minute per account on the others, on top of the failure lock-outs. Open passkey sign-in ceremonies are capped, so the sign-in options route cannot grow memory without bound.
- **Updated dependencies with fixed advisories**: the web framework (FastAPI 0.163, Starlette 1.10; fixes among others a slow Range header parse for file responses), cryptography 50.0.2 and pyOpenSSL 26.4.0.
- **Base image**: the current stable release of the base distribution instead of the previous one, with the distribution's security updates applied in every build (no critical findings left in the image).
- The CI actions of the build and test workflows are pinned to commit hashes; the scanner, SBOM and signing tools are pinned by version and SHA-256 (`security/tools.json`).
- Threat model: [docs/SECURITY-MODEL.md](docs/SECURITY-MODEL.md) (attacker in the local network, stolen session, malicious counterpart, supply chain, with what is left over), including the review of all writing endpoints.

## [1.28.0] – 2026-11-17

### Added
- **Availability over time**: once a minute Lanloom's watcher books whether each device and each connection to a hub, broker, controller or adapter answered into **5-minute slots**, kept for 35 days (Settings › Storage › *Availability and events*: 35 to 365 days). A slot without a look is *unknown*, never "down": nothing is booked while Lanloom is not running or still settling in after a start, and not when none of three or more devices answers at once (Lanloom's own network is the likelier cause). A *gap* is a run of slots with a failed look (two runs one good slot apart are one flapping gap); the *typical repair time* is the mean length of the gaps that ended; three or more gaps in 7 days make a device *wobbly*.
- **Stability list** (*Devices › List | Stability*, `#/devices?view=stability`, command *Open Stability*): five key figures, the wobbly candidates, and one row per device and connection - the worst first - with 35 daily bars, availability over 24 hours and 7 days, gaps and repair time; the search box filters, a click opens the card; a phone layout.
- **Traffic plot per node**: the card has *Live* plus **1 h, 6 h, 24 h and 7 days** (bytes per minute per connection of the map, kept 8 days), a full-screen view (`Esc` leaves) and exact values for the minute under the pointer; an empty range says so instead of drawing a flat line.
- **Card of a device, extended**: an *Availability* section (24 hours as 288 blocks, shares over 24 hours / 7 / 35 days, gaps of the last week, longest gap, 35 daily bars, the last gaps), *Connections* by name with the kind and the live rate in both directions (a device at the other end links to its card), and **Simulate maintenance** - what would lose its way if the device were switched off, read from the known topology; nothing is switched and nothing is sent. Every section folds, and the choice survives the live updates and other cards.
- **Hover window on the map**: resting on a tile shows the traffic of the last hour and the availability of the last 24 hours (mouse and pen only).
- Command palette: *Open Stability*, and the device actions *Show availability*, *Show traffic history* and *Simulate maintenance* (each opens the card at that section).
- Nightly report section **Availability**: devices and connections without a gap over 7 days, the gaps in all, the typical repair time and every wobbly device as a point to look at.
- Routes `GET /api/stability`, `/api/availability/{id}`, `/api/traffic/{id}` and `/api/maintenance/{id}` (all read-only, viewer role). Demo network: 35 days of availability with a wobbly tablet, a camera with a long night, a connection that dropped and a time Lanloom itself was not running, and 8 days of traffic with a day rhythm. Guide: [docs/AVAILABILITY.md](docs/AVAILABILITY.md).

### Changed
- The card's traffic section is titled *Traffic* (it holds the live chart and the history); the component card's "Connections" list shows names and rates.
- The availability and the minute traffic count in Settings › Storage under *Availability and events*, and are removed with a device when it is forgotten.

## [1.27.0] – 2026-11-17

### Added
- **Change guests of a hypervisor** (stage B, executor only): start a guest, shut it down cleanly, move a network card to another bridge or VLAN, switch its firewall flag, switch the guest's own firewall on or off, and change, switch, add or remove its firewall rules. The Firewall tab gets a **Guests** card in *Change* mode with a sheet for the network card (bridge of the guest's node or a virtual network, VLAN tag, firewall flag) and one for rules; everything goes into the draft, the preview shows every field old → new, and start, shutdown and the firewall switch need their own tick. Guide: [docs/integrations/proxmox.md](docs/integrations/proxmox.md#changing-guests-stage-b). Product names are used only to identify compatible products; no affiliation or endorsement.
- **Guests that keep the network running stay as they are**: Lanloom does not stop a guest, move its network card, switch its firewall on or change its rules when the guest runs Lanloom or the executor, is the network's name server or gateway, a hub, controller or broker Lanloom reads, carries the tag `lanloom-protect` or is listed in the executor's options (`protect_guests`, `protect_addresses`). The app checks this in the preview with what it knows (its own address, gateway, name servers, connections, the hardware addresses of the inventory), and the executor checks it again right before writing with what it sees on its own (its addresses, the app's address, its name servers and gateway, its neighbour table). Templates, guests locked or protected on the host and guests of the cluster's high-availability manager are never changed; the card shows a lock and the reason.
- Write half of the hypervisor family: one send path with a field-exact allow-list (only network card values plus the config digest, start, shutdown with `forceStop=0`, the guest firewall switch, guest rules - a new rule must carry the Lanloom mark), the host's digest against changes made meanwhile, host tasks followed to their end (a shutdown that does not finish in time leaves the guest running and rolls the plan back), a card change the running guest could only take after a restart is reverted at once, a fresh read before every write, and the way back puts back only what a step reached. The superuser's token is refused; a token that could change much more gets a warning. Rights template (`LanloomOperator` on a pool) in the guide.
- Night report of the hypervisor: a guest's firewall switched off (a look) or on, network cards moved to another bridge or VLAN (before → now), guest rules changed (count before → now) - whoever made the change.
- Command palette: *Change a guest (start, shut down, network card, firewall) …* opens the Guests card.
- Simulation: a sample hypervisor (one node, four guests and a template, bridges, a virtual network, guest firewall) next to the sample network, with a name server and a broker that are protected, so guest changes can be rehearsed end to end.

### Changed
- Plan steps: the network card step also carries the firewall flag; new steps for the guest firewall switch and for adding and removing a guest rule. The executor pipeline asks a family for rules over the whole plan (`plan_lockout`).
- A guest whose firewall options are all at their defaults counts as *firewall off* (it showed as unknown).
- The read half of the hypervisor family publishes no write keys; the executor brings them.
- Performance budget: like the API times, the growth from 250 to 1000 devices is measured once more when a busy test machine puts a key over its limit; it fails only when both runs are over (limits unchanged).

## [1.26.1] – 2026-11-17

### Changed
- The bundled end-of-updates table carries identifiers, patterns and dates only, no display names; the entry sheet says "From the table: updates end on <date>". An own table may still give its entries a name.
- 1.26.0 was not released on its own; 1.26.1 contains all of it.

## [1.26.0] – 2026-11-17

### Added
- **Inventory and life cycle** (Devices | Inventory, `#/inventory`): every device carries what only a person knows - purpose, room, purchase date, warranty until, shop and note, serial number, the responsible person (one of the accounts), own firmware and an own end-of-updates date - next to what Lanloom found (maker, model, firmware with its source, serial number where the device announces one). Filters (needs attention, missing, probably gone, warranty ending or ended, updates ending or ended, unconfirmed, decommissioned), free-text search over every field, sorting, deep links, and editing many devices at once (own fields only, never name, type or serial number). The table becomes cards on a phone; the device card has an Inventory block. Guide: [docs/INVENTORY.md](docs/INVENTORY.md).
- **States over a device's life**: *active*, *missing* (no sign of life for N days, default 7), *probably gone* (missing, and reasons add up: long absence, a successor of the same kind and maker appeared, its switch port is used by another device, it normally stays on - shown in plain words, with *Decommission* and *Still here*) and *decommissioned* (date, replaced by, note; hidden from the map, kept as history; a note when it answers again). Signs of life are remembered across restarts, and nothing is judged while Lanloom has not looked at the network for two days.
- **Provisional entries**: an entry Lanloom made on its own is provisional until you confirm it (button, or by touching it: name, type, own fields, manual connection, hiding). Without a sign of life for 7 days (setting, 0 = never) it leaves the list once a day; the gateway, this computer, devices a connected family or hub knows and everything confirmed are never removed.
- **Warranty and end of updates**: chips for warranty (ok, ends in N days, expired) and for the end of a device's updates, from an offline table in the image (`lanloom/data/eol.json`, renewed with each release, never downloaded; advisory) or from your own date; an own table can be added in the data directory. Firmware comes from your own entry, a device-family adapter's equipment list, the hub's device registry or what the device announced.
- Nightly report section **Inventory**: devices that became missing or probably gone, retired devices that are back, warranties and end-of-updates dates that crossed a threshold (once, in the night it happened), provisional entries taken off the list.
- Roles: everyone reads; operators write the own fields, confirm, decommission and edit in bulk; admins change the thresholds. Every change is in the audit log with field names, never values. Command palette: *Open Inventory* and four filtered views; the device search finds a device by room, purpose, person or shop. Sample network: purposes and rooms, a missing device, one that is probably gone, one decommissioned, warranties and an end of updates.
- The UPnP description's serial number is kept (shown beside your own, hidden from AI texts together with your own serial numbers).

### Changed
- The Devices page uses the same width as the Inventory page, so the switch stays where it is.
- Performance budget: the inventory list is measured as `inventory_ms`.

## [1.25.0] – 2026-11-17

### Added
- Read adapter for Sophos Firewall (local XML API, see [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) and the guide [docs/integrations/sophos.md](docs/integrations/sophos.md)). Product names are used only to identify compatible products; no affiliation or endorsement.
- **Zone firewall, read only**: zones with the management services they allow, interfaces, VLANs and aliases as networks, firewall rules in their order (network and user rules, one row per source and destination zone, addresses and services in plain words, schedules), rule groups, port forwarding from NAT rules, source NAT, DHCP servers with fixed addresses, static routes, IPsec and SSL VPN. The Firewall tab shows the zone matrix (traffic no rule allows is dropped), the rules, port forwarding, a new **Checks** card and a table of internet connections, VPN connections and other objects; rules for every zone are grouped under *any zone*.
- **Checks** in plain words: management open on the internet side, a rule that lets everything in from the internet side, an enabled any-to-any rule, rules that are switched off.
- **Nightly report section** of the firewall (only with such a connection): rules added, removed, switched on or off or acting differently since the last report, port forwarding added or removed, and the checks. The report keeps one short fingerprint per rule instead of the rule list.
- Command **Firewall checks** in the search (`Ctrl K`): opens the Firewall tab on the firewall source with the checks in view.
- Setup assistant: suggests the family for a device whose admin console answers on its default port with a matching page title (never a sign-in).
- Demo: a *Sample firewall* with zones, rules, forwarding, VPN and a rule that arrived overnight.
- Transport: a read allow-list may hold one *query* rule for devices whose read API takes a query document by POST - form-encoded, one path, and a mandatory check per field that proves the document only reads; anything else is refused before a byte is sent. The adapter's check allows exactly an account and a `<Get>` of known objects.

### Security
- The adapter never asks for users, certificates or the administrator list; secrets that a reply may carry (pre-shared key of an IPsec connection, PPPoE password of an interface) are dropped in the same step and never stored, logged or shown; after a refused sign-in it waits 15 minutes so a wrong password cannot lock the account; reply documents with type or entity declarations are refused unread.

## [1.24.0] – 2026-11-17

### Added
- **Networks and VLANs.** *Settings → Network* lists every network Lanloom knows – the networks of this host's interfaces, the networks and VLANs a controller adapter publishes (name, VLAN number, address service, isolation) and private networks behind a router – and how far it may look into each: *scanned directly*, *scan allowed across router*, *adapter data only* or *never scanned* (with the reason). Behind a router nothing is sent into a network unless an administrator agrees to a gentle scan across it (per range, revocable, audit entry `zone.consent`); automatic rescans include only on-link networks and agreed ranges. The network map titles every network and VLAN zone with its name, VLAN number and range.
- **Devices only an adapter knows.** Clients of a controller (also in other VLANs), the address table of a switch (SNMP) and LLDP neighbours join the inventory without a scan (*Controller or switch data* on the card). A switch in *Settings → Network* turns this off; the first booking is not reported as "new devices" in the nightly report.
- **Devices without an IP address.** A hardware address no IP address is known for is kept under its hardware address, drawn with the address as its subtitle, and folded into the device it belongs to when the evidence is strong: same registered block, same switch port and first seen within ten minutes while the other device has an address; an LLDP neighbour's chassis and port addresses; several addresses of one device in the hub's device registry. Grouping stays undoable.
- **Scan protection.** Three fixed classes: *printer* (never touched on 9100–9102 and 515, not even by a connection that closes right away), *industrial* (gentle scan: web ports 80 and 443 only, no name requests) and *passive only* (no packet at all; medical and payment systems and everything you mark). The class comes from what a device announced or an earlier scan saw and stays until you set another one on the device card (*No protection* switches the automatic class off); the presence check and the sweep of routed ranges obey it. The industrial and medical ports (102, 104, 502, 2575, 11112, 20000, 44818, 47808) are probed only by a deep scan with consent, never on a protected device. Audit entry `device.protect`.
- **What can be connected.** A built-in catalog of device classes (hub, MQTT broker, SNMP devices, controllers, hypervisors, storage devices, mobile-network routers; planned: UPS, printers, cameras, container hosts, DNS filters) with a read-only-account guide for each. The device card has a *Can be connected* section (the *Connect* button opens the usual dialog, pre-filled with the address only) and *Settings → Network* lists everything, best first. Planned classes say so and offer no button. Nothing is sent to a device to build a suggestion; no new outgoing connection.
- API: `GET /api/zones`, `POST /api/zones/consent` (administrator), `GET /api/protect`, `POST /api/devices/{id}/protect` (operator), `GET /api/integrate`, `GET /api/devices/{id}/integrate`; setting `adapter_inventory`.
- Demo: three adapter-only networks (smart home, cameras, guests), a second network card without an IP address, a protected printer, planned catalog entries.
- Guide: [docs/DISCOVERY.md](docs/DISCOVERY.md); README section with screenshots.

### Fixed
- The devices list: a row without an IP address shows its hardware address, and the row of a second interface opens the card of the device it belongs to (it has no card of its own on the map).

### Changed
- The scan result carries a `protect` field per host; port 20000 is listed as an industrial port in the deep scan.

## [1.23.0] – 2026-11-17

### Added
- **Change clients through the executor** for UniFi Network controllers (stage B; product names are used only to identify compatible products, no affiliation or endorsement): move a wireless client to another network (network override plus a fixed address there), give a client a fixed address, change or remove it, and rename it. The write half runs only in the executor service with a site-administrator account of its own that the app never sees; client settings exist only in the classic API. Everything else is refused by a field-exact allow-list below the adapter. Guide: [docs/integrations/unifi.md](docs/integrations/unifi.md#changing-clients-stage-b).
- **Device sheet in the firewall editor**: tap or click a device in Change mode (on a phone too) to move it, rename it or give it a fixed address - only what the connected controller can change is offered. The draft bar collects moves and changes per device; the preview shows every field old → new, and a device that is moved and renamed in one draft becomes one step.
- **Checks before anything is written**: a new or fixed address must lie inside its network, must not be the network's own address or its gateway, must not belong to another device and must not be used twice in one plan - in the preview, again in the executor's gate against a fresh read, and once more in the write half (controller software stores some wrong combinations without a word). After writing, every client is read back; a value that did not take rolls the change back.
- **Connection guard in the executor**: the executor process can connect only to the app and to its controller; anything else is refused before a connection is made. `LANLOOM_EXECUTOR_DENY_HOSTS` names addresses that are always refused, for example the real controller while the executor rehearses against a test controller.
- Write half sessions: at most four sign-ins an hour, counted across executor restarts, a 15-minute pause after a refused session, one new sign-in after HTTP 401 and none after 403; the connection can be bound to the controller's certificate (`cert_sha256`). An account that cannot write is refused, an owner-level account gives a warning.
- Known clients that are offline but carry settings of their own (a name, a fixed address, a network override) appear in the Firewall tab with their last network, so they can be renamed or get their address before they return.
- Command palette: *Change a device (network, name, fixed address) …*, and the action *Move to … / change* on a device hit; deep links `#/firewall/device/<address>` and `#/firewall/pick`.
- Nightly report section **Changes written**: what the executor changed since the last report (done, undone, refused), with a point for each change that was put back on its own or could not be put back. Rehearsals on the sample network never appear. The demo shows one of each.
- The sample network (simulation and demo) can rename devices and change fixed addresses too.
- Notification event **Controller change put back** (Settings › Notifications, group *Changes on the controller*): a change on a real controller that was put back on its own (warning) or could not be put back (critical). Finished changes are not announced; they are in the nightly report.

### Changed
- A device whose network follows its switch port (connected by cable) and a device whose network is not known while it is offline are no move target any more; the preview says why instead of the executor refusing the plan later.
- The plan fingerprint accepts measured values with decimals in the controller state (for example a port's power draw) instead of failing.
- Live configurations read by a device-family adapter never offer write controls, whatever the family could write through the executor.

## [1.22.1] – 2026-11-17

### Changed
- The service worker of the notifications is registered only where it is needed – on the notifications page, in the installed app and in a browser that already receives push – and no longer takes over a page that is already open. A plain browser tab has nothing between it and the server; a page reload under heavy load in one browser engine waited for the worker in the release check.
- 1.22.0 was not released on its own; 1.22.1 contains all of it.

## [1.22.0] – 2026-11-17

### Added
- **Notifications – calm by default.** Every event still goes into the nightly report. Under *Settings → Notifications* each person (any role) chooses per event *Immediately*, *Bundled every hour*, *Nightly report only* (default) or *Off*, from which severity on, and on which channels. Two presets: *All in the nightly report* and *Important sooner*.
- **Events**: new device, new device in a **protected network** (administrators choose the networks; critical), device offline (network equipment and servers as a warning), internet down, connection not answering, sign-in refused by a connection, certificate expires within 14 days, backup too old, Lanloom update available, and security notices of the own account (only to that person). *Incident* and *Vulnerability* are registered for the features that will report them; later features add their own events with `lanloom.notify.events.register` and `core.notify.emit`.
- **Channels**: **web push** on the person's own devices – VAPID key created on first use and kept in the encrypted secret store, messages encrypted for each browser (RFC 8291) and signed (RFC 8292) without any extra package, one subscription per browser, listed with its last message and removable from any device; subscriptions the browser ended are forgotten. Plus the shared channels of the nightly report delivery (e-mail, webhook, push topic, chat bot) once they are set up, with an optional own e-mail address per person and a *Send test* per channel. Webhook notifications are JSON with `type`, `event`, `severity`, `title`, `text`, `link`, `time`.
- **Quiet hours** per person (for example 22:00–07:00): only *Immediately* + *critical* gets through; the rest waits for the nightly report.
- **Maintenance window** (planned by operators and administrators): one-off or every week (also across midnight), for all or for some devices, *Maintenance for one hour from now* with one click or from the search (`Ctrl K`). Nothing is announced while a window is active, occurrences are marked as maintenance (`maintenance_at` for incident handling) and no all-clear follows; the start page shows an active window.
- **Flap protection**: an outage must last three minutes (one to fifteen) before it is announced; a 12-hour cool-down per subject (1 to 24); an all-clear only after an announced outage and only to the people who were told; a **daily limit** per person (default 20) with one note when it is reached.
- **Heartbeat to the outside** (administrators, optional): Lanloom calls an address of yours every 1 to 60 minutes so an outside service notices when Lanloom itself is down; stored encrypted, *Call now* to test.
- **History** of the last 30 days with what the rules did (sent, bundled, failed, nightly report, quiet hours, maintenance, flap, cool-down, daily limit, no channel); administrators see the outcome per person.
- **Nightly report section "Notifications"**: messages sent, held back by quiet hours, during maintenance, flaps, repeats, over the daily limit; failed deliveries and a failing heartbeat need a look.
- **Service worker**: web push and the offline shell of the installed app (network first, the API is never cached), registered only on the notifications page, in the installed app or once push is allowed.
- Demo: rules, a weekly and a past maintenance window, a day of history with every outcome, a heartbeat.
- API: `GET /api/notify`, `PUT /api/notify/prefs`, `GET /api/notify/history`, `POST /api/notify/push`, `POST /api/notify/push/forget`, `DELETE /api/notify/push/{id}`, `POST /api/notify/test` (any role, own data), `POST`/`DELETE /api/notify/maintenance` (operator), `PUT /api/notify/settings`, `PUT /api/notify/heartbeat`, `POST /api/notify/heartbeat/test` (admin); every change is written to the audit log.
- Guide: [docs/NOTIFICATIONS.md](docs/NOTIFICATIONS.md); README section with screenshots; SECURITY.md lists the new opt-in outbound connections.
## [1.21.1] – 2026-11-17

1.20.0 and 1.21.0 were not released on their own; their changes are part of this version.

### Fixed
- English texts of the start page, the rails and the live grade dialog use English quotation marks.

### Changed
- Performance budget: a budget check that lands over its limit on a busy machine is measured once more with a fresh calibration before it fails; the limits are unchanged.

## [1.21.0] – 2026-11-17

### Added
- **One component, two sides**: a device the home-automation hub knows and the scan finds is drawn as one component. Matching uses the hardware address from the hub's device registry (95 %), the IP address of the device's configuration page together with a tracker of the hub (85 %) or alone (70 %); the same name on both sides is only a suggestion (45 %). Merged devices carry a **HUB** badge; their card starts with the *Automation hub side* – which evidence decided, name, model, firmware and room at the hub, integration and entities. *Show separately*, *Same device* and *Different devices* need the operator role and go into the audit log (`hub.match`). See [docs/HUB-BRIDGE.md](docs/HUB-BRIDGE.md#one-component-network-side-and-hub-side).
- **Radio networks as lanes**: the hub's radio networks (802.15.4 mesh, Thread mesh, sub-GHz mesh, BLE, IP smart-home standard; recognised from the device registry, shown with neutral names) are dashed lanes – on the map one tile per network from the hub, on the network view a zone per network with the coordinator (or the network) and its radio devices behind it, one radio mark per network, up to 36 devices drawn and a "+N more" tile.
- **Radio freshness**: a radio device is fresh while one of its entities is not `unavailable` and, when the integration reports "last seen", while that is younger than 24 hours – never by the age of the last update. Silent devices are red; a network with a silent coordinator or half of its devices silent is *disturbed*.
- **Event rings**: each state change of the hub travels as a small hollow ring along the lane of its radio network or the trace of its room – a dot shape of its own, separate from the byte dots (none with reduced motion).
- **Sensors tab** (appears with a hub connection, also in the phone's bottom bar): every sensor grouped by room with search and room filter; *On site* – batteries to change now (≤ 10 % or reported low) or soon (≤ 25 %) and silent radio devices, with battery bars; *Radio networks* – health, coordinator, devices, silent devices, low batteries, BLE receivers (radio proxies), Thread border routers, devices of the IP standard and state changes per integration.
- **Sensor page** for every entity: 1 hour from Lanloom's own live memory, 24 hours from the hub's recorder, 7 and 30 days from the hub's long-term statistics (or the recorder), thinned out at the server – numbers as a minimum-to-maximum band with the mean as a line, states as a state bar with the share of each state; hover shows the values.
- The hub bridge may ask for the history of **one entity on request** (`history/history_during_period`, `recorder/statistics_during_period`; never a private entity, at most 12 per minute, answers kept for 5 to 60 minutes) and keeps the device class of an entity (battery, timestamp, ...). Static tests check the extended allow-list.
- Search and commands (`Ctrl K`): *Go to Sensors* and, when typed, its parts *On site* and *Radio networks*; *Open the sensor page* is the first action of an entity hit.
- Nightly report section *Radio and batteries*: silent radio devices, batteries to change now, silent receivers and border routers and disturbed networks as points, batteries getting low as notes; the AI features see it only as its summary.
- API: `GET /api/hub/{id}/sensors`, `GET /api/hub/{id}/entity/{entity_id}?range=1h|24h|7d|30d` (viewer), `POST /api/hub/{id}/match` (operator); `/api/state` names the hub connections (`hubs`).
- Demo mode: matched devices (by hardware address, configuration page plus tracker, configuration page, and a name suggestion), five radio networks with a coordinator, a controller, a border router, two BLE receivers, batteries at 7 %, 18 % and 22 %, a silent contact and a silent blind, and recorded courses for every sensor.

### Fixed
- The demo's runaway automation also shows at night (the sample data quieted every automation between 1 and 6 o'clock).

## [1.20.0] – 2026-11-17

### Added
- **Overview as the start page.** After signing in Lanloom opens *Overview*: a health banner ("All good", "N things need a look", "Something is down") with the last 24 hours in five-minute steps, six key figures – devices, online, new since yesterday, fresh sources, the internet line, data paths of the day – each a link to the details, and cards for what stands out (one line and one sub line per issue), the latest nightly report, new devices (first seen, origin of the address), devices whose type is not yet certain, and every source with its last sign of life. The page redraws itself every ten seconds and keeps folded cards and "Show more" as you left them.
- **Calm states for sources**: "Fresh", "Source waiting", "No data yet", "Stale", "Connection fails", "Off" – nothing shows an error just because it has nothing yet.
- **Right now** rail (key `L` or the pulse button): noteworthy issues, active conversations with their rates, the changes of the last six hours and new devices; refreshes every five seconds while shown, rests in a background tab; shown or hidden is remembered.
- **About this view** rail (key `I` or the *i* button): a short explanation of the open view with a link to the new manual page [docs/manual/views.md](docs/manual/views.md); open by default from 1900 px. Below 1280 px both rails slide over the page, on a phone they fill the screen.
- **Live state in the header**: "Live · 14:32:05", "Updated 12 s ago", "Disconnected" with a **Reconnect** button, "Paused" while the tab is in the background (the live feed then rests). The live grade dialog also shows the **clock offset** between the browser and the server.
- **Deep links and the back key**: the address follows the open component card (`#/map/<id>`), the device card (`#/devices/<id>`), the nightly report (`#/reports/<day>`) and the map filter; the back key closes the card or returns to the report or filter shown before.
- **Map filter**: chips above the board show one area (zone) or problems only (`#/map?zone=…`, `#/map?only=problems`). The device list opens filtered from the start page (`?show=new|unsure|offline`).
- Commands in the search (`Ctrl K`): Overview, show or hide *Right now* and *About this view*, live grade and clock, reconnect.
- API: `GET /api/overview` and `GET /api/overview/now` (any signed-in role, read only). Later features add noteworthy entries with `lanloom.overview.register_note`, figures and cards with `Lanloom.registerOverviewTile` and `Lanloom.registerOverviewCard`.
- Demo: two devices that joined today, a day of health history and recent changes, so every part of the start page and the rail shows something.

### Changed
- Signing in, finishing an invitation or a second factor now lands on *Overview* instead of the map.
- The header with eight tabs: from 1401 to 1700 px the tabs show their names without icons, from 701 to 1400 px icons only (the name as a tooltip); the theme button moves into the settings and the search below 960 px. The search button keeps its place.

## [1.19.0] – 2026-11-17

### Added
- **Search and commands** (`Ctrl K`, `⌘ K`, `/` or the Search button in the top bar): one box that finds devices, virtual machines and containers, services (which device offers which service, with the port), automations and scripts, and entities of a home-automation hub, and that runs commands – every view, the settings pages, light/dark, each colour theme, the rate unit (bits or bytes) and "Check for updates". Every word has to match; typos are forgiven from four letters; "Recent" shows your last choices (kept in this browser only; "Clear recent items" empties it).
- **Value filter**: a comparison such as `> 25 °C` or `<= 40 %` lists hub entities whose current number fits (units are compared when typed), highest or lowest first; the words of the query still narrow by name.
- **Actions of a hit** as a menu (`→` or the dots on a phone): Open (the card of the device), Copy address, Show the hub card, Copy ID. An action is offered only when its view exists; features register their own with `Lanloom.registerAction`, commands with `Lanloom.registerCommand`.
- Deep links `#/map/<id>` and `#/network/<id>` open the card of a component (the page switches to the board that has it).
- API: `GET /api/search?q=&limit=` (any signed-in role, read only, not audited). The hub entities are indexed once per snapshot; a search over 3000 of them answers in well under 50 ms (typically 3 to 10 ms on a small computer).
- Keyboard and screen reader: combobox with a listbox, `aria-activedescendant`, announced result count, focus returns to where it was; on a phone the palette fills the screen. English and German texts, demo data for every group, documentation in [docs/PALETTE.md](docs/PALETTE.md), screenshots.

## [1.18.1] – 2026-11-17

1.18.0 was not released on its own; its changes are part of this version.

### Added
- **Settings › Storage** (administrators): how much space each kind of data takes (inventory, data paths, availability and events, nightly reports, audit log, scan history, accounts) with a bar and a table, the database file, free space inside it, stored backups, free disk space, the growth per day, and what Lanloom holds in memory.
- **Retention per kind of data**: live history in memory (5–60 minutes), data paths per hour (1–7 days) and per day (31 days – 1 year), availability and events (35 days – 1 year), nightly reports and the audit log (the same settings as under Reports and Security), scan history (last 50–1000). Changes are written to the audit log.
- **Nightly clean-up** after 03:00 (or at the next start after a missed night): removes what is past its retention, refreshes the query statistics and compacts the database when a sixth of it is free; skipped with a note when the disk has no room for the copy. "Clean up now" runs it by hand.
- Nightly report section **Storage**: the clean-up of the night, the database size and its growth, and a point when the data volume would be full within 30 days at the current growth.
- **Counter check**: every hour two meters that see the same traffic are compared – the router's internet uplink counter with the bytes from NetFlow/IPFIX, and this host's interface counters with its connections. A ratio outside 80–125 % (only "too much" for the host) is a finding: counted twice or not counted. Shown in the source view (the live grade button) and as a nightly report section.
- **Map animation setting** (Settings › Appearance, per browser): automatic (up to 30 frames per second), smooth (60), saver (15, fewer dots) or off (glow only).
- `scripts/benchmark.py`: server time of the main APIs, the live loop and memory for 50, 250 and 1000 generated devices; a budget test keeps them within limits relative to the machine. Measured values in [docs/PERFORMANCE.md](docs/PERFORMANCE.md) and the hardware table in the README.
- API: `GET /api/storage`, `PUT /api/storage/retention`, `POST /api/storage/cleanup` (administrators), `GET /api/selfcheck/counters`.
- Demo mode: storage history, a nightly clean-up and a counter check with one hour counted twice.

### Changed
- **Frame budget of the map**: the dot animation draws at most 30 frames per second by default (20 without graphics acceleration) and steps down on its own (to 20, 15, 10) when drawing gets slow, back up when there is room; it stops while the tab is hidden or the board is out of view, and nothing moves without a live change.
- **No polling of the map and the data paths**: the live connection carries a change number; the map is fetched only when statuses, flow traces or sources really changed, the data paths only after new hourly data (at most every 20 seconds), never for a hidden tab. An open component card stops refreshing while the tab is hidden.
- The legend over the map no longer blurs what lies behind it (a blur over the animated layers made some browsers redraw it every frame).
- Live history in memory takes 24 bytes per sample instead of about 150, and traces that went quiet are forgotten after the retention.
- Device views are built once and reused until the device changes; the network detection is read at most every 30 seconds; the open component card reuses the map for up to 5 seconds. At 250 devices the device card answers about 15 times faster on a small host.

## [1.17.2] – 2026-11-16

### Added
- NetFlow / IPFIX: **sampled exports are scaled up** by their sampling rate – from the NetFlow v5 header, the v9/IPFIX sampling interval of a record or in the exporter's options data, and the IPFIX packet interval and space – so an exporter that looks at only 1 of N packets no longer shows N times too little.
- NetFlow / IPFIX settings: while no exporter is entered, a button offers the gateway Lanloom detected (never filled in silently).
- Data paths map: keyboard zoom – Tab to the map, `+` and `-` zoom at the centre, `0` shows the whole world; the map shows a focus ring.

## [1.17.1] – 2026-11-16

### Added
- **NetFlow / IPFIX receiver for data paths**: the world map can show every device of the network, not only the Lanloom host. Your firewall or router sends NetFlow v5, NetFlow v9 or IPFIX (v9 and IPFIX with IPv6) to Lanloom, which books each flow per device and country into the same tables; only country, device id and bytes are kept, no addresses. Off by default, in *Settings › NetFlow / IPFIX from your router*: UDP port (standard 2055) and the **allowed exporter addresses**, which are required – without one the receiver cannot be switched on, and packets from any other address are dropped and counted (logged once per sender, then at most every 10 minutes). Templates are kept per exporter; records whose template has not arrived yet wait. Runs inside the app, no second container; with host networking no port mapping is needed. The settings card shows the receiver state (port, last packet, flows, booked, dropped, waiting for a template). See [docs/DATA-PATHS.md](docs/DATA-PATHS.md#setting-up-netflow--ipfix).
- The footer of the data paths view names the actual source ("connections of this host", "NetFlow from" the exporter, or both), and a note under the map explains that routers usually report a connection when it ends, so long streams appear late, and that an active timeout of about 60 s helps.
- Addresses without a known device are booked as **Unknown device**; the IPv6 addresses of known devices are recognised; extra own networks (for example an IPv6 prefix) can be added. The Lanloom host's own flows are skipped in NetFlow, because the host source counts them exactly.
- **Zooming and panning on the data paths map**: mouse wheel at the pointer (up to 16×), drag to move, pinch on a phone, double click or the reset button to see the whole world. Outside the map, or fully zoomed out, the page scrolls as usual; one finger moves the map only while it is zoomed. Lines, dots and labels keep their size on screen, labels are laid out again for every zoom level, and the zoom works in the full screen and survives the live refresh every minute. Based on a contribution from a tester.
- Label points for the territories the country data knows but the base map had no shape for (Bonaire, Bouvet Island, Christmas Island, Cocos Islands, Gibraltar, Guadeloupe, Martinique, Mayotte, Réunion, Svalbard, Tokelau, the minor outlying islands of the United States), so their traffic is shown; a test checks every country code of the country data.
- **Home-automation bridge 2**: the hub connection also follows automation and script runs, reads which integration each device belongs to and the hardware addresses of devices from the hub's registry (for matching with network devices). The hub's component card has a new *Home-automation bridge* section: what is read, what stays out for privacy, how the hub is spared, the busiest automations, script loops and paused lookups. See [docs/HUB-BRIDGE.md](docs/HUB-BRIDGE.md).
- **Admin mode** (off by default, needs an administrator token on the hub): reads which entities each automation and script uses per role (trigger, condition, action) and related items, and on request the last runs of one automation with result, last step and step path. Only entity ids are kept – never aliases, templates, values, variables or error texts.
- **Load protection**: templates are never rendered; configuration reads (30 per minute), related-item lookups (20 per minute) and run lookups (6 per minute, only on request) each have a budget; fresh answers are not asked again; scripts that call each other in a loop are never looked up (the hub would recurse); an item the hub answers with an error is paused for 24 hours, doubling up to a week; a message type an older hub does not know is not sent again.
- **Privacy filter**: people, trackers, zones, cameras, pictures, calendars, to-do lists and notifications are dropped inside the bridge by default (each group can be switched on per connection). Trackers only lend their hardware/IP address for matching. Events carry entity, state and time, plus which automation caused a change or just "user" – never service data, user ids or context ids. Hub data stays in memory (never in backups); the AI features see the home-automation report section only as its summary, and "Explain" on one of its notes hides the automation name.
- Nightly report section *Home automation*: runaway automations (60 runs or more within an hour) and script loops as points, paused lookups and the busiest automations as notes.
- API: `GET /api/hub/{id}/bridge`, `GET /api/hub/{id}/automations`, `GET|POST /api/hub/{id}/traces/{entity_id}` (asking for runs needs the operator role and is written to the audit log).
- Demo mode: sample automations and scripts with run counts, a runaway automation, a script loop, a paused lookup and sample runs.

### Fixed
- **A country lights up only with its main area**: France no longer lights up French Guiana in South America. French Guiana is now a shape of its own (its own country code); far-away parts of a country without an own code are drawn but never lit with it (the map builder measures the distance on the globe, so the two sides of the date line stay one country).
- **`latest` after a one-click update**: the updater created the new container with the version tag, so from the second one-click update on the moving tag of the compose file (`latest`) stayed on the version of the first update, and a later `docker compose up -d` or a host restart with a recreated container could start that old version again. The new container now keeps the image reference of the compose file, the tag is moved to the new version before the swap and back on a rollback, and the compose image label follows the new image. At start the updater moves a `latest` that lags behind the running version forward (never backwards, for example after a manual pull). An updater container from before this version is shown in Settings › Updates with the one-time command to renew it: `docker compose pull && docker compose --profile updater up -d`.

## [1.16.0] – 2026-11-16

### Added
- **Ask your network** (with an AI set up; operators and admins): an *Ask* button in the top bar opens a conversation panel – a sheet on wide screens, full screen on a phone. Questions about devices, availability, data paths, the firewall configuration and the nightly reports are answered from facts the Lanloom server builds from its own data for each question; the browser only sends the question and the last turns, kept for this tab. Answers name devices and views with references that become links (a device opens its card, a view its tab); references that do not exist are dropped. *Preview what is sent* shows the exact text. Registered as a command for the coming command palette. See [docs/integrations/ai.md](docs/integrations/ai.md#ask-your-network).
- **Nightly report: In plain words**: an optional AI paragraph above each report, written after the report is made. The rule-based report stays the reference; without an answer within four minutes the paragraph is missing. Operators can write it (again) for any saved report. It goes into deliveries (mail, push, chat, webhook field `plain`) only when *Put the paragraph into the delivered report* is switched on (off by default).
- **Suggestions for unknown devices**: *Ask AI what this is* on the card of a device Lanloom is unsure about returns one of Lanloom's kinds with a reason. Confirm or dismiss; a confirmed suggestion counts as a weak clue (confidence 0.3, reason "AI hint") that only fills in a kind where Lanloom has nothing better – your own choice always wins. Optional nightly suggestions for up to five unsure devices (off by default); the nightly report's AI section counts new suggestions waiting for a decision.
- **Firewall suggestions as a draft**: *AI suggestions* in the firewall editor proposes moves to other networks – only moves the editor itself allows. Asking never writes the draft; the suggestions you pick join it with *Take over as draft*, followed by the usual preview, confirmation and signature.
- Settings › AI: *What the AI may do* (paragraph, its delivery, nightly suggestions) and a cloud permission for each new feature (answering questions, the paragraph, device suggestions, firewall suggestions); all off for the cloud until allowed.
- Device deep links: `#/devices/<id>` opens the device list with that device's card.
- Audit log actions for questions (never the question itself), paragraphs on request, device suggestions and decisions, and firewall suggestions.
- Demo mode: the sample AI answers questions with links, writes the paragraph for the sample reports, has an open suggestion for an unknown device and proposes two moves in the firewall simulation.

## [1.15.0] – 2026-11-16

### Added
- **Backup in the app** (Settings › Backup, admins): one file with the whole configuration – settings, users and roles with their second factors and recovery codes, passkeys, every connection with its credentials, the inventory with every name, kind and hidden device set by hand, "connected via" entries, firewall drafts and the audit log; history (scans, event journal, nightly reports, data paths) optionally, with its size shown first. The file is encrypted with your passphrase (Argon2id, AES-256-GCM with an authenticated header); credentials are only inside the encrypted content, and the instance key never leaves the host. See [docs/BACKUP.md](docs/BACKUP.md).
- **Automatic backups**: every 6 or 12 hours, daily, every 3 days or weekly into `/data/config-backups`, keeping 1–60 files; the app keeps only a key derived from the passphrase. Stored backups can be downloaded, restored or deleted, and one can be written at once.
- **Restore** with a preview of what is replaced and what stays (accounts, connections, devices and names, connections set by hand, drafts, settings, credentials, history, audit log, sessions that end). A safety copy of the database is taken first; credentials are encrypted again with the key of the restoring instance; every session ends; the restore is written to the audit log and every admin gets a notice. An instance with its own audit log keeps it. Wrong passphrases are rate limited; a damaged or changed file and a file that is not a backup are refused with a clear message.
- **Versions**: a backup from an older version is brought up to date while restoring; one from a newer version is refused and names the version to update to first.
- **Move to another host**: on the first-start page, *Restore from a backup instead* (needs the setup code) brings accounts, second factors, connections and names to a new instance, also across architectures – tested from an arm64 board to an amd64 server and back. The new host keeps its own instance key; old sessions are not valid there.
- Nightly report: new section "Backup" (age of the newest backup, automatic backups written, a restore) with a point when automatic backups are on and the newest one is older than twice the interval, or when the last automatic backup failed.
- Audit log actions for exporting, downloading, uploading, checking, writing, deleting and restoring backups and for changed backup settings.
- Demo mode shows automatic backups with three stored files.

## [1.14.0] – 2026-11-16

### Added
- **Setup assistant 2.0: a first picture in minutes.** The networks step now shows what Lanloom found on the host without sending a packet (interfaces, routes, neighbour table, name servers, DHCP lease files): gateway, DNS and DHCP servers, and the ranges it proposes. Proposals are private and at most a /22 each; a larger interface network is narrowed to the smallest range around this host that holds every known neighbour, and routed networks behind a router are listed but never pre-ticked. Scanning still needs your explicit consent. *Start gentle scan* begins right away; *Scan options …* leads to the deep scan.
- **First picture during the scan**: after the quick phases (neighbour table, mDNS, SSDP, WS-Discovery) the devices already appear, before ports and web pages are read. *Open the map now* leaves the assistant while the scan finishes in the background. The time from the first setup screen to the first picture is measured and shown at the end; on the isolated test network the first picture is ready after about 7 seconds.
- **Adapter suggestions**: from what the gentle scan saw (open ports, web page titles, mDNS and SSDP announcements) the connect step suggests a home-automation hub, an MQTT broker, SNMP at the gateway and switches, and every device-family adapter whose manifest declares detection signs (controller and hypervisor families so far). Nothing is ever tried without your credentials. Each suggestion has a step-by-step guide for a read-only account or token, a form prefilled with address and port (never a credential) and a test button. See [docs/INSTALL.md](docs/INSTALL.md#first-start).
- **Clear test results**: a failed connection test now says what to look at – credentials refused, certificate, not reachable, or something else – with a hint for each.
- **Sample network from the assistant**: *Explore a sample network* (networks step and Settings › Setup assistant) shows a fictional network next to your own, read-only and only for this browser session, with a banner and *Leave the sample*. It lives in a separate in-memory store; your inventory is not read or changed, every change is refused there, and it ends with sign-out.
- **Run the assistant again** under Settings › Setup assistant (admins); devices, names and connections stay. *Back to the map* in the assistant header leaves it at any time.
- Adapter manifests can declare `detect` signs (port, page title, mDNS type, SSDP text); the registry validates them. See [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md).
- The gentle scan also checks the web port of hypervisors (8006), 27 ports in all.
- Demo mode: the sample controller and hypervisor answer on their usual ports, so every suggestion appears in the demo.

### Changed
- Screenshots of the assistant renewed; README section "First start", installation guide, discovery notes and FAQ updated.

## [1.13.0] – 2026-11-16

### Added
- **Maker, model and firmware on every device card**: what the device says itself (UPnP description, mDNS TXT records, SNMP system description) wins over the address register; the source of each value is shown. See [docs/DISCOVERY.md](docs/DISCOVERY.md).
- **Offline address register**: the public IEEE MA-L, MA-M and MA-S lists ship with the image as a compact, checksummed file (about 450 KB, 54,000 prefixes, longest prefix wins) and are renewed with each release. Nothing is downloaded at run time; a newer table can be dropped into the data directory. Private (random) addresses never get a maker.
- **Address origin** per device: registered maker, private or random address, unregistered address, or only an IP address known.
- **One device, several interfaces**: two addresses of one device (two network cards, cable and radio) are drawn as one component when the evidence is strong (same UPnP device id, same SNMP system name, same NetBIOS or mDNS name, neighbouring hardware addresses on the same port or first seen together). The card lists the interfaces with reasons and confidence; weaker cases are offered as suggestions (*Same device* / *Different devices*), and *Show separately* undoes a grouping. Changing it needs the operator role and is written to the audit log.
- **More ways to find devices**, all gentle and without privileges: the kernel's IPv6 neighbour cache is read (nothing is sent) and IPv6 addresses are attached to their devices; one WS-Discovery probe; one NetBIOS name request per found device; one LLMNR reverse-name request for devices without a name. A new scan step *Names and identity*.
- **Optional SNMP with the factory community** (off by default, in the wizard and in Settings › General › Network scan): reads only the system description, object id and name.
- The classifier also weighs WS-Discovery types, NetBIOS names, SNMP answers, model words in TXT records and, weakly, neutral words in the maker's name.
- Each device keeps its raw identification features (address prefix, UPnP id and model, SNMP object id and description, WS-Discovery types, NetBIOS name) for the inventory.
- Nightly report: new devices name the registered maker of their address, or say that the address is private or in no register; a second interface of a known device is no longer reported as a new device.
- Demo mode shows makers from a fictional register, a storage box with two interfaces, a grouping suggestion, IPv6 addresses, a NetBIOS name, WS-Discovery types and SNMP data.
## [1.12.1] – 2026-11-16

### Fixed
- After an update from an earlier version, the existing account went straight to the set-up page for a second factor instead of getting the 48 hours to set it up. Found by the update test before 1.12.0 was released; 1.12.1 contains everything from 1.12.0.

## [1.12.0] – 2026-11-16

### Added
- **Several users with three roles**: viewer (looks at everything, changes only the own account), operator (renames and hides devices, sets manual connections, runs scans, ticks report items, prepares firewall drafts) and admin (settings, connections and their credentials, users, sign-in security, audit log, executor approvals, AI, updates). The rights are enforced on the server for every route from one central table; a test checks every route of the app against it, and a route missing from the table is for admins only. The web page hides what a role cannot use. See [docs/ACCESS.md](docs/ACCESS.md).
- **Invitations**: Settings › Users creates a one-time link (24 hours) bound to a role; open links can be withdrawn. Users can be given another role, disabled or deleted; at least one admin always stays.
- **Two-step sign-in**: authenticator app (time-based one-time codes, each usable once) with ten recovery codes, or a passkey as the strong factor. After the password, sign-in asks for the code, a recovery code or the passkey. A second factor can be required per role (Settings › Security); by default it is required for admins, with 48 hours to set it up, after which the next sign-in leads to a set-up page.
- **Confirmation for critical actions** (users, connections with credentials, sign-in security, AI pairing, own second factors): a passkey, an app code or (without a second factor) the password, valid for 10 minutes; a fresh sign-in counts.
- **Sessions**: Settings › Account lists where the account is signed in, ends single sessions or all of them ("Sign out everywhere"); admins can end the sessions of any user. "Public or shared computer" at sign-in gives a short session without a lasting cookie.
- **Password reset by an admin** with a one-time link that can also remove a lost second factor; the emergency way for a locked-out only admin stays as described in the installation guide.
- **Security notices** in the app for a sign-in from a new address or device, a lock-out after wrong attempts (also to all admins), a used recovery code and a password reset.
- **Audit log** (Settings › Security): every change, sign-in, failed sign-in and refusal with who, when, what and from where, sealed as a chain so later edits show up; filters, CSV export, adjustable retention (365 days by default). Secrets are never written to it.
- Nightly report: new section "Access" (lock-outs, failed sign-ins, new devices, new users, changed roles, removed second factors, broken audit chain).
- Optional sign-in through a trusted authentication proxy (`LANLOOM_AUTH_HEADER`, groups to roles with `LANLOOM_AUTH_GROUP_ROLES`), only from addresses in `LANLOOM_TRUSTED_PROXIES`; off by default.
- Serving below a sub-path: every address of the web page is relative, and `LANLOOM_BASE_PATH` lets Lanloom accept the prefix itself.
- Demo mode shows two sample users, an open invitation and sample audit entries.

### Changed
- The account card moved from Settings › General to the new Settings › Account. The language is now a personal choice of each user; the language chosen at setup stays the default.
- An existing installation keeps its account; it becomes an admin.

### Security
- Recovery codes and invitation or reset tokens are stored only as keyed hashes and work once; one-time-code secrets are stored encrypted. The second sign-in step is limited per client and per account and expires after 5 minutes or 5 wrong codes.
- The audit log is an append-only chain of HMAC-sealed entries with a key derived from the instance key; editing, inserting, removing or cutting off entries is reported. A name typed into the sign-in form is logged only if such a user exists.

## [1.11.1] – 2026-11-16

### Fixed
- Tests only, no behaviour change: the checks of the new language scanner no longer write helper files into the source folder (they failed in read-only test runners), and the list of texts that may equal their English original is keyed by pattern.

## [1.11.0] – 2026-11-16

### Added
- Plural forms inside the translations (`{n, plural, one {# device} other {# devices}}`): "1 device", "3 devices", "1 Gerät", "2.000 Geräte". About 50 texts that used to say "1 devices" or picked their form in code now carry their plural forms in the language files, in the app, on the map and in delivered night reports.
- Checks that every release has to pass (they run with the unit tests, so the release gate runs them too): all languages have the same keys and placeholders, plural forms are complete, no key is missing or unused, no text equals its English original by accident, every list of languages in the code agrees with the language files, and visible text in the web files only comes from the translation files. A planted gap in any of these places fails the check.
- A short section on suggesting translations in CONTRIBUTING.md.

### Changed
- Numbers follow the language everywhere: decimal comma and thousands separator in German in sizes, rates, counters, percentages, durations, load and the live values on the map tiles; also in the plain-text night report that is delivered. Times use the 24-hour clock, dates follow the language.
- The list of languages exists once in the app (one list for both language selectors and for picking the language of the browser, which now looks at all preferred languages).
- The self-test compares every language file with English, including the placeholders.

### Removed
- Five texts nothing used any more.

### Fixed
- The text of one scan refusal (IPv6 ranges) was missing in both languages; it is there now.
- Two tests of the night report depended on the time of day and failed after a while on the day they were written; they no longer do.

## [1.10.1] – 2026-11-16

### Fixed
- Tests: the dry runs of the installer and of the AI setup script are skipped in sandboxed test runners whose temporary folder does not allow executing files (the stand-in programs could not run there, so the shell used the real ones). No change to the app; everything from 1.10.0 is included.

## [1.10.0] – 2026-11-16

### Added
- **AI, local or cloud** (Settings, new sub-tab "AI" next to "General"): let an AI explain a device, a note of the nightly report or a firewall rule in plain words. Off by default; nothing leaves the host until you set up a service and switch it on. Setup is in two cards, "Local AI" and "Cloud AI". See [docs/integrations/ai.md](docs/integrations/ai.md).
- Choice of service: a local model server, compatible local servers, five cloud services and any compatible address; the list is in [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md). Cloud services need your own key.
- "Test the connection" asks the model a real question and shows the answer and the response time. "Load models" reads the model list from the service itself and pre-selects the recommended model. The order can be "local first, cloud only if needed" or "cloud first, local as fallback"; the default is local first.
- **One-click local AI on this computer** through the updater sidecar: the app only writes a trigger file, the sidecar creates the service next to Lanloom, so the app itself still has no container rights. The model is downloaded with a progress bar, and the setup counts as done only after the model has answered a test question. Without the sidecar the card shows the command to use instead. Models stay in their own volume when the service is removed.
- `install.sh --with-ai` adds the local AI service to a new installation (written to `compose.override.yaml`, safe to delete).
- **Another computer**: if this host is small, a setup script puts the model on a stronger computer that is always on. One command per operating system for the three common desktop and server operating systems; the script installs the model server, lets it listen in your network, downloads the recommended model and pairs it with Lanloom. The one-time setup code is valid for 4 hours and works once. Suggestions come from your own inventory (servers, storage and computers that are on). Looking for a running service only asks known addresses on the AI port (this host, configured and known devices); no address ranges are swept. Lanloom itself checks that the other computer answers.
- **Hardware advice** before a local model is installed: below 4 GB of memory no local AI is offered, below 8 GB only a small model with an honest warning; slow computers get a pointer to the other ways.
- **Explain** button on the device card, a note in the nightly report and a firewall rule (hidden while no AI is set up). The answer comes in the language of the app. "Preview what is sent" shows the exact text and which values are hidden before anything leaves.
- Nightly report: new section "AI" (requests per day, daily limit reached, failed requests); it appears only when AI is set up.
- Demo mode shows a sample AI service without any product names. The self-test checks the privacy layer.

### Changed
- The text of the update-check setting no longer calls the update check the only outbound connection; report delivery and cloud AI are optional connections of their own.

### Security
- Cloud services only get placeholders instead of IP and hardware addresses, host and device names, wireless network names, serial numbers and locations. The answer is translated back on the host. A final check refuses to send when an address pattern survived (fail closed).
- Every function needs its own permission to use a cloud service, and a daily limit caps the requests. A local address that leads to the internet counts as a cloud service.
- Keys are stored encrypted like other credentials and never appear in answers, logs, exports or reports.
- The setup code is stored only as a hash, is used up after a successful pairing and the pairing endpoints are rate limited; they accept only private peer addresses.
- AI never writes configuration. It only explains; every change still goes through the executor with preview and confirmation.
- New outbound connections exist only after switching them on: a cloud service you set up, and the download of the local AI service image and of the models when you ask for them.

## [1.9.0] – 2026-11-16

### Added
- **Colour themes.** Settings > Appearance > Colour theme: **Circuit green** (the default, the look of every earlier version) and **Aurora** (turquoise, blue and violet on a cool ground). A theme colours the whole app: surfaces, text, the traces and dots on the map, the data paths arcs, the firewall views, the charts and the live grades. Each theme works in light, dark and automatic mode. The choice is remembered per device, takes effect at once without reloading, and the phone's address bar follows the theme. Previews of each theme in the settings are drawn in the mode that is showing now.
- Warning and error colours keep their meaning in every theme (amber and red stay amber and red); the colours of the live grades follow the theme.
- Tests: contrast of every text and surface pair (4.5:1) and of every graphic colour on its grounds (3:1) in each theme and mode, no dark surface in light mode and no light surface in dark mode, complete token sets per theme, distinct trace colours, and no colour literal in any component.

### Changed
- Four colours of the light theme (warning, "near real-time" grade, "periodic" grade, "unknown" grade) and the copper of the board are a little darker so that they reach 3:1 against the light board. Nothing else of the Circuit green look changed.
- The settings field that switched light, dark and automatic is now called "Light / dark" to tell it apart from the colour theme.

## [1.8.0] – 2026-11-16

### Added
- **Nightly report** (new tab "Reports", also in the phone tab bar): every morning at 06:30 a short report about the night in plain words. The headline says "All quiet" or how many things need a look, followed by one section per source: internet outages and uplink switches, new devices and devices not seen for a day or briefly offline (with times), availability of every connection, connections whose sign-in is refused, data paths of the last 24 hours with their hints, changes to firewall rules, networks, wireless networks, port forwards and zones compared with the previous report plus changed firmware versions, the sections of device families, TLS certificates of your own connections (a note under 30 days, a point under 14 days or expired), and Lanloom itself (new version, own updates and restarts shown as maintenance, sources without data, free space). A section only appears when its source exists. In English and German.
- Points that need a look can be ticked off; the marks are stored with the report. The archive keeps 30 days by default (7 to 365). An unread report shows a badge on the Reports tab, on the map and in the phone tab bar; opening it marks it read. "Preview now" shows the next report as it would look at that moment, without saving or sending it.
- Report time and time zone can be set (default: the container's `TZ`, else the browser's time zone). If Lanloom was not running at report time, the report is made at the next start and says so in one line; several missed mornings become one report.
- Optional delivery of a three-line summary with a link: e-mail (SMTP), webhook (JSON), push topic (ntfy-compatible server) or your own chat bot. Every channel is off by default, has a test button and retries quietly after a failure.
- An event journal behind the report: once a minute Lanloom compares the live state (presence, connections, the gateway's internet port) and records every change, so the report knows what happened while nobody watched.
- Sections are a register: later features add their own section without touching the rest (see [docs/NIGHTLY-REPORT.md](docs/NIGHTLY-REPORT.md)). The sections device families bring through their manifest (since 1.7.0, for example the hypervisor) appear here, one per connection, compared with the state of the previous morning.
- Demo mode shows three mornings of reports with sample names and addresses.
- The self-test builds a report.

### Changed
- The phone tab bar holds seven tabs; "Data paths" uses the short label "Paths".

### Security
- Nothing leaves the host for the report unless you switch on a channel. A delivered summary never contains device ids or other internal values. Passwords, tokens, the webhook address and the push topic are encrypted like other credentials and never logged; delivery errors are reason codes without the address.
- For the certificate section Lanloom opens one TLS handshake per morning to each address of your own connections (hub, controller, broker, adapters), nothing else.

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
