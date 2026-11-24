# Lanloom

**Your local network, drawn as a circuit board.** Devices and services become components, connections become routed traces, and measured traffic flows along them as moving dots.

**License:** free for private, non-commercial use with up to 100 devices; commercial use requires a paid license. See [License](#license).

![Lanloom map in dark theme](docs/screenshots/hero-map-dark.png)

> ## ⚠️ Please read before installing
>
> - **It scans your network.** Lanloom probes the private address ranges you approve. Only scan networks you own or are explicitly allowed to scan. The optional deep port scan touches many ports per device and may trigger alerts in intrusion-detection or security tools; it only runs after you tick an explicit consent box.
> - **It stores credentials you give it.** Optional integrations (home-automation hub, MQTT broker, router or switch, switch controller, device-family adapters) need secrets. They are stored encrypted (AES-256-GCM) and never shown again, but the key file lives next to the data: whoever controls the host controls them. Use read-only or least-privilege accounts.
> - **Host networking.** To see your network, the container shares the host's network stack. It runs unprivileged, with a read-only root file system and all capabilities dropped, but it can reach whatever the host can reach.
> - **The optional updater sidecar gets the container-runtime socket.** Access to that socket is root-equivalent on the host. Skip the sidecar (`--no-updater`) if you prefer to update by hand.
> - **The firewall editor only simulates unless you arm it.** Real changes need the optional executor service, a built-in adapter for your controller family that can write (see [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) for what each family can change) and an explicit arming step. A real change can interrupt the wireless network for minutes and can lock you out if your network has paths the gate does not know. Read [docs/FIREWALL-EDITOR.md](docs/FIREWALL-EDITOR.md) first.
> - **Values are observations, not guarantees.** Classifications are heuristics, and traffic and liveness figures depend on what could be measured. Do not base security or safety decisions on them.
> - **Provided as is, without warranty.** See [LICENSE](LICENSE).
> - **Written with AI assistance.** Parts of this software were written with the help of AI tools; the code is reviewed and covered by automated tests and release checks.

## What it does

- **Overview first.** The start page says in three seconds how the network is doing: a health banner with the last 24 hours in five-minute steps, six key figures (devices, online, new since yesterday, fresh sources, the internet line, data paths of the day) that each link to the details, and cards for what stands out, the nightly report, new and uncertain devices (first seen, origin of the address) and the last sign of life of every source. A source that simply has nothing yet says so calmly ("Source waiting", "No data yet", "Stale") instead of showing an error. The page redraws itself every few seconds without folding what you opened.
- **Right now, About this view, live state.** Key `L` (or the pulse button) opens the *Right now* rail: what is noteworthy (one sub line each), the active conversations with their rates, the changes of the last hours and new devices. Key `I` explains the open view and links to the manual; on wide screens it is open by default. The header says "Live" with the time of the last update, "Updated 12 s ago", "Disconnected" with a Reconnect button, or "Paused" while the tab is in the background; its dialog shows the live grade of every source and the clock offset between your browser and the server. See [docs/manual/views.md](docs/manual/views.md) and the [manual](docs/manual/README.md).
- **Deep links everywhere.** Every view, component card, device, nightly report and map filter has its own address (`#/map/<id>?zone=services`), so you can bookmark or share it, and the back key returns to what you saw before. The map filters by area or shows only what is down.
- **Draws the network as a PCB.** Zones for the Internet edge, Network, Servers and services, Computers and phones, Media and cameras, Smart home, Printers and other. Hub rooms and MQTT topic groups appear when those integrations are connected.
- **Routes traces like a board.** Orthogonal and 45° segments, buses and vias. Traces that end at the same component and port share one trunk, and their dots add up on it.
- **Filters by line style.** The legend at the bottom names the line styles (cable, wireless, virtual, flow, uplink, LAN, not measured); each entry is a switch that hides or shows that kind of line on the whole board. The choice is kept per view in the browser and survives the live redraw.
- **Says where traces go.** End marks name peers outside your network where a trace leaves the board; port marks show the destination port or protocol at the end of a live trace.
- **Animates only what is measured.** Each dot stands for a fixed number of bytes ("1 dot ≈ 64 KiB"; the quantum is chosen automatically in powers of two) and bytes are conserved. Traces without measurements stay dashed.
- **Shows how fresh the data is.** Every source and component carries a live grade: realtime (cadence up to 2 s), near (up to 15 s), periodic (up to 10 min), sluggish, interrupted, or unknown.
- **Two views.** *Map* is an overview by kind; *Network* shows the gateway, the switches and access points, one zone per subnet and the guests of virtualization hosts.
- **Physical topology.** Which device hangs on which switch port, which clients use which access point (with band and network name), and which guests run on a host. Cables are solid traces, wireless links dashed with a radio mark, guests dotted; the port number sits at the device end. The data comes from a manual "connected via" field, a switch controller (view-only account), LLDP neighbours and switch address tables over SNMP, in this order of precedence. Clients behind an access point default to wireless. This host's measured traffic flows along the cabling. See [docs/TOPOLOGY.md](docs/TOPOLOGY.md).
- **Data paths.** A calm world map shows where your devices send data on the internet, by country: arcs from your home country, width by bytes, outside the EU in amber. 24 hours, 7 days or 30 days, a full-screen map, lists of countries and devices, and calm hints for unusual behaviour (a device talks to a new country, uploads far more than on a typical day, or sits in a network that should not reach the internet). Countries come from an offline database shipped in the image; only countries and byte counts are stored, never addresses. Own networks and tunnels are kept apart, well-known anycast networks (content delivery, public resolvers) get their own line instead of the country they are registered in, and the EU share only counts traffic with a known country. To see every device, not only the Lanloom host, let your router send NetFlow v5, v9 or IPFIX (with IPv6) to Lanloom's built-in receiver: off by default, only from the exporter addresses you allow. The map zooms with the mouse wheel or a pinch, up to 16 times. Every device card answers **Where does it talk to?** (countries on the internet and the devices of your network, by period), the card of the Internet tile lists who talks to the internet and how much, and the chip **Who talks to whom** draws dashed lines between devices that talk to each other, from the same flows. See [docs/DATA-PATHS.md](docs/DATA-PATHS.md).
- **Real cables**, the default view of the data paths map: **only lines, never an arc**. Every country's traffic runs over infrastructure - mapped sea cables, known cable corridors where the data has gaps, land lines along long-distance corridors, a radio link to an island close to a hub, and a satellite hop for remote destinations and satellite networks - bundled into strands that get thicker with the traffic. Measured routes (option below) are followed through the places they pass, at each sea crossing over the cable they probably used - always labelled as an estimate (*Probably via …*, *Possibly via … or …*, *Cable unknown*), never as a measurement; a latency that rules out the database country (anycast) ends the way where it was measured. A two-part switch returns to *Rain*, the arcs from the home country; the choice is remembered per device and travels in the link (`#/paths?view=rain`), the sum of the map is the same in both views, and the country detail has the same switch. The cable data ships with the app as a separate file under the Open Database License (© OpenStreetMap contributors, incomplete, community-mapped), next to a second file from open sources (cable routes in U.S. waters from national open data, cable facts such as owners, year and capacity from Wikidata, both CC0) that adds lines the community map lacks and fills the tooltips; nothing is downloaded at run time; administrators can import an Overpass answer of their own. See [docs/DATA-PATHS.md](docs/DATA-PATHS.md#real-cables-and-rain-two-views).
- **Network routes** (an option, off by default): which networks and places the data passes on the way to your most important destinations (provider, exchange point, transit, destination network) with the time each section adds, in the country detail and on the data paths map; measured rarely with one probe packet per hop, without extra container rights, estimated parts dashed, a calm note when a route changed. See [docs/DATA-PATHS.md](docs/DATA-PATHS.md#network-routes-an-option-off-by-default).
- **Nightly report.** Every morning a short report about the night in plain words: internet outages, new and missing devices, interrupted connections and refused sign-ins, data paths hints, changes to the controller configuration, expiring certificates, and Lanloom's own updates and restarts as maintenance. One headline says "All quiet" or how many things need a look; those can be ticked off. Rule-based from Lanloom's own data, with no online service needed (an AI can explain a note on request, see below); made at the next start if Lanloom was off at report time; 30-day archive; optional delivery by e-mail, webhook, push topic or your own chat bot (off by default). See [docs/NIGHTLY-REPORT.md](docs/NIGHTLY-REPORT.md).
- **Inventory and life cycle.** Beside the found facts (maker, model, firmware and where each comes from) every device carries what only you know: purpose, room, purchase date, warranty, shop and note, serial number, the responsible person. Devices are *active*, *missing* (not seen for N days), *probably gone* (with the reasons: long absent, a successor appeared, its switch port is used by another device) or *decommissioned* (date, replaced by, note; hidden from the map, kept as history). Entries Lanloom made on its own are provisional and disappear after seven days without a sign of life unless you confirm them. An offline table in the image points at devices whose updates ended or end soon; warranties that run out do the same. Filters, sorting, editing many devices at once, the phone layout and a section in the nightly report come with it. Everything stays in your database; nothing is asked online. See [docs/INVENTORY.md](docs/INVENTORY.md).
- **Export and import.** The network plan as a PDF (A4 or A3, light, dark or print-friendly, with the inventory list), as an SVG (vector, texts stay text) or as a draw.io file (boxes, zones and lines to edit); the inventory as CSV or JSON, and the own columns (names, notes, "connected via" with port) read back in after a check. A *Redact* option replaces names, addresses, hardware addresses, persons and notes by neutral ones for sharing in an issue. Everything is built locally; every download is an audit entry. See [docs/EXPORT.md](docs/EXPORT.md).
- **Vulnerability hints, offline.** Published vulnerabilities that match a device's maker, model and firmware, from a feed in the image (the public catalog of known exploited vulnerabilities and the national vulnerability database's affected-version ranges, renewed with every release, never downloaded at run time; a newer feed file can be imported). Each hint says how sure it is (*certain* or *possible*, with the reasons) and what to do (update to the fixed firmware, or disconnect/replace the device); *exploited in the wild* is marked. Strict on purpose: no hint without a known maker, an exact model and a comparable firmware, so devices that cannot be checked are listed with the reason. A section in the device card, the nightly report and a notification event come with it. No guarantee and no test of the device itself. See [docs/VULNERABILITIES.md](docs/VULNERABILITIES.md).
- **Control room.** One page for what keeps the house running: the internet with two readings (what the gateway says and what Lanloom measures once you pick targets - the measurement counts, a flap guard keeps a bad minute from becoming twenty alarms, a reserve line such as a mobile router is shown with its signal quality, bands and data volume), the storage boxes with a ring, volumes, containers and backups, the battery backup (UPS over the Network UPS Tools protocol or the UPS-MIB, with its remaining runtime), and the backup chains - *current* only when a recent success is known, *cannot be judged* when nobody reports, progress instead of a stopwatch, and a note when a restore was last checked. Everything only reads and every block says how old its picture is. See [docs/CONTROL-ROOM.md](docs/CONTROL-ROOM.md).
- **Resilience.** How well the network copes when something fails: a score with a grade from A to F and its last 90 days, the ten weak spots with the one step that would help most (seven axes per part - impact, spare, backup, power, up to date, watch, health - unknown counts as half weak, never as fine), emergency scenarios (a power cut with a slider over the battery backup's live runtime, the internet going down with the reserve line, the gateway, storage or a server off), the layers everything rests on, and a risk lens that colours the map (key `B`). Power source and spare are fields per part; everything is a simulation, nothing is switched. See [docs/RESILIENCE.md](docs/RESILIENCE.md).
- **Host health and early warnings.** The card of every host Lanloom reads (optional host agent, SNMP host tables, hypervisor, network controller, system sensors of a home hub) shows a health ring and round gauges for processor, memory, disk, temperature, swap, load per core and throttling, green, yellow or red, with a stale source dimmed instead of red. Memory, swap, disk, load and temperature are followed as a trend over 6 hours and 7 days; a value that will reach its limit within 72 hours is an early warning on the card, on the overview, in the nightly report and as a notification - a daily rhythm or a sawtooth never is. Rows only a person can end (a device switched off, a refused sign-in, a battery to replace) move to their own *Waiting on someone* card after 24 hours unchanged, so they no longer drown out new problems. See [docs/HOST-HEALTH.md](docs/HOST-HEALTH.md).
- **Availability, stability and traffic over time.** Once a minute Lanloom checks every device and connection and books the result into 5-minute slots for 35 days (a slot without a look stays *unknown*, never "down"). *Devices › Stability* lists everything, the worst first, with 35 daily bars, availability over 24 hours and 7 days, the gaps of the last week, the typical repair time and the wobbly candidates (three or more gaps in 7 days). The card of a device shows the 24 hours in 5-minute blocks, a traffic plot over 1 h, 6 h, 24 h or 7 days (full screen, exact values under the pointer), its connections by name with live rates, and *Simulate maintenance*: what would lose its way if it were switched off, only calculated. Resting on a tile of the map shows traffic and availability at a glance. The sections of a card fold and keep their state. See [docs/AVAILABILITY.md](docs/AVAILABILITY.md).
- **Failure chain and outages.** *What fails if this fails?*, *What does it depend on?* and *Simulate maintenance* for every device, container, connection, hub device, entity and automation - from the topology, the gateway, the hub's devices and the triggers, conditions and targets of its automations, plus the dependencies you enter by hand (hard, soft or none). The board frames the part, puts a stage mark on everything that hangs on it and keeps only those traces lit; a list by stages gives the reason of every part. Sure links carry the chain on, likely ones end it, so one partial failure never colours the whole home. When a device really goes down, the **outage automatic** dashes what probably hangs on it, says so on the cards and in the *Automations* tab, and the offline notification and the nightly report name how many parts depended on it. Hovering a tile shows only the traces it needs. Only calculated: nothing is switched. See [docs/manual/failure-chain.md](docs/manual/failure-chain.md).
- **Services, web interfaces and certificates.** Every device card lists what the device offers (name, port, version where the device names it itself, web interface or not). *Devices › Links* lists every web interface with the state of its probe (alive, login required, server error, not answering) and opens it in a new tab; a link that stops answering is switched off and comes back by itself. The certificate clock warns under 30, 14 and 3 days. Only known services are looked at, every 3 minutes, never a login. See [docs/SERVICES.md](docs/SERVICES.md).
- **Now and Focus on the phone.** *Now* is the phone's start: a health ring with the share of devices online, the sites, what changed in the last hours and the last 24 hours as a strip (a tap replays the map at that moment). *Focus* puts one device in the middle and its partners in a ring, each with the colour of its trace and its live light. Details open in sheets you drag up or down, the back key walks back through them, pull down to refresh, and the phone bar keeps four views plus *More*. Installed on the home screen, Lanloom opens on *Now* with a themed status bar and a symbol that fits round and square masks. See [docs/manual/now.md](docs/manual/now.md).
- **Energy with cost and CO₂.** With a home-automation hub that has power sensors, an *Energy* page draws grid, solar and battery feeding the home and the home feeding its consumers; the dots carry watts instead of bytes. Every consumer is a lane with watts, cost per hour and CO₂ per hour; the price and the CO₂ intensity come from a sensor of the hub or a number you enter, nothing is asked online. A control-room block and a nightly-report section come with it. See [docs/manual/energy.md](docs/manual/energy.md).
- **Several sites in one Lanloom.** Your home, a holiday flat, an office: each site has its own networks, devices, history and nightly report, and a switcher next to the logo (remembered per browser, on the phone too) changes between them. *All sites* shows every site at a glance. A remote site is seen only over ways that are already there - one more site of your controller, or the host agent on a machine there; Lanloom never scans across routers on its own. Administrators decide per person which sites they see, and the server never names another site to them. Reports per site or one combined report. See [docs/manual/sites.md](docs/manual/sites.md).
- **Several users, three roles.** Viewer, operator and admin, enforced on the server for every route. Invitation links, two-step sign-in with an authenticator app, recovery codes or passkeys, sessions you can end, security notices and a tamper-evident audit log. Optional sign-in through your own authentication proxy. See [docs/ACCESS.md](docs/ACCESS.md).
- **AI, local or cloud (optional).** An Explain button on devices, notes of the nightly report and firewall rules lets an AI answer in plain words, in the language of the app. **Ask your network** answers questions with links to the devices and views concerned, the nightly report gets a paragraph *In plain words*, the AI suggests what an unknown device is (you confirm), and the firewall editor shows suggestions you can take over as a draft. Off by default; AI only reads and never changes a setting. See [docs/integrations/ai.md](docs/integrations/ai.md).
- **Component card.** Classification with reasons, open ports, announcements, a chart of recent traffic (and its history), availability, connections with live rates, the physical connection (where it is plugged in, which source says so, what other sources say, the devices on its ports), and options to rename, re-type, hide, forget or set "connected via" by hand. Hidden devices keep their card, and a device behind a wireless repeater that rewrites its address is offered as *Same device* (or drawn as one device when IP address and name both fit). A device nothing could classify takes the class of the hub device that names its address.
- **Search and commands.** One box for everything: press `Ctrl K` (`⌘ K` on a Mac) or `/`, or tap the Search button in the top bar. It finds devices, virtual machines and containers, services, automations and entities of a home-automation hub, and it runs commands: every view, the settings pages, light/dark, colour theme, rate unit, "Check for updates". Typos are forgiven, the last choices come first, a hit has a menu of actions (open, copy the address), and a comparison such as `> 25 °C` lists hub values that fit. Read only, full screen on a phone, usable with a keyboard and a screen reader. See [docs/PALETTE.md](docs/PALETTE.md).
- **Device list** with search, light/dark/auto mode and four colour themes (Circuit green, Aurora and the glass pair SummerGlass and WinterGlass) plus your own imported theme, a phone layout with bottom tabs, a UI in English, German, French, Spanish, Dutch and Italian.
- **Firewall editor.** A Firewall tab shows every zone, network and device. In Change mode you drag a device to another network, or tap it (on a phone too) to move it, rename it or give it a fixed address; the changes collect in a draft and you get a plain-language preview: what the device may do afterwards, what it may no longer do, every field old → new, and what to watch out for. Addresses are checked before anything is written (inside the network, not the gateway, not taken). Gateway changes go the same way: create a network with a VLAN of its own from a template, change an address service, reserve or release fixed addresses, add or switch off port forwards (each new one confirmed separately); the preview names hub integrations, presence trackers and automations that reach a device by its address. The **zone firewall** can be rehearsed and changed the same way: create, rename and remove own zones, move networks between zones, switch, change, reorder, add and remove rules, and ask *May one reach the other?* now and after your draft, with lockout protection before anything is written. **Switch ports** show every switch as its faceplate; in Change mode a port gets a name, a port profile, an untagged network, tagged networks (none, all or a choice) and power over Ethernet on or off, or a power cycle that restarts the device behind it; uplinks, network equipment and protected devices keep their networks and their power, and every change of power needs its own confirmation. A **network plan** keeps the target network of every device (for example a new IoT network), marks each tile *→ IoT*, counts *5 of 12 moved* and drafts the moves that are possible now. Out of the box it runs as a **simulation** against a sample network and writes nothing anywhere; with the optional executor and a controller family that can write, the same draft becomes a signed change with backup, watch and automatic rollback. See [docs/FIREWALL-EDITOR.md](docs/FIREWALL-EDITOR.md).
- **Firewall board, rules in plain words, "May one reach the other?".** For a zone firewall that Lanloom reads, the Firewall tab shows the zones as a board with the traffic between them (hits of the last hour, what was blocked today), every rule as a sentence, an answer to "May this device reach that one on this port?" from the rules in their order (and an honest *Depends* where a rule needs something Lanloom cannot see), and checks that come with a suggestion: a rule an earlier rule always overrides, a guest zone that reaches the inside, an intrusion-detection suppression list that has grown long. Read only. See [docs/manual/firewall.md](docs/manual/firewall.md).
- **Switch front panels.** With a switch controller read, every switch shows its ports like the hardware: link, speed, power over Ethernet, uplink, VLAN, the devices behind each port and how often the link dropped or counted errors in 24 hours (a symbol and a word, not only a colour), and the port profile, the tagged networks and whether power is switched off; access points show their radios, clients, roaming and the signal of their wireless devices (good / fair / weak per band, and devices clinging to a far access point, kept in memory only). Where one device sits alone on a port, its cable is measured exactly from the port counters. A port that turns unstable is a notification event and a section of the nightly report. See [docs/manual/network.md](docs/manual/network.md).
- **A calmer network view.** Large networks fold their quiet devices into one tile, the devices list carries the network and wireless network of each device with a filter, one mode switch (Live, Plan, Firewall, Data paths) connects the views, the legend holds line styles and networks in one strip, and the network opens full screen on a phone.
- **Your controller's configuration, read only.** With a device-family adapter (see [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)) the Firewall tab shows the real zones, networks and clients, the firewall rules in the controller's order with hit counters and a zone matrix, port forwarding, wireless networks, and the network equipment with every switch port. The same adapter feeds the physical topology. It uses a read-only account or key and cannot change anything.
- **Your firewall, read only.** A zone firewall adapter shows the firewall's zones as a matrix, every rule in its order with addresses, services and schedule, port forwarding, VPN connections, static routes and fixed addresses, with plain checks such as management open on the internet side. The nightly report tells which rules were added, removed or now act differently. The adapter can only send queries that read.
- **Your gateway firewall with rules on interfaces.** A gateway firewall adapter for firewalls whose rules sit on interfaces turns every interface into a zone and shows the zone matrix, every rule in the order the firewall evaluates it (floating, group and interface rules, classic rules marked), port forwarding, source NAT, VPN instances, gateways and fixed addresses, with plain checks such as management open on the internet side and a gateway that is down, and a night report section. It reads with `GET` requests only. With the optional executor and a key of its own it can add rules between interfaces, change and switch simple rules and remove its own - written as one apply, with Lanloom's backup and, on firewall releases that have it, the firewall's own revert timer that puts the rules back unless the executor confirms. See [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md).
- **Your router, read and (optionally) changed.** An adapter for routers with a ubus API reads the firewall zones, rules in configuration order, port forwards, fixed addresses, wireless networks with their security and the clients on air, with plain checks such as management open on the internet side or an open wireless network, and a night report section. It reads through a login whose access list only allows reading and refuses one that allows changes. With the optional executor and a login of its own it can add, change, switch and remove firewall rules - staged first, put live with one apply, with Lanloom's backup and the router's own revert timer that puts the rules back unless the executor confirms. The demo includes a *Sample router*. See [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md).
- **Your hypervisor.** A hypervisor adapter puts every virtual machine and container below the node it runs on, and shows on the component cards what runs where, with which bridge and VLAN, how full the storage is and when each guest was last backed up. Findings point at guests without a recent backup, storage that is almost full and VLANs that cannot leave the node. It reads with a token that can only read; a token that could change anything is refused. With the optional executor and a token of its own, the Firewall tab can also start or cleanly shut down a guest, move a network card to another bridge or VLAN and adjust the guest firewall - as a signed change with backup and automatic rollback. Guests that keep the network running (Lanloom itself, the name server, the gateway, a hub or controller) are refused.
- **Your home router and its mesh, read only.** A home-router adapter shows which devices the router knows (wired or wireless, active now), where each one hangs in the mesh - on the router or on which repeater, on which LAN port, on which radio band - the router and its mesh devices with model, firmware and state, the home network with its DHCP range and the state of the internet connection, with plain checks and a night report section. It reaches the router through its documented local interface with an account made only for Lanloom and can only send calls that read.
- **Your switching router, read only.** A router adapter shows the router and the network equipment next to it, every port with link, speed, power over Ethernet, untagged and tagged VLANs, the networks with their address range and DHCP range, the clients (wired with their port, wireless with network and band), port forwarding, the internet connections and the filter rules in the router's own order - with plain checks and a night report section. It reaches the router through its documented local interface with a user whose group only reads, refuses a user that can change anything where that is checkable, and can only send requests that read.
- **Your access points and switches, read only.** An access-point controller adapter shows the site's gateway, switches and access points with their state and uplinks, the clients behind every port and wireless network, the switch ports (link, speed, power over Ethernet, port profile, the device behind the port), networks and VLANs, wireless networks with their security, and the access rules, with plain checks (a device that is not connected, an internet connection that is down, a wireless network without encryption) and a night report section. It uses an application made only for Lanloom, takes a short-lived token and can only read.
- **Your DNS filter, read only.** A DNS filter adapter shows what the name server that blocks trackers and advertising does: the blocked share of the last 24 hours, queries per hour and day, the most blocked names, the busiest clients matched to your devices, the upstream resolvers and the block lists, with plain findings and a night report section. It reads totals only - never single queries, which device asked for which name - and keeps two numbers per hour and day. See [docs/DNS-FILTER.md](docs/DNS-FILTER.md).
- **Read-only by construction.** The map observes; the app cannot change your devices. Real changes are only possible through the separate, optional executor service, with a passkey signature per change and an explicit arming step by the operator; the executor can connect only to the app and to its controller.

## Tour

A short clip of the sample network - the map with its moving dots, the data paths and the nightly report:

![The sample network: map, data paths and nightly report (animated, 4 MB)](docs/site/media/demo.gif)

Every view in light and dark and in the colour themes *Circuit* and *Aurora*, plus the phone layout, is in the **[tour](docs/TOUR.md)** (the glass themes are shown [below](#colour-themes)); the same tour, with a viewer, is the static [landing page](docs/site/README.md) in `docs/site/`. A picture here opens its section of the tour.

<p><a href="docs/TOUR.md#overview"><img src="docs/site/img/overview-light-circuit.png" width="640" alt="Overview, light, Circuit"></a><br><sub>Overview</sub></p>

<p><a href="docs/TOUR.md#map"><img src="docs/site/img/map-light-circuit.png" width="640" alt="Map, light, Circuit"></a><br><sub>Map</sub></p>

<details>
<summary>12 more pictures</summary>

<p><a href="docs/TOUR.md#network"><img src="docs/site/img/network-light-circuit.png" width="640" alt="Network, light, Circuit"></a><br><sub>Network</sub></p>

<p><a href="docs/TOUR.md#devices"><img src="docs/site/img/devices-light-circuit.png" width="640" alt="Devices, light, Circuit"></a><br><sub>Devices</sub></p>

<p><a href="docs/TOUR.md#data-paths"><img src="docs/site/img/data-paths-light-circuit.png" width="640" alt="Data paths, light, Circuit"></a><br><sub>Data paths</sub></p>

<p><a href="docs/TOUR.md#nightly-report"><img src="docs/site/img/nightly-reports-light-circuit.png" width="640" alt="Nightly report, light, Circuit"></a><br><sub>Nightly report</sub></p>

<p><a href="docs/TOUR.md#control-room"><img src="docs/site/img/control-room-light-circuit.png" width="640" alt="Control room, light, Circuit"></a><br><sub>Control room</sub></p>

<p><a href="docs/TOUR.md#firewall"><img src="docs/site/img/firewall-light-circuit.png" width="640" alt="Firewall, light, Circuit"></a><br><sub>Firewall</sub></p>

<p><a href="docs/TOUR.md#automations"><img src="docs/site/img/automations-light-circuit.png" width="640" alt="Automations, light, Circuit"></a><br><sub>Automations</sub></p>

<p><a href="docs/TOUR.md#inventory"><img src="docs/site/img/inventory-light-circuit.png" width="640" alt="Inventory, light, Circuit"></a><br><sub>Inventory</sub></p>

<p><a href="docs/TOUR.md#energy"><img src="docs/site/img/energy-light-circuit.png" width="640" alt="Energy, light, Circuit"></a><br><sub>Energy</sub></p>

<p><a href="docs/TOUR.md#vulnerabilities"><img src="docs/site/img/vulnerabilities-light-circuit.png" width="640" alt="Vulnerabilities, light, Circuit"></a><br><sub>Vulnerabilities</sub></p>

<p><a href="docs/TOUR.md#rewind-and-incident-files"><img src="docs/site/img/rewind-light-circuit.png" width="640" alt="Rewind, light, Circuit"></a><br><sub>Rewind</sub></p>

<p><a href="docs/TOUR.md#settings"><img src="docs/site/img/settings-light-circuit.png" width="640" alt="Settings, light, Circuit"></a><br><sub>Settings</sub></p>

</details>


## Screenshots

All screenshots show the built-in demo network with invented names and addresses.

### Setup wizard

<p><img src="docs/screenshots/wizard-1-account.png" width="640" alt="Wizard step 1: account"><br><sub>1. Create the account with the one-time setup code</sub></p>

<p><img src="docs/screenshots/wizard-2-networks.png" width="640" alt="Wizard step 2: networks"><br><sub>2. Detected networks: gateway, DNS, DHCP and the proposed ranges</sub></p>

<details>
<summary>5 more pictures</summary>

<p><img src="docs/screenshots/wizard-3-scan.png" width="640" alt="Wizard step 3: scan"><br><sub>3. Scan options: gentle or deep</sub></p>

<p><img src="docs/screenshots/wizard-4-progress.png" width="640" alt="Wizard scan progress"><br><sub>First picture while the scan continues</sub></p>

<p><img src="docs/screenshots/wizard-5-review.png" width="640" alt="Wizard step 4: review"><br><sub>4. Review the automatic classification</sub></p>

<p><img src="docs/screenshots/wizard-6-connect.png" width="640" alt="Wizard step 5: connect"><br><sub>5. Suggested adapters with a read-only guide</sub></p>

<p><img src="docs/screenshots/wizard-7-done.png" width="640" alt="Wizard step 6: done"><br><sub>6. Done: time to the first picture, rescan interval, update check</sub></p>

</details>


### Overview

![Overview start page, light theme](docs/screenshots/overview-light.png)

<p><img src="docs/screenshots/overview-dark.png" width="640" alt="Overview start page in dark theme"><br><sub>Health with the last 24 hours, key figures, noteworthy issues, new devices</sub></p>

<p><img src="docs/screenshots/overview-rail-dark.png" width="640" alt="The Right now rail beside the overview"><br><sub>Key L: noteworthy issues, active conversations, changes, new devices</sub></p>


### The map

![Map, light theme](docs/screenshots/map-light.png)

![Map filtered to one area](docs/screenshots/map-filter-light.png)

![Network view, dark theme](docs/screenshots/network-dark.png)

![Network view, light theme](docs/screenshots/network-light.png)

### The network as a tree

The Network view has three faces: the board, a **3D** stack of boards (below) and a **tree** that folds, Internet, gateway, switches and access points, devices and guests, with the link (cable and port, wireless band, virtual), the live traffic as a number and the state as a word. It is also the text alternative of the picture. Everything can be done with the keyboard (`?` shows the keys), the spoken status for screen readers is throttled, and an automated check allows no serious accessibility finding before a release ([Accessibility](docs/ACCESSIBILITY.md)).

<p><img src="docs/screenshots/network-tree-light.png" width="640" alt="Network tree with the card of an access point open"><br><sub>The chain as a tree; the chain of the line under the pointer is lit</sub></p>

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/network-tree-phone-dark.png" width="150" alt="Network tree on a phone"><br><sub>On a phone the lines wrap</sub></td><td width="50%" align="center" valign="top"><img src="docs/screenshots/keyboard-help-light.png" width="150" alt="Keyboard help"><br><sub>Key <code>?</code></sub></td></tr>
</table>


### The network in 3D

The same chain as a stack of circuit boards that you can turn, tilt and zoom: Internet on top, then the gateway, switches and access points, devices at the bottom; copper traces with vias between the boards and the live traffic running along them as dots. It needs graphics acceleration; without it the browser gets a note and the flat board ([Network manual](docs/manual/network.md#the-network-in-3d)).

<p><img src="docs/screenshots/network-3d-light.png" width="640" alt="The network as a stack of circuit boards in 3D"><br><sub>One board per step of the chain; the element in focus is named in the corner</sub></p>

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/network-3d-phone-dark.png" width="150" alt="The 3D view on a phone"><br><sub>One finger turns, two zoom</sub></td><td width="50%"></td></tr>
</table>


### Cabling

<p><img src="docs/screenshots/connection-switch-light.png" width="640" alt="Switch card with the devices on its ports"><br><sub>A switch: where it is plugged in and what hangs on each port</sub></p>

<p><img src="docs/screenshots/connection-conflict-dark.png" width="640" alt="Device card with a manual entry that overrides the controller"><br><sub>A manual entry wins; the card says what the other source reports</sub></p>


### Component card

![Component card](docs/screenshots/detail-light.png)

![Room detail](docs/screenshots/area-detail-dark.png)

### Live grade

![Live grade](docs/screenshots/live-grade.png)

### Firewall editor

![Firewall editor in change mode with a draft of three moves](docs/screenshots/firewall-draft-light.png)

<p><img src="docs/screenshots/firewall-preview-light.png" width="640" alt="Plain-language preview"><br><sub>Preview: what changes, what it may and may no longer do, what to watch out for</sub></p>

<p><img src="docs/screenshots/firewall-progress-dark.png" width="640" alt="Change in progress"><br><sub>Backed up, changed, watched; undo for 24 hours</sub></p>

<details>
<summary>6 more pictures</summary>

<p><img src="docs/screenshots/firewall-device-light.png" width="640" alt="Device sheet: network, name and fixed address"><br><sub>Tap a device: move it, rename it, give it a fixed address</sub></p>

<p><img src="docs/screenshots/firewall-edits-dark.png" width="640" alt="Preview of a rename and a new fixed address"><br><sub>Every field old → new; the address is checked before anything is written</sub></p>

<p><img src="docs/screenshots/netplan-light.png" width="640" alt="Network plan: target network per device and progress"><br><sub>Network plan: target per device, progress, draft the moves</sub></p>

<p><img src="docs/screenshots/gateway-preview-dark.png" width="640" alt="Preview of a new network, a port forward and a released fixed address"><br><sub>New network, port forward, fixed address; what uses a device by address</sub></p>

<p><img src="docs/screenshots/switch-ports-editor-light.png" width="640" alt="Switch ports card: the faceplate of a sample switch with one port selected and the button Change port"><br><sub>Switch ports: the faceplate of a switch, a port selected, Change port</sub></p>

<p><img src="docs/screenshots/switch-port-preview-dark.png" width="640" alt="Preview of a power cycle, a port switched off and a network change with warnings and confirmation checkboxes"><br><sub>Preview of port changes: devices that restart, switch off or change network</sub></p>

</details>


### Controller configuration (read only)

![Firewall tab with a live controller configuration: zones, networks and clients](docs/screenshots/firewall-live-light.png)

<p><img src="docs/screenshots/firewall-rules-dark.png" width="640" alt="Zone matrix and firewall rules with hit counters"><br><sub>Zone matrix and rules in the controller's order, with hit counters</sub></p>

<p><img src="docs/screenshots/firewall-equipment-light.png" width="640" alt="Network equipment with switch ports, wireless networks and port forwarding"><br><sub>Equipment with every port, wireless networks, port forwarding</sub></p>


### Firewall board and switch front panels (read only)

![The firewall board with blocked today, own rules, checks and the traffic between zones](docs/screenshots/firewall-board-light.png)

<p><img src="docs/screenshots/firewall-ask-dark.png" width="640" alt="May one reach the other? The answer names the deciding rule"><br><sub>The answer from the rules in their order, with the rules that cannot be judged</sub></p>

<p><img src="docs/screenshots/switch-front-tagged-light.png" width="640" alt="Switch front panel with the details of a port: untagged network, tagged networks and power switched off"><br><sub>Front panels with link, power, VLAN, profile, tagged networks, the devices behind a port and the last 24 hours</sub></p>

<details>
<summary>2 more pictures</summary>

<p><img src="docs/screenshots/firewall-checks-light.png" width="640" alt="Checks with a suggestion"><br><sub>Checks come with what you can do about them</sub></p>

<p><img src="docs/screenshots/network-legend-light.png" width="640" alt="Network view with the mode switch and the legend of lines and networks"><br><sub>Mode switch, and a legend with lines and networks in one strip</sub></p>

</details>


<img src="docs/screenshots/phone-firewall-board.png" width="200" alt="Phone: the firewall board"> <img src="docs/screenshots/phone-switch-front.png" width="200" alt="Phone: a switch front panel"> <img src="docs/screenshots/phone-network-full.png" width="200" alt="Phone: the network in full screen">

### Firewall (read only)

![Firewall tab with a zone firewall source: zone cards and the zone matrix derived from the rules](docs/screenshots/zonefw-matrix-light.png)

<p><img src="docs/screenshots/zonefw-checks-dark.png" width="640" alt="Checks of the read firewall configuration, port forwarding, internet connections, VPN and other objects"><br><sub>Checks, port forwarding, VPN and other objects</sub></p>

<p><img src="docs/screenshots/zonefw-report-light.png" width="640" alt="Nightly report section of the firewall: new rule, new port forwarding, checks"><br><sub>Nightly report: what changed on the firewall overnight</sub></p>


<img src="docs/screenshots/zonefw-phone.png" width="260" alt="Firewall checks on a phone">

### Gateway firewall (rules on interfaces)

![Firewall tab with a gateway firewall source: interfaces as zones and the zone matrix from the rules in evaluation order](docs/screenshots/gwfw-matrix-light.png)

<p><img src="docs/screenshots/gwfw-rules-dark.png" width="640" alt="Rules of the gateway firewall as sentences, with checks, port forwarding and VPN instances"><br><sub>Rules as sentences, checks, forwarding and VPN</sub></p>

<p><img src="docs/screenshots/gwfw-report-light.png" width="640" alt="Nightly report section of the gateway firewall: a new rule from the internet side, a rule switched off, checks"><br><sub>Nightly report: what changed on the gateway overnight</sub></p>


<img src="docs/screenshots/gwfw-phone.png" width="260" alt="Gateway firewall checks on a phone">

### Router with a ubus API

![Firewall tab with a router source: firewall zones, the zone matrix and the rules in configuration order](docs/screenshots/router-rules-light.png)

<p><img src="docs/screenshots/router-wireless-dark.png" width="640" alt="The router as network equipment, its wireless networks with security and state, and port forwarding"><br><sub>Wireless networks, the router and port forwarding</sub></p>

<p><img src="docs/screenshots/router-report-light.png" width="640" alt="Nightly report section of the router: management open on the internet side, a new rule from there, a rule switched off"><br><sub>Nightly report: what changed on the router overnight</sub></p>


<img src="docs/screenshots/router-phone.png" width="260" alt="Router checks on a phone">

### Access points and switches (read only)

<p><img src="docs/screenshots/apctl-equipment-light.png" width="640" alt="Network equipment of an access-point controller: gateway, switch with its front panel and uplink, access points with their state, wireless networks"><br><sub>Equipment with the switch front panel, access points and wireless networks</sub></p>

<p><img src="docs/screenshots/apctl-report-dark.png" width="640" alt="Nightly report section of the access-point controller: an access point went offline, a new access rule, a firmware change, checks"><br><sub>Nightly report: what changed on the controller overnight</sub></p>


<img src="docs/screenshots/apctl-phone.png" width="260" alt="Equipment of an access-point controller on a phone">

### Home router and mesh (read only)

<p><img src="docs/screenshots/homerouter-equipment-light.png" width="640" alt="Network equipment of a home router: the router and a repeater with their state and uplink"><br><sub>The router and its mesh devices with model, firmware, address and uplink.</sub></p>

<p><img src="docs/screenshots/homerouter-report-dark.png" width="640" alt="Nightly report section of the home router: a repeater got a new firmware, a device the router had not known"><br><sub>Nightly report: what changed on the home router since the last report.</sub></p>


<img src="docs/screenshots/homerouter-phone.png" width="260" alt="Equipment of a home router on a phone">

### Switching router (read only)

<p><img src="docs/screenshots/router-switch-equipment-light.png" width="640" alt="Network equipment of a switching router: the router with its four ports and a neighbouring switch, and the port forwarding"><br><sub>The router with its ports (link, speed, power over Ethernet, uplink), the neighbouring switch and the port forwarding.</sub></p>

<p><img src="docs/screenshots/router-switch-report-dark.png" width="640" alt="Nightly report section of the switching router: a new filter rule, a newer release, a device the router had not known"><br><sub>Nightly report: what changed on the router since the last report.</sub></p>


<img src="docs/screenshots/router-switch-phone.png" width="260" alt="Equipment of a switching router on a phone">

### Hypervisor

![Component card of a hypervisor node: load, findings, guests with their newest backup, storage](docs/screenshots/hypervisor-node-dark.png)

<p><img src="docs/screenshots/hypervisor-guest-light.png" width="640" alt="Component card of a guest: node, network cards, backup, traffic of the last hour, guest firewall"><br><sub>A guest: node, network cards, backup, traffic, its firewall</sub></p>

<p><img src="docs/screenshots/hypervisor-firewall-light.png" width="640" alt="Firewall tab with the hypervisor source: findings and firewall at datacenter, node and guest level"><br><sub>Firewall tab: findings, firewall at every level, guest network cards</sub></p>

<details>
<summary>2 more pictures</summary>

<p><img src="docs/screenshots/hypervisor-guests-light.png" width="640" alt="Guests card in change mode: start, network cards, firewall rules; guests that keep the network running carry a lock"><br><sub>Change guests: start or shut down, network cards, firewall; critical guests stay as they are</sub></p>

<p><img src="docs/screenshots/hypervisor-guest-preview-dark.png" width="640" alt="Preview of guest changes: new rule, VLAN of a network card, start with its own confirmation"><br><sub>Preview: every field old → new; a start or shutdown needs its own tick</sub></p>

</details>


### Data paths

![Data paths: Real cables view - every country's traffic runs over infrastructure (sea cables, land lines, a satellite hop to a remote island), bundled strands that get thicker with the traffic, key figures and the period switch](docs/screenshots/paths-cables-light.png)
<sub>Real cables view of the sample network: only lines, never an arc. Sea cables: © OpenStreetMap contributors (ODbL), incomplete, community-mapped, with cable routes of U.S. waters and cable facts from open data (CC0); gaps and land paths are estimated, and which line a way takes is an estimate unless measured routes back it.</sub>

<p><img src="docs/screenshots/paths-cables-dark.png" width="640" alt="Real cables view in the dark theme"><br><sub>Real cables, dark theme: measured ways full, estimated ones lighter, a satellite from the earth station to a remote destination. Sea cables: © OpenStreetMap contributors (ODbL), incomplete, community-mapped</sub></p>

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-paths-cables.png" width="150" alt="Phone: Real cables view"><br><sub>On a phone the switch spans the width</sub></td><td width="50%"></td></tr>
</table>


![Data paths: Rain view - world map with arcs from the home country](docs/screenshots/paths-light.png)

Country detail: click a country for the map zoomed on it, bubbles that add up to the figure at the top (traffic without a known city as a dashed, approximate bubble), history, cities, network operators, devices and destinations; cities and operators are an option that is off by default.

<p><img src="docs/screenshots/paths-country-light.png" width="640" alt="Country detail: map with bubbles, key figures, history, cities and network operators"><br><sub>Country detail (sample network)</sub></p>

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-paths-country.png" width="150" alt="Phone: country detail"><br><sub>On a phone it fills the screen</sub></td><td width="50%"></td></tr>
</table>


Who talks to whom: dashed lines between devices that talk to each other, and the card of a device with *Where does it talk to?*

<p><img src="docs/screenshots/map-talk-light.png" width="640" alt="Map with dashed lines between devices that talk to each other"><br><sub>The chip Who talks to whom draws the conversations of the last 24 hours</sub></p>

<p><img src="docs/screenshots/talk-card-light.png" width="640" alt="Card of a device with countries and devices it talks to"><br><sub>Where does it talk to? Countries and devices, by period</sub></p>

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-talk-card.png" width="150" alt="Phone: card with Where does it talk to?"><br><sub>On a phone the card is a sheet</sub></td><td width="50%"></td></tr>
</table>

<details>
<summary>1 more picture</summary>

<p><img src="docs/screenshots/talk-internet-dark.png" width="640" alt="Card of the Internet tile with sums per device"><br><sub>The Internet tile: who talks to the internet and how much</sub></p>

</details>


<p><img src="docs/screenshots/paths-full-dark.png" width="640" alt="World map in full screen"><br><sub>Full screen with period switch and legend; Esc or back closes it</sub></p>

<p><img src="docs/screenshots/paths-lists-light.png" width="640" alt="Unusual behaviour, countries and devices"><br><sub>Calm hints, countries and devices; a click selects</sub></p>

<details>
<summary>2 more pictures</summary>

<p><img src="docs/screenshots/paths-zoom-dark.png" width="640" alt="Zoomed world map around the home country"><br><sub>Zooming with the mouse wheel or a pinch: nearby countries get their own labels</sub></p>

<p><img src="docs/screenshots/netflow-settings-light.png" width="640" alt="NetFlow / IPFIX receiver settings"><br><sub>NetFlow / IPFIX from the router: allowed exporters, port, receiver state and one block per exporter</sub></p>

</details>


Country data: [IP Geolocation by DB-IP](https://db-ip.com) (CC BY 4.0). Base map: Natural Earth (public domain).

### Hosts and containers (optional agent)

![Running: hosts with their containers, services, timers and load](docs/screenshots/running-dark.png)

An optional host agent, started from the same signed image on another host, shows what runs there: containers (folded into their host on the map), listening services, timers and failed services, load, memory and disks, and who talks to whom as dashed lines. It reads metadata only - never contents, environment variables or command lines - opens no port, takes no commands and pairs with a one-time code from *Settings > Agents*. A container source reads the containers of a host through a read-only socket proxy instead. See [docs/HOST-AGENT.md](docs/HOST-AGENT.md) and the [Running](docs/manual/running.md) page of the manual.

### Control room

![The control room: internet, storage boxes, battery backup and backups](docs/screenshots/controlroom-light.png)

The internet with two readings and a reserve line, the storage boxes, the battery backup and the backup chains on one page; a block that went quiet is dimmed and counts as unknown, never as fine. See [docs/CONTROL-ROOM.md](docs/CONTROL-ROOM.md).

### Energy

![Energy: sources, home and consumers, the dots carry watts](docs/screenshots/energy-light.png)

Grid, solar and battery feed the home, the home feeds its consumers; one dot for every 150 W. Every consumer with its own power sensor is a lane with watts, cost per hour and CO₂ per hour. Only with a hub that has power sensors. See [docs/manual/energy.md](docs/manual/energy.md).

### Resilience

![Resilience: score, history and the ten weak spots with what helps most](docs/screenshots/resilience-light.png)

![The risk lens on the Network view](docs/screenshots/resilience-lens-light.png)

The score and its grade, the weak spots with six axis squares and a recommendation, emergency scenarios with a power-cut slider, and the risk lens on the map. See [docs/RESILIENCE.md](docs/RESILIENCE.md).

### Host health and early warnings

![Host health: ring, round gauges and a disk that will be full in about two days](docs/screenshots/hosthealth-card-light.png)

Every host Lanloom reads gets a health ring, round gauges and trends; a value that reaches its limit within 72 hours is an early warning on the card, the overview, the nightly report and as a notification. Rows only a person can end move to *Waiting on someone* after 24 hours. See [docs/HOST-HEALTH.md](docs/HOST-HEALTH.md).

### Nightly report

![Nightly report: headline, sections with points to tick off, and the archive](docs/screenshots/reports-light.png)

<p><img src="docs/screenshots/reports-dark.png" width="640" alt="Nightly report in the dark theme"><br><sub>Things that need a look can be ticked off; notes stay calm</sub></p>

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/report-settings-light.png" width="150" alt="Report time, time zone, archive and optional delivery"><br><sub>Report time, archive and optional delivery (off by default)</sub></td><td width="50%"></td></tr>
</table>


### Notifications

![Settings, Notifications: rules per event with when, from which severity and on which channel](docs/screenshots/notify-rules-light.png)

<p><img src="docs/screenshots/notify-maintenance-light.png" width="640" alt="Planned maintenance: one-off or weekly, for all or some devices"><br><sub>A maintenance window pauses notifications and mark outages as maintenance</sub></p>

<p><img src="docs/screenshots/notify-history-dark.png" width="640" alt="History of what the rules did with each event"><br><sub>History: sent, bundled, quiet hours, maintenance, flap, cool-down</sub></p>


Calm by default: every event goes into the nightly report. Each person chooses per event *immediately*, *bundled every hour* or *nightly report only*, from which severity on, and on which channel – web push on their own devices (with VAPID, per device, removable) or the shared e-mail, webhook, push topic and chat bot channels. Quiet hours let only critical immediate events through; planned maintenance (one-off or weekly, for all or some devices) pauses everything; flap protection, a 12-hour cool-down, an all-clear only after an announced outage and a daily limit keep it quiet. An optional heartbeat calls an address of yours so an outside service notices when Lanloom itself is down. Details: [docs/NOTIFICATIONS.md](docs/NOTIFICATIONS.md).

### Several sites

![All sites: every site at a glance with its state, devices, internet and how it is seen](docs/screenshots/sites-dark.png)

<p><img src="docs/screenshots/site-switch-light.png" width="640" alt="The site switcher next to the logo"><br><sub>The switcher next to the logo: every site released for you and All sites</sub></p>

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/sites-phone-light.png" width="150" alt="All sites on a phone"><br><sub>All sites on a phone</sub></td><td width="50%"></td></tr>
</table>


### Users and sign-in security

![Settings, Users: roles, second factor and last sign-in](docs/screenshots/access-users-light.png)

<p><img src="docs/screenshots/access-audit-dark.png" width="640" alt="Audit log with filters and chain check"><br><sub>Audit log: who did what, when and from where, with a checkable chain</sub></p>

<p><img src="docs/screenshots/access-second-step-light.png" width="640" alt="Second step of the sign-in"><br><sub>Second step: app code, recovery code or passkey</sub></p>


### Home-automation bridge

The card of the hub shows what the bridge reads, which private data stays out (people, trackers, zones, cameras, calendars and notifications by default), how it spares the hub (no template rendering, budgets per minute, paused lookups after errors) and the busiest automations; a runaway automation and scripts that call each other in a loop are marked and go into the nightly report. In admin mode operators can see the last runs of an automation. See [docs/HUB-BRIDGE.md](docs/HUB-BRIDGE.md).

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/hub-bridge-light.png" width="150" alt="The bridge section on the hub's card: mode, privacy, sparing the hub, busiest automations"><br><sub>What is read, what stays out, how the hub is spared</sub></td><td width="50%" align="center" valign="top"><img src="docs/screenshots/hub-runs-dark.png" width="150" alt="Last runs of an automation, a runaway automation and a script loop"><br><sub>Runaway automation, last runs, a script loop</sub></td></tr>
</table>


### Add-on and sensors in the hub

Lanloom can run as an add-on of a home-automation hub: opened from the hub's side panel, signed in with the hub account, updated by its add-on system. The optional MQTT output publishes a few figures (devices online, new devices, internet line, open issues, health, vulnerability hints, nightly report) as sensors through MQTT discovery - counts only, never names or addresses. See [Add-on and MQTT output](#add-on-and-mqtt-output).

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/mqtt-output-light.png" width="150" alt="Settings, MQTT output: broker, state of the connection, the sensors with their current values"><br><sub>MQTT output: the sensors and what they carry right now</sub></td><td width="50%"></td></tr>
</table>

<p><img src="docs/screenshots/addon-updates-dark.png" width="640" alt="Updates card in the add-on: updates come through the add-on system"><br><sub>In the add-on, updates come from the add-on system</sub></p>


### Search and commands

Press `Ctrl K`, `⌘ K` or `/` anywhere, or use the Search button. Views and commands are matched in the page; devices, services, automations and hub entities are searched on the server. A comparison such as `> 20 °C` filters hub values, and the arrow to the right (or the dots on a phone) opens the actions of a hit.

<p><img src="docs/screenshots/palette-search-light.png" width="640" alt="The search box with hits grouped into devices, services, automations and hub entities"><br><sub>Everything in one box, grouped by what it is</sub></p>

<p><img src="docs/screenshots/palette-values-dark.png" width="640" alt="The value filter listing hub sensors above 20 degrees"><br><sub>A comparison filters hub values: highest first</sub></p>

<details>
<summary>5 more pictures</summary>

<p><img src="docs/screenshots/palette-actions-light.png" width="640" alt="The actions of a device hit: open and copy address"><br><sub>The actions of a hit; only what exists is offered</sub></p>

<p><img src="docs/screenshots/radio-networks-dark.png" width="640" alt="The map with one tile per radio network on dashed lanes from the hub"><br><sub>Radio networks as lanes from the hub</sub></p>

<p><img src="docs/screenshots/hub-merged-light.png" width="640" alt="Card of a device with the hub side: evidence of the match, room, firmware, entities"><br><sub>One component: the hub side on the card</sub></p>

<p><img src="docs/screenshots/onsite-light.png" width="640" alt="On site: batteries to change now or soon and silent radio devices"><br><sub>On site: batteries and silent radio devices</sub></p>

<p><img src="docs/screenshots/sensor-page-dark.png" width="640" alt="Sensor page with a minimum-to-maximum band and the mean over 7 days"><br><sub>Sensor page: 7 days from the hub's statistics</sub></p>

</details>


### Automations in plain words

The Automations tab explains every automation and script of the hub in plain words (When → Only if → Then, written by fixed rules, without AI, in every language of the app), shows its flow with the live state of every entity and checks it: missing or unavailable entities, runaways, two automations switching one entity in opposite directions, automations that start themselves or drop starts, and those that have not run for 30 days. The dependency board shows what sets an entity and what reads it, with a dot travelling whenever an automation runs; *Who switches what* names the cause of every change (an automation, a person - never who - or the device itself) and, from the runs the hub stored, the chain that led to it (sensor → automation → script → device) and the mode conflicts (dropped starts, restarts, full queues), the timeline covers the last 24 hours, the day plan puts every fixed time and sun trigger on today's axis with a now line, the rooms view lists devices with their live state, and *Hub health* bundles integrations with errors and unavailable entities - read only, nothing is ever healed or switched. See [docs/AUTOMATIONS.md](docs/AUTOMATIONS.md).

<p><img src="docs/screenshots/automations-light.png" width="640" alt="The list of automations with tiles and findings"><br><sub>Every automation with its findings</sub></p>

<p><img src="docs/screenshots/automations-item-dark.png" width="640" alt="One automation in plain words with its flow"><br><sub>In plain words: When, Only if, Then</sub></p>

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/automations-phone-dark.png" width="150" alt="The list of automations on the phone"><br><sub>On the phone</sub></td><td width="50%" align="center" valign="top"><img src="docs/screenshots/automations-plan-phone-light.png" width="150" alt="The day plan on the phone as an agenda"><br><sub>The day plan as an agenda</sub></td></tr>
</table>

<details>
<summary>4 more pictures</summary>

<p><img src="docs/screenshots/automations-chain-light.png" width="640" alt="Effect chains: sensor, automation, script, device"><br><sub>What led to a switch: the effect chains</sub></p>

<p><img src="docs/screenshots/automations-mode-conflict-light.png" width="640" alt="A mode conflict in the findings of an automation"><br><sub>A mode conflict from real runs</sub></p>

<p><img src="docs/screenshots/automations-plan-light.png" width="640" alt="The day plan with time and sun triggers and the now line"><br><sub>The day plan with the now line</sub></p>

<p><img src="docs/screenshots/automations-board-dark.png" width="640" alt="The dependency board of an entity: what sets it"><br><sub>Dependency board; a dot runs when an automation fires</sub></p>

</details>


### Backup and moving to another host

Settings › Backup saves the whole configuration into one file encrypted with your passphrase (Argon2id, AES-256-GCM): accounts and second factors, connections with their credentials, every name you set, "connected via" entries, drafts, settings and the audit log, optionally history. Automatic backups run on a schedule into the data volume, keep as many files as you choose, and the nightly report says when the newest one is too old. A restore shows first what it replaces, ends every session and works on a new host too, from the first-start page and across architectures (amd64 ↔ arm64). See [docs/BACKUP.md](docs/BACKUP.md).

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/backup-settings-light.png" width="150" alt="Settings, Backup: back up now and automatic backups"><br><sub>Back up now or on a schedule</sub></td><td width="50%"></td></tr>
</table>

<p><img src="docs/screenshots/backup-restore-preview-light.png" width="640" alt="Restore preview comparing now and the backup"><br><sub>Before a restore: what is replaced, what stays, who is signed out</sub></p>


### Storage, counter check and a light map

Settings › Storage shows how much space each kind of data takes and how long it is kept – live history, data paths per hour and per day, availability and events, reports, audit log, scan history – each adjustable. A nightly clean-up removes what is past its retention and compacts the database; the nightly report warns before the disk runs full. Every hour a counter check compares meters that see the same traffic (the router's uplink with NetFlow, this host's interfaces with its connections) and reports traffic counted twice or not at all. The map draws at most 30 frames per second, slows down by itself on a weak device, stops when nobody looks, and is fetched only when something changed. See [docs/PERFORMANCE.md](docs/PERFORMANCE.md).

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/storage-light.png" width="150" alt="Settings, Storage: space per area, retention per kind of data, nightly clean-up, memory"><br><sub>Space per area, retention, nightly clean-up</sub></td><td width="50%"></td></tr>
</table>

<p><img src="docs/screenshots/counter-check-dark.png" width="640" alt="Source view with the counter check and one hour counted twice"><br><sub>Counter check in the source view</sub></p>


### API, webhooks and external reports

Settings › API gives every person their own API tokens for scripts and other programs: the scope is never more than the person's role, every token expires, only a fingerprint is stored, and each token has its own request limit. Administrators add webhooks that tell another program about new devices, devices going offline, the internet line, the nightly report or an old backup – every call signed with HMAC-SHA256 and a timestamp, retried with a growing pause, and listed in a delivery log. Other programs can deliver short reports of their own, which show up in the next nightly report. The reference is built from the OpenAPI description the app makes from its own code. See [docs/API.md](docs/API.md).

<p><img src="docs/screenshots/api-webhooks-light.png" width="640" alt="Settings, API: webhooks with their state and the delivery log"><br><sub>Webhooks with their state and the delivery log</sub></p>

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/api-reference-dark.png" width="150" alt="Settings, API: reference of the public API with scopes and an example"><br><sub>Reference of the public API, from the OpenAPI description</sub></td><td width="50%"></td></tr>
</table>


### Colour themes

Settings > Appearance > Colour theme switches the accent palette of the whole app: the map, the traces, the data paths arcs, the firewall views, the charts and the live grades. **Circuit green** is the default and looks as before; **Aurora** uses turquoise, blue and violet on a cool ground. **SummerGlass** and **WinterGlass** are one glass family: every box is a pane of glass with a light edge along its top, a soft sheen, a soft shade toward its foot in the light face (so a box bulges out instead of looking pressed in) and a soft shadow, a glare that crosses it when the pointer rests on it, a map tile with a state shows it in its sheen and outline (green for reachable, red for down, amber for flapping; unknown stays white), and soft colour fields behind the panes that shine through - SummerGlass is its light face (sky blue, mint and a warm sun on a bright ground), WinterGlass its dark face (ice blue, teal and polar violet on gunmetal). Picking one of them picks its mode, and switching between light and dark switches between the two. The glass needs no blur, so it stays smooth on small machines, and the glare stands still when the system asks for reduced motion. Circuit green and Aurora work in light, dark and automatic mode, and the choice is remembered per device. Warning and error colours keep their meaning in every theme, and the text and graphic contrast of each theme and mode is checked against WCAG AA in the test suite.

<p><img src="docs/screenshots/theme-circuit-dark.png" width="640" alt="The map in the Circuit green theme"><br><sub>Circuit green (default)</sub></p>

<p><img src="docs/screenshots/theme-aurora-dark.png" width="640" alt="The same map in the Aurora theme"><br><sub>Aurora, the same view</sub></p>

<details>
<summary>2 more pictures</summary>

<p><img src="docs/screenshots/theme-summerglass.png" width="640" alt="The control room in the SummerGlass theme"><br><sub>SummerGlass (light)</sub></p>

<p><img src="docs/screenshots/theme-winterglass.png" width="640" alt="The control room in the WinterGlass theme"><br><sub>WinterGlass (dark)</sub></p>

</details>


![Colour theme setting with a preview of each theme](docs/screenshots/colour-theme-picker-light.png)

#### Your own colour theme

Below the tiles, **Import theme...** loads a theme file, **Export current theme** saves the active theme as a starting point, and **Remove imported theme** deletes yours again. A theme file is JSON (up to 64 KB):

```json
{ "format": "lanloom-theme/1", "name": "My theme", "light": { "--ll-accent": "#0A7C95" }, "dark": { "--ll-accent": "#4FD1E8" } }
```

Only colour values are accepted (hex colours, `rgb()`/`rgba()`, shadows made of offsets and a colour, and two plain numbers) for a fixed list of tokens; anything else is refused and unknown entries are ignored. Missing tokens come from the default theme, so a partial file is fine. Before you take a theme, a preview shows it in light and dark together with warnings where text or graphics would fall below the contrast of the built-in themes.

![Preview of an imported theme in light and dark before it is used](docs/screenshots/colour-theme-import-light.png)
 The theme stays in this browser (per device); nothing is sent to the server.

### AI

![Settings, AI: local and cloud services, order and privacy](docs/screenshots/ai-settings-light.png)

<p><img src="docs/screenshots/ai-explain-dark.png" width="640" alt="Explain dialog with the answer, the service and the response time"><br><sub>Explain: a short answer, and where it was made</sub></p>

<p><img src="docs/screenshots/ai-preview-light.png" width="640" alt="Preview of the exact text that is sent, with placeholders"><br><sub>Preview what is sent: placeholders instead of addresses and names</sub></p>


![Another computer: one-time setup code and one command per operating system](docs/screenshots/ai-other-computer-light.png)

![Ask your network: answers with links to the devices and views concerned](docs/screenshots/ask-light.png)

<p><img src="docs/screenshots/report-plain-dark.png" width="640" alt="Nightly report with the paragraph In plain words above the rule-based report"><br><sub>In plain words: an AI paragraph above the report, which stays the reference</sub></p>

<p><img src="docs/screenshots/identify-suggestion-light.png" width="640" alt="AI suggestion for an unknown device with its reason, confirm or dismiss"><br><sub>Unknown device: a suggestion with its reason; nothing changes until you confirm</sub></p>

<details>
<summary>2 more pictures</summary>

<p><img src="docs/screenshots/firewall-ai-suggestions-dark.png" width="640" alt="AI suggestions for moving devices to other networks"><br><sub>Firewall: suggestions to pick from</sub></p>

<p><img src="docs/screenshots/firewall-ai-draft-light.png" width="640" alt="The chosen suggestions in the draft bar, waiting for the preview"><br><sub>Taken over as a draft: the usual preview and confirmation follow</sub></p>

</details>


### Inventory

![Inventory: warranty, end of updates, states and the reasons for "probably gone"](docs/screenshots/inventory-light.png)

<p><img src="docs/screenshots/inventory-edit-light.png" width="640" alt="The sheet of one device: purpose, room, purchase, warranty, person, serial number"><br><sub>The sheet of one device: what it is for, where it stands, how long it is covered</sub></p>

<p><img src="docs/screenshots/inventory-bulk-light.png" width="640" alt="Editing several devices at once"><br><sub>Several devices at once (own fields only)</sub></p>

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/inventory-phone-dark.png" width="150" alt="Inventory on the phone"><br><sub>On the phone the table becomes cards</sub></td><td width="50%"></td></tr>
</table>

<details>
<summary>1 more picture</summary>

<p><img src="docs/screenshots/inventory-dark.png" width="640" alt="Inventory, dark theme, a device that is probably gone"><br><sub>A device that is probably gone, with the reasons</sub></p>

</details>


### Export and import

![The export page: plan as PDF, SVG or draw.io, inventory as CSV or JSON, redact option, import of own fields](docs/screenshots/export-page-light.png)

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/export-plan-light.png" width="150" alt="The network plan as a document: zones as boards, devices as pads"><br><sub>The plan as SVG, PDF or draw.io: zones as boards, devices as pads, lines in the gaps</sub></td><td width="50%"></td></tr>
</table>

<p><img src="docs/screenshots/export-pdf-print.png" width="640" alt="One page of the PDF in the print-friendly look"><br><sub>One page of the PDF, print-friendly (state also in words)</sub></p>

<p><img src="docs/screenshots/export-import-light.png" width="640" alt="Import of own fields: the check lists rows that would change and rows that are wrong"><br><sub>Import of own fields: check first, then apply</sub></p>

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/export-phone-dark.png" width="150" alt="The export page on a phone"><br><sub>On the phone the options and buttons stack</sub></td><td width="50%"></td></tr>
</table>


### Vulnerability hints

![Vulnerability hints: severity, exploited in the wild, certain or possible, why, and what to do](docs/screenshots/vulns-light.png)

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/vulns-card-light.png" width="150" alt="The Vulnerabilities block in the card of a device"><br><sub>The block in the device card</sub></td><td width="50%" align="center" valign="top"><img src="docs/screenshots/vulns-phone-dark.png" width="150" alt="Vulnerability hints on the phone"><br><sub>On the phone, dark theme</sub></td></tr>
</table>


### Availability and stability

![Stability: 35 days per device, gaps, repair time, wobbly candidates](docs/screenshots/stability-light.png)

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/availability-card-light.png" width="150" alt="The card of a wobbly device: availability in the last 24 hours, 35 daily bars and the last gaps"><br><sub>The card of a wobbly device</sub></td><td width="50%" align="center" valign="top"><img src="docs/screenshots/traffic-card-dark.png" width="150" alt="Traffic of a router over 24 hours with exact values under the pointer"><br><sub>Traffic over 24 hours, exact values under the pointer</sub></td></tr>
</table>

<p><img src="docs/screenshots/traffic-full-light.png" width="640" alt="The traffic plot full screen"><br><sub>The plot full screen</sub></p>

<p><img src="docs/screenshots/tile-hover-light.png" width="640" alt="The hover window of a map tile with traffic and availability"><br><sub>Hover window on the map</sub></p>

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/stability-phone-light.png" width="150" alt="The Stability list on the phone"><br><sub>Stability on the phone</sub></td><td width="50%" align="center" valign="top"><img src="docs/screenshots/availability-phone-dark.png" width="150" alt="The card of a device on the phone"><br><sub>The card on the phone</sub></td></tr>
</table>


### Failure chain and outages

![The failure chain of a switch: the root framed, a stage mark on every part that hangs on it, the numbers by kind above the board](docs/screenshots/failure-chain-light.png)

<p><img src="docs/screenshots/failure-list-light.png" width="640" alt="The list by stages with the reason of every part"><br><sub>The list by stages, with the reason of every part</sub></p>

<p><img src="docs/screenshots/failure-sim-dark.png" width="640" alt="Simulated maintenance of a virtualization host: what stops red, what is only impaired yellow"><br><sub>Simulated maintenance: what stops red, what is impaired yellow</sub></p>

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/failure-card-light.png" width="150" alt="The card section Dependencies with a dependency entered by hand"><br><sub>Dependencies on the card, entered by hand: hard, soft or none</sub></td><td width="50%"></td></tr>
</table>

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/failure-phone-outage-dark.png" width="150" alt="The outage note on a phone"><br><sub>The outage note on the phone</sub></td><td width="50%" align="center" valign="top"><img src="docs/screenshots/failure-phone-chain-dark.png" width="150" alt="The failure chain as a sheet on a phone"><br><sub>The chain as a sheet on the phone</sub></td></tr>
</table>

<details>
<summary>3 more pictures</summary>

<p><img src="docs/screenshots/failure-outage-light.png" width="640" alt="The outage note above the board and the dashed parts behind the access point that is down"><br><sub>The outage automatic: a note and the parts it probably reaches</sub></p>

<p><img src="docs/screenshots/failure-hover-light.png" width="640" alt="Hovering a tile lights only the traces it needs on its way up"><br><sub>Hovering a tile: only the traces it needs</sub></p>

<p><img src="docs/screenshots/failure-palette-light.png" width="640" alt="The command Show failure chain asks for the part in a second step"><br><sub>Search and commands: the part in a second step</sub></p>

</details>


### Rewind and incident files

![The rewind of the last 24 hours: the time line with its incidents above the board, the garden access point down at that moment](docs/screenshots/rewind-24h-light.png)

Press **Rewind** above the map (or `R`) and the last 24 hours play back on the board: the dots carry the traffic of that
minute, the tiles show what was down; `Space` pauses, `1`-`4` set 60x to 3600x, `W` switches to 7 days, `Esc` goes back
to live, and the palette understands *rewind to 14:30*. Faults are bundled into **incident files**: devices that went down
together are listed under the part they hang on in the failure chain (sure or probably), with the kind (outage, flapping,
blip, maintenance - Lanloom's own updates and a maintenance window never count as faults), what changed before, the
timeline, the measurements and how it healed. Rule changes made outside Lanloom - on the device or in the controller's own
interface - are noticed as soon as two reads in a row agree and listed under *What changed before* for a whole day back
(danger first, as something to check, not necessarily the cause). A 90-day calendar, a 7-day heatmap by hour, the event chronicle and a
*Timeline & data state* (firmware jumps, own updates, new devices, long outages) answer "when does this happen?". The
nightly report has a section *Incidents of the night*. See the [manual page](docs/manual/incidents.md).

<p><img src="docs/screenshots/incidents-light.png" width="640" alt="The incidents page with the 90-day calendar, the heatmap of the last week and the list"><br><sub>Incidents: calendar, heatmap by hour, the list</sub></p>

<p><img src="docs/screenshots/incident-file-light.png" width="640" alt="The file of an outage of the core switch with root, firmware jump before, timeline and measurements"><br><sub>An incident file: root, what changed before, timeline</sub></p>

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/rewind-phone-dark.png" width="150" alt="The rewind on a phone"><br><sub>The rewind on the phone</sub></td><td width="50%" align="center" valign="top"><img src="docs/screenshots/incident-file-phone-dark.png" width="150" alt="An incident file on a phone"><br><sub>An incident file on the phone</sub></td></tr>
</table>

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/now-rule-change-phone-light.png" width="150" alt="The change list on a phone with a rule that was moved"><br><sub>The same kind of change in the list of recent changes</sub></td><td width="50%"></td></tr>
</table>

<details>
<summary>3 more pictures</summary>

<p><img src="docs/screenshots/rewind-7d-light.png" width="640" alt="The rewind of 7 days at the outage of the core switch three days ago"><br><sub>Seven days back: the core switch and what went with it</sub></p>

<p><img src="docs/screenshots/incidents-chronicle-light.png" width="640" alt="Timeline and data state with firmware jumps, own updates and new devices"><br><sub>Timeline &amp; data state, collected once a day</sub></p>

<p><img src="docs/screenshots/incident-rule-changes-light.png" width="640" alt="What changed before an outage: a zone pair opened from the internet, a new rule, a port forward switched on and a firmware jump"><br><sub>Rule changes made outside Lanloom in the day before an outage</sub></p>

</details>


### Services, web interfaces and certificates

![Links: every web interface with the state of its probe and the certificate clock](docs/screenshots/links-light.png)

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/services-card-light.png" width="150" alt="The card of a device with its services, versions and states"><br><sub>The Services section of a device card</sub></td><td width="50%"></td></tr>
</table>

<p><img src="docs/screenshots/links-certs-light.png" width="640" alt="The certificate clock: expired, red, yellow, hint and valid certificates"><br><sub>The certificate clock</sub></p>

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/links-phone-light.png" width="150" alt="The Links view on the phone"><br><sub>Links on the phone</sub></td><td width="50%" align="center" valign="top"><img src="docs/screenshots/services-phone-light.png" width="150" alt="The services of a device on the phone"><br><sub>The services of a device on the phone</sub></td></tr>
</table>


### Devices and settings

![Device list](docs/screenshots/devices-light.png)

![Settings](docs/screenshots/settings-light.png)

### On the phone

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/now-phone-dark.png" width="150" alt="Phone: Now with the health ring"><br><sub>Now: health ring, sites, changes</sub></td><td width="50%" align="center" valign="top"><img src="docs/screenshots/focus-phone-dark.png" width="150" alt="Phone: Focus, one device with its partners in a ring"><br><sub>Focus: one device, its partners in a ring</sub></td></tr>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/energy-phone-dark.png" width="150" alt="Phone: energy flow"><br><sub>Energy: the dots carry watts</sub></td><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-overview.png" width="150" alt="Phone: overview start page"><br><sub>Overview start page</sub></td></tr>
</table>

<details>
<summary>8 more pictures</summary>

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-rail.png" width="150" alt="Phone: the Right now rail"><br><sub>The Right now rail</sub></td><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-map-light.png" width="150" alt="Phone: map, light"><br><sub>Map, light</sub></td></tr>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-detail.png" width="150" alt="Phone: component card"><br><sub>Component card</sub></td><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-devices.png" width="150" alt="Phone: device list"><br><sub>Device list</sub></td></tr>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-paths.png" width="150" alt="Phone: data paths"><br><sub>Data paths</sub></td><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-reports.png" width="150" alt="Phone: nightly report"><br><sub>Nightly report</sub></td></tr>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-firewall-rules.png" width="150" alt="Phone: firewall rules read from the controller"><br><sub>Firewall rules read from the controller</sub></td><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-ask.png" width="150" alt="Phone: Ask your network"><br><sub>Ask your network</sub></td></tr>
</table>

</details>


<details>
<summary>More phone screenshots</summary>

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-map-dark.png" width="150" alt="Phone: map, dark"><br><sub>Map, dark</sub></td><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-connection.png" width="150" alt="Phone: connection of a device"><br><sub>Connection of a device</sub></td></tr>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-identity.png" width="150" alt="Phone: two addresses that may be one device"><br><sub>Two addresses that may be one device</sub></td><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-paths-full.png" width="150" alt="Phone: world map in full screen"><br><sub>World map in full screen</sub></td></tr>
</table>

<details>
<summary>14 more pictures</summary>

<table>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-netflow-settings.png" width="150" alt="Phone: NetFlow / IPFIX receiver settings"><br><sub>NetFlow / IPFIX receiver settings</sub></td><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-onsite.png" width="150" alt="Phone: sensors on site, batteries and silent radio devices"><br><sub>Sensors on site, batteries and silent radio devices</sub></td></tr>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-sensor-page.png" width="150" alt="Phone: sensor page with the course of one sensor"><br><sub>Sensor page with the course of one sensor</sub></td><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-storage.png" width="150" alt="Phone: storage settings"><br><sub>Storage settings</sub></td></tr>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-api.png" width="150" alt="Phone: webhooks in Settings, API"><br><sub>Webhooks in Settings, API</sub></td><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-ai-explain.png" width="150" alt="Phone: AI explanation of a device"><br><sub>AI explanation of a device</sub></td></tr>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-palette-search.png" width="150" alt="Phone: search and commands"><br><sub>Search and commands</sub></td><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-palette-values.png" width="150" alt="Phone: value filter in the search"><br><sub>Value filter in the search</sub></td></tr>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-report-plain.png" width="150" alt="Phone: nightly report in plain words"><br><sub>Nightly report in plain words</sub></td><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-wizard-scan.png" width="150" alt="Phone: wizard scan step"><br><sub>Wizard scan step</sub></td></tr>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-firewall-move.png" width="150" alt="Phone: move a device to another network"><br><sub>Move a device to another network</sub></td><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-firewall-device.png" width="150" alt="Phone: rename a device or give it a fixed address"><br><sub>Rename a device or give it a fixed address</sub></td></tr>
<tr><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-firewall-preview.png" width="150" alt="Phone: firewall preview"><br><sub>Firewall preview</sub></td><td width="50%" align="center" valign="top"><img src="docs/screenshots/phone-hypervisor-nic.png" width="150" alt="Phone: move a guest's network card to another VLAN"><br><sub>Move a guest's network card to another VLAN</sub></td></tr>
</table>

</details>


</details>

## Quick start

One command (installs into `~/lanloom`, port 7380, with the optional updater):

```sh
curl -fsSL https://raw.githubusercontent.com/robeertm/lanloom/main/deploy/install.sh | sh
```

Or with the compose file from this repository:

```sh
docker compose up -d                      # app only
docker compose --profile updater up -d    # app + one-click updater
```

Then open `http://<host>:7380`. On first start Lanloom prints a one-time **setup code** to its log:

```sh
docker logs lanloom 2>&1 | grep -i "setup code"
```

The code is also stored in `setup-code.txt` in the data volume. Enter it in the wizard to create your account. See [docs/INSTALL.md](docs/INSTALL.md) for all platforms and options.

### First start

The setup assistant takes you from the setup code to a first picture of your network, usually in under five minutes (the time is measured and shown on the last screen).

1. **Account.** Enter the setup code and create the administrator account.
2. **Networks.** Lanloom reads the host's interfaces, routes, neighbour table and DHCP lease files (nothing is sent) and shows the gateway, DNS and DHCP it found. It proposes private ranges of at most /22; routed networks behind a router are listed but never pre-ticked. You confirm with a mandatory consent box, then start the gentle scan directly or open *Scan options* for the deep scan.
3. **First picture.** As soon as the quick phases (neighbours, mDNS, SSDP, WS-Discovery) are done, the devices appear as a preview. *Open the map now* lets you start looking while the scan finishes in the background.
4. **Connect**. From what the gentle scan saw, Lanloom suggests integrations (home-automation hub, MQTT broker, SNMP, controllers, hypervisors), each with a step-by-step guide to create a read-only account or token, a prefilled address and a test button. Lanloom never tries a sign-in on its own.
5. **Done**. Rescan interval, update check and the time to your first picture.

Not sure yet? *Explore a sample network* opens a read-only demo beside your real inventory. You can run the assistant again any time under Settings › Setup assistant; devices, names and connections stay. Details: [docs/INSTALL.md](docs/INSTALL.md#first-start).

## How the scan works

The default **gentle scan** needs no privileges and sends very little:

- It reads the host's kernel neighbour table and nudges addresses with a single UDP datagram to port 9, so the kernel resolves them. No raw sockets, no ICMP.
- It browses mDNS/DNS-SD and sends an SSDP M-SEARCH. Device descriptions are fetched only from the responding private address, capped at 64 KiB, and XML with DOCTYPE or ENTITY declarations is refused.
- It tries a TCP connect to 27 well-known service ports and reads the page title and `Server` header of web services (`GET /` only).
- It reads the kernel's IPv6 neighbour cache (nothing is sent), sends one WS-Discovery probe, and asks each found device once for its NetBIOS name and, if it has no name yet, once by LLMNR.
- Asking devices with the factory SNMP community is a separate switch and off by default.
- Limits: about 40 new connections per second, 24 concurrent, 4 per host. A /24 takes roughly 10 to 60 seconds.

The **deep scan** checks about 218 common ports with the same limits and only runs with your explicit consent. Lanloom never scans public ranges, never reaches into the internet, and never tries logins. Only private ranges up to a /22 (1024 addresses) are accepted.

A presence check runs every 2 minutes (one TCP connect to a known open port, or a look at the neighbour table). A gentle rescan runs automatically every N hours (default 6).

Devices are classified by deterministic rules (gateway or self role, DNS-SD types, UPnP and WS-Discovery device types, port combinations, neutral words in titles and host names). A score of 0.6 or more is "sure", 0.3 or more "probable", otherwise "unknown".

### Finding everything: maker, model, interfaces

Every device card shows the maker (what the device says itself, otherwise the public IEEE address register that ships with the image and is never fetched at run time), model, firmware, IPv6 addresses and the origin of the hardware address. A private (random) address never gets a maker. Two addresses of one device (two network cards, cable and radio) are drawn as one component when the evidence is strong (same UPnP device id, same SNMP system name, same NetBIOS or mDNS name, neighbouring addresses on the same port); weaker cases are offered as a suggestion, and every grouping can be undone. The nightly report names the maker of new devices and does not report a second interface of a known device as new. Details: [docs/DISCOVERY.md](docs/DISCOVERY.md).

<p><img src="docs/screenshots/identity-light.png" width="170" alt="Device card with maker, model, firmware and a second interface, light">
<img src="docs/screenshots/identity-dark.png" width="170" alt="The same card, dark"></p>


### Networks, VLANs, devices without an address, scan protection

*Settings › Network* lists every network and VLAN Lanloom knows. It scans only what it reaches directly; behind a router it fills a network from the controller's data and sends nothing into it, unless you agree to a gentle scan across that router. Devices that only a controller or a switch knows (other VLANs, hardware addresses without an IP address) join the inventory without a scan, and a second network card without an address folds into its device when the evidence is strong. A protection list keeps printers off their raw printing ports, industrial controllers to the web ports and medical or payment systems completely passive. The catalog of what can be connected shows, per device, which source fits (read-only first); entries that are not available yet say so. Details: [docs/DISCOVERY.md](docs/DISCOVERY.md).

<p><img src="docs/screenshots/network-vlans-light.png" width="640" alt="Network map with a zone for each VLAN"></p>

<p><img src="docs/screenshots/network-zones-light.png" width="170" alt="Settings: networks and VLANs with their reach"></p>

<p><img src="docs/screenshots/network-connect-light.png" width="640" alt="What can be connected: planned entries show their steps"></p>

<p><img src="docs/screenshots/phone-network-protect.png" width="170" alt="Phone: scan protection list"></p>


## Live data and integrations

Dots appear only where bytes are actually measured:

- **This host's own TCP flows** come from the kernel's socket diagnostics (netlink) when running with host networking. No extra privileges needed. They also feed the data paths world map.
- **NetFlow v5 / v9 / IPFIX** from your router or firewall (optional, off by default, UDP 2055): the traffic of every device for the data paths world map and the conversations between devices. Only packets from the exporter addresses you allow are read (flow packets from another address are listed with an *Allow* button); templates are kept per exporter and expire when the router stops repeating them; the settings show protocol, state, flows and templates per exporter. Only country, device and bytes are kept (for a conversation: the two devices and the bytes per direction). See [docs/manual/netflow.md](docs/manual/netflow.md) and [docs/DATA-PATHS.md](docs/DATA-PATHS.md#setting-up-netflow--ipfix).
- **Router or switch (SNMP v2c or v3)** gives interface counters, which animate the internet uplink, and LLDP neighbours plus the switch address table for the cabling. Only Get, GetNext and GetBulk are used.
- **Switch controller** (the local controller application of a switch and access point product line) tells which client sits on which switch port or access point. A local view-only account is enough. Lanloom signs in and then only reads two lists; one code path makes every request and refuses everything else.
- **Device-family adapters** read a controller's configuration and state through its local APIs (an API key and/or a local view-only account): zones, networks, firewall rules with hit counters, ports, wireless networks, clients and their ports. They only send read requests through one allow-listed code path, refuse an account that can change settings where that is checkable, and cap sign-ins. A zone firewall adapter reads zones, interfaces, rules, NAT, DHCP and VPN through query documents that can only read. A gateway firewall adapter reads interfaces, rules, NAT, reservations, routes, gateways and VPN instances with `GET` requests only. An access-point controller adapter reads devices, clients, switch ports, networks, wireless networks and access rules after one token exchange. A home-router adapter reads hosts, mesh topology and the internet connection state through calls that are rows of a fixed table of read actions. A switching-router adapter reads the router, its neighbours, ports with VLANs, addresses, clients, forwarding, internet connections and filter rules with `GET` requests only. A hypervisor adapter reads nodes, guests, their network cards, bridges, firewall, storage and backups with a token that can only read (checked against the token's own permission list). Which families exist: [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md). The read half never writes; a family that can also change something (so far: clients of one controller family - move, fixed address, name - guests of the hypervisor family - start, clean shutdown, network card bridge and VLAN, guest firewall and rules - and firewall rules of the gateway firewall family, with the firewall's own revert timer where the release has it) does it only through the separate executor service with its own account, after the gate and your passkey signature.
- **MQTT broker** topics are grouped; Lanloom only counts messages and lengths per group. It connects, subscribes to `#`, and cannot publish. Payloads are never stored. TLS is optional. (Publishing a few of Lanloom's own figures is a separate, optional output, see below.)
- **Home-automation hub (WebSocket API)** adds rooms, devices and entities, state changes and automation runs, using a long-lived access token (a non-admin user is enough). A strict allow-list limits it to read commands; it cannot call services and never renders templates. Optional **admin mode** also reads which entities each automation and script uses and, on request, their last runs – within budgets per minute, never looking up scripts that call each other in a loop. People, trackers, zones, cameras, calendars and notifications are kept out by default. See [docs/HUB-BRIDGE.md](docs/HUB-BRIDGE.md).

Supported integrations: [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md).

All integrations are optional. Credentials are encrypted at rest with a key file (`secret.key`, mode 0600) in the data volume, never displayed again and never logged.

## Add-on and MQTT output

**Add-on.** If your home-automation hub has an add-on system with ingress, Lanloom runs there as an add-on: copy [`addon/lanloom`](addon/lanloom) into the hub's folder for local add-ons, install it from the add-on store and open it from the side panel. You are signed in with your hub account (the user header is believed only from the add-on system's ingress proxy), Lanloom's own updater is switched off because the add-on system updates it, and the data lives in the add-on volume, so the hub's backups include it. Host networking is the only extra it asks for. Details: [docs/ADDON.md](docs/ADDON.md).

**MQTT output (off by default).** *Settings › MQTT output* publishes ten figures as sensors of the hub through MQTT discovery: devices, devices online and offline, new devices, the internet line, open issues, the network health, open vulnerability hints, the last nightly report and its open points. Only counts, a level, a flag and a time leave Lanloom - never a device name, address, person or place. Its own small client sends only CONNECT, PUBLISH, PINGREQ and DISCONNECT and only to the topics of its own node (checked twice, tested against the source); it never subscribes, so nothing comes back. Switching off removes the sensors again. In the add-on it can use the broker the add-on system offers. Details: [docs/MQTT-OUTPUT.md](docs/MQTT-OUTPUT.md).

## AI – local or cloud

Optional and off by default. With an AI set up, an **Explain** button appears on a device, on a note of the nightly report and on a firewall rule. The answer is short, in plain words and in the language of the app. The AI only explains: it never changes a setting, and every change still goes through the preview and your confirmation.

- **Ask your network:** a panel from the top bar (full screen on a phone) answers questions about devices, availability, data paths, the firewall configuration and the nightly reports. The facts are built on the server from Lanloom's own data; the answer links to the devices and views it names.
- **In plain words:** a short AI paragraph above each nightly report. The rule-based report stays the reference; without an answer the paragraph is missing. It goes into the delivered report only if you allow it.
- **Unknown devices:** the AI suggests a kind with a reason; once you confirm, it counts as a weak clue that never overrides better evidence or your own choice. Optional nightly suggestions for up to five unsure devices.
- **Firewall suggestions:** proposed moves to other networks, only those the editor allows; your click puts them into the draft, then the usual preview and confirmation apply.

- **Local AI** runs on a computer in your network. It gets the real data, and nothing leaves your network.
- **Cloud AI** is a service on the internet with your own key. Before anything is sent, every IP and hardware address, host and device name, wireless network name, serial number and location is replaced by a placeholder; the answer is translated back on your host. If an address pattern would still slip through, nothing is sent. Each function needs its own permission, and a daily limit caps the requests. "Preview what is sent" shows the exact text first.
- **Three one-click ways to a local AI:** in the app (Settings, AI, "Install local AI here", through the updater sidecar), with the installer option `--with-ai`, or on another, stronger computer with a short setup command and a one-time code. Setup counts as done only after the model has answered a test question.
- **Honest hardware advice:** below 4 GB of memory no local AI is offered, below 8 GB only a small model, and slow computers are pointed to the other ways.

Supported services: [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md). Step-by-step guide: [docs/integrations/ai.md](docs/integrations/ai.md).

## API

Lanloom has a public API for scripts and other programs, so a dashboard, a backup job or a home-automation rule can use the network map without a person signing in. Everything starts with a token that every person makes under *Settings > API*. Details: [docs/API.md](docs/API.md).

- **API tokens** per person with a scope (read, operate, admin, or ingest for scripts that only deliver reports), never above the account's role; they always expire and only a hash is stored.
- **Public API v1**, described as OpenAPI 3.1 (`/api/v1/openapi.json`): status, devices, topology, availability, data paths and nightly reports; fields are only added within v1.
- **Webhooks** that administrators set up: signed messages for new, gone, offline and returning devices, the internet line, incidents, new reports and stale backups, with retries and a delivery log.
- **External reports:** other programs deliver a short report that shows in the app and in the nightly report.
- **Rate limit and audit:** a request limit per token, and every change made with a token is written to the audit log with the token's name.

## Security and privacy

Sign-in is mandatory; there is no open mode. The first account can only be created with the setup code. Further people join by one-time invitation with a viewer, operator or admin role, enforced on the server for every route; a second factor (authenticator app or passkey) is required for admins by default, critical actions ask for a fresh confirmation, and every change is written to a tamper-evident audit log ([docs/ACCESS.md](docs/ACCESS.md)). Sign-in through your own authentication proxy is optional and off by default. Passwords use argon2id, sessions use random 256-bit tokens in `HttpOnly`, `SameSite=Strict` cookies, and state-changing requests are protected against cross-site use. The container runs as an unprivileged user with a read-only root file system and no capabilities. There is no telemetry. The optional update check against the public release feed is the only outbound connection of its own; everything else happens only when you switch it on: the delivery of the nightly report summary and of notifications (web push to the push service of your own browsers), the optional heartbeat to an address you enter, webhooks to an address an administrator enters, a cloud AI service (which only ever receives placeholders instead of addresses and names) and the download of the local AI service and its models. Countries on the data paths map come from a database inside the image; no address is ever looked up online, and only countries and byte counts are stored. Vulnerability hints come from a feed inside the image as well; nothing is looked up online. Put Lanloom behind a TLS reverse proxy for passkeys and remote access (list it in `LANLOOM_TRUSTED_PROXIES`, see [docs/INSTALL.md](docs/INSTALL.md#behind-a-reverse-proxy)), and never expose it directly to the internet. Real changes on a controller run only in the optional executor service (its own container, updated by hand, credentials the app never sees, a passkey signature per change, a connection guard that allows only the app and the controller). Use them at your own risk and keep local console access to your controller; Lanloom never writes without your passkey. Details and how to report a vulnerability: [SECURITY.md](SECURITY.md).

**Supply chain.** Every image is signed keyless (Sigstore, no key anywhere), carries a signed SPDX SBOM per platform, and is scanned for known vulnerabilities in its Python and operating-system packages before it is released (critical or high = no release, unless a dated exception explains why the flaw does not reach Lanloom). "No telemetry" is a checked rule: a static check lists every place in the code that can connect anywhere against an allow-list. How to verify an image: [SECURITY.md](SECURITY.md#verifying-an-image); threat model: [docs/SECURITY-MODEL.md](docs/SECURITY-MODEL.md).

## Updating

- **With the updater sidecar:** an "Update" button appears when a new version is available. It pulls the image, runs a self-test without network, starts a trial on a copy of your data, backs up the database and tags the previous image, swaps the container, checks health, and rolls back automatically on failure.
- **Without it:** `docker compose pull && docker compose up -d`.
- **As an add-on:** the hub's add-on system updates Lanloom; the built-in updater and the release check are off.
- **Channels:** Stable (default) or Preview; Preview offers a pre-release only when it is newer than the newest stable release and went through the same gate and signing, never `:edge`. Details: [docs/manual/updating.md](docs/manual/updating.md).
- **Hold this version:** no update notices until you release it; the nightly report notes when a newer version waits.
- **Back to the previous version:** one click for administrators with a fresh confirmation, same checks as an update; not offered for versions before 1.29.0 (unsigned), which are restored by hand from the backup.
- **Signature check:** the updater (1.34.0 and later) verifies the signature of the new image against the publishing workflow and the public transparency log before it swaps anything; on failure nothing changes.
- **The updater renews itself:** after a healthy update it re-creates itself from the same checked image (no second download), waits for its heartbeat and goes back to the old updater if none comes; only one updater runs at a time. An updater from before this feature is renewed once by hand, the settings page shows the command.
- **What's new?:** the version number in the header opens the changes of each version from the changelog inside the image; nothing is fetched online.

Image tags: version tags such as `:1.1.0` are immutable; `:1` and `:latest` move only after the published image has been tested; `:edge` is the latest build of the main branch.

## Requirements

- A container runtime with compose v2 (details in [docs/INSTALL.md](docs/INSTALL.md))
- amd64 or arm64
- About 300 MB for the image

### Hardware requirements

| Network size | Host | Memory for Lanloom | CPU (one core) | Map request | Database |
|---|---|---|---|---|---|
| up to 250 devices | a 4-core arm64 single-board computer with 2 GB (1.5 GHz class) or any small amd64/arm64 machine | about 80 MB | about 3 % at rest | 0.06 s overview, 0.09 s network view | about 2 MB |
| up to 1000 devices | a 4-core arm64 single-board computer with 4 GB, or better | about 100–150 MB | about 3 % at rest, up to 16 % with live traffic on every device | 0.24 s overview, 0.4 s network view | about 7 MB |

Measured on one core of a current 4-core arm64 single-board computer (2.4 GHz class; a 1.5 GHz board is roughly two to three times slower per core); the container starts in about 5 seconds. The viewing device needs no graphics acceleration. All values, the method and the retention settings: [docs/PERFORMANCE.md](docs/PERFORMANCE.md); measure your own host with `python3 scripts/benchmark.py`.

## Documentation

- [docs/manual/README.md](docs/manual/README.md): the manual, one page per view (overview, map, network, data paths, devices, inventory, firewall, nightly reports, running, settings, AI) with purpose, what you see, how to use it, limits and privacy; [docs/manual/views.md](docs/manual/views.md) covers the rails, keys, live state and deep links
- [docs/TOUR.md](docs/TOUR.md): the tour: every view in light and dark and in the colour themes Circuit and Aurora, and the phone layout; the animated clip
- [docs/site/README.md](docs/site/README.md): the static landing page (`docs/site/`): what is in it, how to preview it, how to publish it with Pages, the pictures and the preview image
- [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md): reading the status of a source, the reasons of every source and adapter with cause and fix, scanning, sign-in, install, and the redacted diagnostic export
- [docs/INSTALL.md](docs/INSTALL.md): installation on each platform, configuration, backup, moving to another host, uninstall
- [docs/BACKUP.md](docs/BACKUP.md): backup and restore in the app, automatic backups, moving to another host, the file format
- [docs/NOTIFICATIONS.md](docs/NOTIFICATIONS.md): notification rules per person, quiet hours, planned maintenance, flap protection, web push, the heartbeat
- [docs/ACCESS.md](docs/ACCESS.md): users and roles, invitations, two-step sign-in, sessions, audit log, authentication proxy, sub-path
- [docs/API.md](docs/API.md): API tokens and scopes, the public API v1 with every route, errors, signed webhooks with verification code, external reports
- [docs/CONTROL-ROOM.md](docs/CONTROL-ROOM.md): the control room: the internet with two readings, flap guard and reserve line, mobile-router card, storage boxes, battery backup (NUT, UPS-MIB), backup chains (current, overdue, failed, running, cannot be judged), roles, nightly section.
- [docs/RESILIENCE.md](docs/RESILIENCE.md): resilience: the seven axes, risk and levels, score and grade, emergency scenarios (power cut with UPS runtime, internet down, a part off), layers, risk lens, own fields, routes, nightly section.
- [docs/HOST-HEALTH.md](docs/HOST-HEALTH.md): host gauges from every source, early warnings (trend and forecast, 72 hours), waiting on someone.
- [docs/AVAILABILITY.md](docs/AVAILABILITY.md): availability in 5-minute slots, the Stability list, traffic plots over 1 hour to 7 days, the card sections, simulate maintenance, what is stored
- [docs/SERVICES.md](docs/SERVICES.md): services and web interfaces per device, the Links view with its probe (answers, login required, error, dead, removed), the certificate clock, what is looked at and how gently
- [docs/EXPORT.md](docs/EXPORT.md): export and import: the network plan as PDF, SVG or draw.io, the inventory as CSV or JSON, import of own fields, the redact option
- [docs/INVENTORY.md](docs/INVENTORY.md): inventory and life cycle: own fields, states, why a device is "probably gone", provisional entries, warranty and end of updates, the offline table, roles
- [docs/VULNERABILITIES.md](docs/VULNERABILITIES.md): vulnerability hints: the offline feed and its sources, building and importing a feed file, the matching rules (certain, possible, not checked), what to do, events, roles and API
- [docs/DISCOVERY.md](docs/DISCOVERY.md): what the scan asks, maker and model recognition, the address register, one device with several interfaces, networks and VLANs, devices without an address, scan protection, what can be connected
- [docs/HUB-BRIDGE.md](docs/HUB-BRIDGE.md): the home-automation bridge: what is read, admin mode, sparing the hub, privacy filter
- [docs/ADDON.md](docs/ADDON.md): Lanloom as an add-on of a home-automation hub: installing, sign-in through the side panel, options, network, updates, data
- [docs/MQTT-OUTPUT.md](docs/MQTT-OUTPUT.md): the optional MQTT output: sensors, topics, payloads, privacy, the write rules, API
- [docs/ACCESSIBILITY.md](docs/ACCESSIBILITY.md): the tree view of the network, the keyboard, screen readers, colours and contrast, how it is tested
- [docs/AUTOMATIONS.md](docs/AUTOMATIONS.md): the Automations tab: plain words, flow, findings, dependency board, who switches what, timeline, day plan, rooms, hub health
- [docs/TOPOLOGY.md](docs/TOPOLOGY.md): physical topology, sources, view-only account, how shared ports are read
- [docs/DATA-PATHS.md](docs/DATA-PATHS.md): the data paths world map, what is counted where, unusual behaviour, privacy, network routes, country data ([IP Geolocation by DB-IP](https://db-ip.com), CC BY 4.0)
- [docs/PERFORMANCE.md](docs/PERFORMANCE.md): hardware requirements with measured values, storage and retention, the nightly clean-up, the counter check, the frame budget of the map
- [docs/HOST-AGENT.md](docs/HOST-AGENT.md): the optional host agent and container source: what is read and never read, the secret filter, installation, pairing, updates, limits
- [docs/NIGHTLY-REPORT.md](docs/NIGHTLY-REPORT.md): the nightly report, its sections, the archive, optional delivery, adding a section
- [docs/PALETTE.md](docs/PALETTE.md): search and commands: keys, what is searched, the value filter, actions, adding a command
- [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md): supported integrations and AI services, what each can read and write
- [docs/integrations/ai.md](docs/integrations/ai.md): AI, local or cloud: setup in three ways, privacy layer, hardware, troubleshooting
- [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md): how device-family adapters are built (read and write halves, capabilities, plan steps, credentials)
- [docs/FIREWALL-EDITOR.md](docs/FIREWALL-EDITOR.md): the firewall editor, the executor service and its safety checks
- [docs/FAQ.md](docs/FAQ.md): frequently asked questions
- [docs/ARCHITECTURE-CORE.md](docs/ARCHITECTURE-CORE.md): how the board engine is structured as a versioned core
- [SECURITY.md](SECURITY.md): security rules, verifying an image, reporting
- [docs/SECURITY-MODEL.md](docs/SECURITY-MODEL.md): threat model (local network, stolen session, malicious counterpart, supply chain), no-telemetry check, container principles, headers, writing endpoints
- [CHANGELOG.md](CHANGELOG.md): release notes
- [CONTRIBUTING.md](CONTRIBUTING.md): ideas, bug reports and adapter requests (as issues)

## License

Lanloom is **source-available**, not open source.

- **Free:** private, non-commercial use by one person for their own household, on machines they own or control, with up to **100 devices** in total (every device or host Lanloom discovers or keeps in its inventory, counted across all installations and sites; the sample network does not count). Reading the source is allowed.
- **Paid license required:** every **commercial use** - by or for a company, a self-employed person, a public authority, an association or other organisation, a school or other educational institution, a service provider for its customers, or any use connected with a trade, business or profession - whatever the number of devices; and private use with **more than 100 devices**. Until a way to buy a license is published, contact the copyright holder.
- Modification and redistribution require prior written permission. It is provided without warranty of any kind.
- These terms apply to every release that ships with them (see the [changelog](CHANGELOG.md)) and to later releases. Releases published earlier keep the license they shipped with.

See [LICENSE](LICENSE); the license text is what counts. Third-party components and their licenses are listed in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). The cable data of the *Real cables* view (`lanloom/data/cables-osm.json.gz`) is © OpenStreetMap contributors and stays available under the [Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/), separately from the code; `lanloom/data/cables-open.json.gz` holds cable lines of U.S. waters (NOAA Office for Coastal Management, BOEM) and cable facts (Wikidata), both CC0 1.0; see [lanloom/data/CABLES-LICENSE.md](lanloom/data/CABLES-LICENSE.md).

© 2026 Robert Manuwald
