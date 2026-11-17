# Lanloom

**Your local network, drawn as a circuit board.** Devices and services become components, connections become routed traces, and measured traffic flows along them as moving dots.

![Lanloom map in dark theme](docs/screenshots/hero-map-dark.png)

> ## ⚠️ Please read before installing
>
> - **It scans your network.** Lanloom probes the private address ranges you approve. Only scan networks you own or are explicitly allowed to scan. The optional deep port scan touches many ports per device and may trigger alerts in intrusion-detection or security tools; it only runs after you tick an explicit consent box.
> - **It stores credentials you give it.** Optional integrations (home-automation hub, MQTT broker, router or switch, switch controller, device-family adapters) need secrets. They are stored encrypted (AES-256-GCM) and never shown again, but the key file lives next to the data: whoever controls the host controls them. Use read-only or least-privilege accounts.
> - **Host networking.** To see your network, the container shares the host's network stack. It runs unprivileged, with a read-only root file system and all capabilities dropped, but it can reach whatever the host can reach.
> - **The optional updater sidecar gets the container-runtime socket.** Access to that socket is root-equivalent on the host. Skip the sidecar (`--no-updater`) if you prefer to update by hand.
> - **The firewall editor only simulates unless you arm it.** Real changes need the optional executor service, a built-in adapter for your controller family that can write (the adapters in this release only read) and an explicit arming step. A real change can interrupt the wireless network for minutes and can lock you out if your network has paths the gate does not know. Read [docs/FIREWALL-EDITOR.md](docs/FIREWALL-EDITOR.md) first.
> - **Values are observations, not guarantees.** Classifications are heuristics, and traffic and liveness figures depend on what could be measured. Do not base security or safety decisions on them.
> - **Provided as is, without warranty.** See [LICENSE](LICENSE).
> - **Written with AI assistance.** Parts of this software were written with the help of AI tools; the code is reviewed and covered by automated tests and release checks.

## What it does

- **Overview first.** The start page says in three seconds how the network is doing: a health banner with the last 24 hours in five-minute steps, six key figures (devices, online, new since yesterday, fresh sources, the internet line, data paths of the day) that each link to the details, and cards for what stands out, the nightly report, new and uncertain devices (first seen, origin of the address) and the last sign of life of every source. A source that simply has nothing yet says so calmly ("Source waiting", "No data yet", "Stale") instead of showing an error. The page redraws itself every few seconds without folding what you opened.
- **Right now, About this view, live state.** Key `L` (or the pulse button) opens the *Right now* rail: what is noteworthy (one sub line each), the active conversations with their rates, the changes of the last hours and new devices. Key `I` explains the open view and links to the manual; on wide screens it is open by default. The header says "Live" with the time of the last update, "Updated 12 s ago", "Disconnected" with a Reconnect button, or "Paused" while the tab is in the background; its dialog shows the live grade of every source and the clock offset between your browser and the server. See [docs/manual/views.md](docs/manual/views.md).
- **Deep links everywhere.** Every view, component card, device, nightly report and map filter has its own address (`#/map/<id>?zone=services`), so you can bookmark or share it, and the back key returns to what you saw before. The map filters by area or shows only what is down.
- **Draws the network as a PCB.** Zones for the Internet edge, Network, Servers and services, Computers and phones, Media and cameras, Smart home, Printers and other. Hub rooms and MQTT topic groups appear when those integrations are connected.
- **Routes traces like a board.** Orthogonal and 45° segments, buses and vias. Traces that end at the same component and port share one trunk, and their dots add up on it.
- **Says where traces go.** End marks name peers outside your network where a trace leaves the board; port marks show the destination port or protocol at the end of a live trace.
- **Animates only what is measured.** Each dot stands for a fixed number of bytes ("1 dot ≈ 64 KiB"; the quantum is chosen automatically in powers of two) and bytes are conserved. Traces without measurements stay dashed.
- **Shows how fresh the data is.** Every source and component carries a live grade: realtime (cadence up to 2 s), near (up to 15 s), periodic (up to 10 min), sluggish, interrupted, or unknown.
- **Two views.** *Map* is an overview by kind; *Network* shows the gateway, the switches and access points, one zone per subnet and the guests of virtualization hosts.
- **Physical topology.** Which device hangs on which switch port, which clients use which access point (with band and network name), and which guests run on a host. Cables are solid traces, wireless links dashed with a radio mark, guests dotted; the port number sits at the device end. The data comes from a manual "connected via" field, a switch controller (view-only account), LLDP neighbours and switch address tables over SNMP, in this order of precedence. Clients behind an access point default to wireless. This host's measured traffic flows along the cabling. See [docs/TOPOLOGY.md](docs/TOPOLOGY.md).
- **Data paths.** A calm world map shows where your devices send data on the internet, by country: arcs from your home country, width by bytes, outside the EU in amber. 24 hours, 7 days or 30 days, a full-screen map, lists of countries and devices, and calm hints for unusual behaviour (a device talks to a new country, uploads far more than on a typical day, or sits in a network that should not reach the internet). Countries come from an offline database shipped in the image; only countries and byte counts are stored, never addresses. Own networks and tunnels are kept apart, and the EU share only counts traffic with a known country. To see every device, not only the Lanloom host, let your router send NetFlow v5, v9 or IPFIX (with IPv6) to Lanloom's built-in receiver: off by default, only from the exporter addresses you allow. The map zooms with the mouse wheel or a pinch, up to 16 times. See [docs/DATA-PATHS.md](docs/DATA-PATHS.md).
- **Nightly report.** Every morning a short report about the night in plain words: internet outages, new and missing devices, interrupted connections and refused sign-ins, data paths hints, changes to the controller configuration, expiring certificates, and Lanloom's own updates and restarts as maintenance. One headline says "All quiet" or how many things need a look; those can be ticked off. Rule-based from Lanloom's own data, with no online service needed (an AI can explain a note on request, see below); made at the next start if Lanloom was off at report time; 30-day archive; optional delivery by e-mail, webhook, push topic or your own chat bot (off by default). See [docs/NIGHTLY-REPORT.md](docs/NIGHTLY-REPORT.md).
- **Several users, three roles.** Viewer, operator and admin, enforced on the server for every route. Invitation links, two-step sign-in with an authenticator app, recovery codes or passkeys, sessions you can end, security notices and a tamper-evident audit log. Optional sign-in through your own authentication proxy. See [docs/ACCESS.md](docs/ACCESS.md).
- **AI, local or cloud (optional).** An Explain button on devices, notes of the nightly report and firewall rules lets an AI answer in plain words, in the language of the app. **Ask your network** answers questions with links to the devices and views concerned, the nightly report gets a paragraph *In plain words*, the AI suggests what an unknown device is (you confirm), and the firewall editor shows suggestions you can take over as a draft. Off by default; AI only reads and never changes a setting. See [docs/integrations/ai.md](docs/integrations/ai.md).
- **Component card.** Classification with reasons, open ports, announcements, a chart of recent traffic, the physical connection (where it is plugged in, which source says so, what other sources say, the devices on its ports), and options to rename, re-type, hide or set "connected via" by hand.
- **Search and commands.** One box for everything: press `Ctrl K` (`⌘ K` on a Mac) or `/`, or tap the Search button in the top bar. It finds devices, virtual machines and containers, services, automations and entities of a home-automation hub, and it runs commands: every view, the settings pages, light/dark, colour theme, rate unit, "Check for updates". Typos are forgiven, the last choices come first, a hit has a menu of actions (open, copy the address), and a comparison such as `> 25 °C` lists hub values that fit. Read only, full screen on a phone, usable with a keyboard and a screen reader. See [docs/PALETTE.md](docs/PALETTE.md).
- **Device list** with search, light/dark/auto mode and two colour themes (Circuit green and Aurora), a phone layout with bottom tabs, English and German UI.
- **Firewall editor.** A Firewall tab shows every zone, network and device. In Change mode you drag a device to another network (or tap it and choose "Move to…" on a phone), collect moves in a draft and get a plain-language preview: what the device may do afterwards, what it may no longer do, and what to watch out for. Out of the box it runs as a **simulation** against a sample network and writes nothing anywhere. See [docs/FIREWALL-EDITOR.md](docs/FIREWALL-EDITOR.md).
- **Your controller's configuration, read only.** With a device-family adapter (see [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)) the Firewall tab shows the real zones, networks and clients, the firewall rules in the controller's order with hit counters and a zone matrix, port forwarding, wireless networks, and the network equipment with every switch port. The same adapter feeds the physical topology. It uses a read-only account or key and cannot change anything.
- **Your hypervisor, read only.** A hypervisor adapter puts every virtual machine and container below the node it runs on, and shows on the component cards what runs where, with which bridge and VLAN, how full the storage is and when each guest was last backed up. Findings point at guests without a recent backup, storage that is almost full and VLANs that cannot leave the node. It uses a token that can only read; a token that could change anything is refused.
- **Read-only by construction.** The map observes; the app cannot change your devices. Real firewall changes are only possible through the separate, optional executor service, with a passkey signature per change and an explicit arming step by the operator.

## Screenshots

All screenshots show the built-in demo network with invented names and addresses.

### Setup wizard

<table>
<tr>
<td width="50%"><img src="docs/screenshots/wizard-1-account.png" width="100%" alt="Wizard step 1: account"><br><sub>1. Create the account with the one-time setup code</sub></td>
<td width="50%"><img src="docs/screenshots/wizard-2-networks.png" width="100%" alt="Wizard step 2: networks"><br><sub>2. Detected networks: gateway, DNS, DHCP and the proposed ranges</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/screenshots/wizard-3-scan.png" width="100%" alt="Wizard step 3: scan"><br><sub>3. Scan options: gentle or deep</sub></td>
<td width="50%"><img src="docs/screenshots/wizard-4-progress.png" width="100%" alt="Wizard scan progress"><br><sub>First picture while the scan continues</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/screenshots/wizard-5-review.png" width="100%" alt="Wizard step 4: review"><br><sub>4. Review the automatic classification</sub></td>
<td width="50%"><img src="docs/screenshots/wizard-6-connect.png" width="100%" alt="Wizard step 5: connect"><br><sub>5. Suggested adapters with a read-only guide</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/screenshots/wizard-7-done.png" width="100%" alt="Wizard step 6: done"><br><sub>6. Done: time to the first picture, rescan interval, update check</sub></td>
<td></td>
</tr>
</table>

### Overview

![Overview start page, light theme](docs/screenshots/overview-light.png)

<table>
<tr>
<td width="50%"><img src="docs/screenshots/overview-dark.png" width="100%" alt="Overview start page in dark theme"><br><sub>Health with the last 24 hours, key figures, noteworthy issues, new devices</sub></td>
<td width="50%"><img src="docs/screenshots/overview-rail-dark.png" width="100%" alt="The Right now rail beside the overview"><br><sub>Key L: noteworthy issues, active conversations, changes, new devices</sub></td>
</tr>
</table>

### The map

![Map, light theme](docs/screenshots/map-light.png)

![Map filtered to one area](docs/screenshots/map-filter-light.png)

![Network view, dark theme](docs/screenshots/network-dark.png)

![Network view, light theme](docs/screenshots/network-light.png)

### Cabling

<table>
<tr>
<td width="50%"><img src="docs/screenshots/connection-switch-light.png" width="100%" alt="Switch card with the devices on its ports"><br><sub>A switch: where it is plugged in and what hangs on each port</sub></td>
<td width="50%"><img src="docs/screenshots/connection-conflict-dark.png" width="100%" alt="Device card with a manual entry that overrides the controller"><br><sub>A manual entry wins; the card says what the other source reports</sub></td>
</tr>
</table>

### Component card

![Component card](docs/screenshots/detail-light.png)

![Room detail](docs/screenshots/area-detail-dark.png)

### Live grade

![Live grade](docs/screenshots/live-grade.png)

### Firewall editor

![Firewall editor in change mode with a draft of three moves](docs/screenshots/firewall-draft-light.png)

<table>
<tr>
<td width="50%"><img src="docs/screenshots/firewall-preview-light.png" width="100%" alt="Plain-language preview"><br><sub>Preview: what changes, what it may and may no longer do, what to watch out for</sub></td>
<td width="50%"><img src="docs/screenshots/firewall-progress-dark.png" width="100%" alt="Change in progress"><br><sub>Backed up, changed, watched; undo for 24 hours</sub></td>
</tr>
</table>

### Controller configuration (read only)

![Firewall tab with a live controller configuration: zones, networks and clients](docs/screenshots/firewall-live-light.png)

<table>
<tr>
<td width="50%"><img src="docs/screenshots/firewall-rules-dark.png" width="100%" alt="Zone matrix and firewall rules with hit counters"><br><sub>Zone matrix and rules in the controller's order, with hit counters</sub></td>
<td width="50%"><img src="docs/screenshots/firewall-equipment-light.png" width="100%" alt="Network equipment with switch ports, wireless networks and port forwarding"><br><sub>Equipment with every port, wireless networks, port forwarding</sub></td>
</tr>
</table>

### Hypervisor (read only)

![Component card of a hypervisor node: load, findings, guests with their newest backup, storage](docs/screenshots/hypervisor-node-dark.png)

<table>
<tr>
<td width="50%"><img src="docs/screenshots/hypervisor-guest-light.png" width="100%" alt="Component card of a guest: node, network cards, backup, traffic of the last hour, guest firewall"><br><sub>A guest: node, network cards, backup, traffic, its firewall</sub></td>
<td width="50%"><img src="docs/screenshots/hypervisor-firewall-light.png" width="100%" alt="Firewall tab with the hypervisor source: findings and firewall at datacenter, node and guest level"><br><sub>Firewall tab: findings, firewall at every level, guest network cards</sub></td>
</tr>
</table>

### Data paths

![Data paths: world map with arcs from the home country, key figures and the period switch](docs/screenshots/paths-light.png)

<table>
<tr>
<td width="50%"><img src="docs/screenshots/paths-full-dark.png" width="100%" alt="World map in full screen"><br><sub>Full screen with period switch and legend; Esc or back closes it</sub></td>
<td width="50%"><img src="docs/screenshots/paths-lists-light.png" width="100%" alt="Unusual behaviour, countries and devices"><br><sub>Calm hints, countries and devices; a click selects</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/screenshots/paths-zoom-dark.png" width="100%" alt="Zoomed world map around the home country"><br><sub>Zooming with the mouse wheel or a pinch: nearby countries get their own labels</sub></td>
<td width="50%"><img src="docs/screenshots/netflow-settings-light.png" width="100%" alt="NetFlow / IPFIX receiver settings"><br><sub>NetFlow / IPFIX from the router: allowed exporters, port and receiver state</sub></td>
</tr>
</table>

Country data: [IP Geolocation by DB-IP](https://db-ip.com) (CC BY 4.0). Base map: Natural Earth (public domain).

### Nightly report

![Nightly report: headline, sections with points to tick off, and the archive](docs/screenshots/reports-light.png)

<table>
<tr>
<td width="50%"><img src="docs/screenshots/reports-dark.png" width="100%" alt="Nightly report in the dark theme"><br><sub>Things that need a look can be ticked off; notes stay calm</sub></td>
<td width="50%"><img src="docs/screenshots/report-settings-light.png" width="100%" alt="Report time, time zone, archive and optional delivery"><br><sub>Report time, archive and optional delivery (off by default)</sub></td>
</tr>
</table>

### Notifications

![Settings, Notifications: rules per event with when, from which severity and on which channel](docs/screenshots/notify-rules-light.png)

<table>
<tr>
<td width="50%"><img src="docs/screenshots/notify-maintenance-light.png" width="100%" alt="Planned maintenance: one-off or weekly, for all or some devices"><br><sub>A maintenance window pauses notifications and mark outages as maintenance</sub></td>
<td width="50%"><img src="docs/screenshots/notify-history-dark.png" width="100%" alt="History of what the rules did with each event"><br><sub>History: sent, bundled, quiet hours, maintenance, flap, cool-down</sub></td>
</tr>
</table>

Calm by default: every event goes into the nightly report. Each person chooses per event *immediately*, *bundled every hour* or *nightly report only*, from which severity on, and on which channel – web push on their own devices (with VAPID, per device, removable) or the shared e-mail, webhook, push topic and chat bot channels. Quiet hours let only critical immediate events through; planned maintenance (one-off or weekly, for all or some devices) pauses everything; flap protection, a 12-hour cool-down, an all-clear only after an announced outage and a daily limit keep it quiet. An optional heartbeat calls an address of yours so an outside service notices when Lanloom itself is down. Details: [docs/NOTIFICATIONS.md](docs/NOTIFICATIONS.md).

### Users and sign-in security

![Settings, Users: roles, second factor and last sign-in](docs/screenshots/access-users-light.png)

<table>
<tr>
<td width="50%"><img src="docs/screenshots/access-audit-dark.png" width="100%" alt="Audit log with filters and chain check"><br><sub>Audit log: who did what, when and from where, with a checkable chain</sub></td>
<td width="50%"><img src="docs/screenshots/access-second-step-light.png" width="100%" alt="Second step of the sign-in"><br><sub>Second step: app code, recovery code or passkey</sub></td>
</tr>
</table>

### Home-automation bridge

The card of the hub shows what the bridge reads, which private data stays out (people, trackers, zones, cameras, calendars and notifications by default), how it spares the hub (no template rendering, budgets per minute, paused lookups after errors) and the busiest automations; a runaway automation and scripts that call each other in a loop are marked and go into the nightly report. In admin mode operators can see the last runs of an automation. See [docs/HUB-BRIDGE.md](docs/HUB-BRIDGE.md).

<table>
<tr>
<td width="50%"><img src="docs/screenshots/hub-bridge-light.png" width="100%" alt="The bridge section on the hub's card: mode, privacy, sparing the hub, busiest automations"><br><sub>What is read, what stays out, how the hub is spared</sub></td>
<td width="50%"><img src="docs/screenshots/hub-runs-dark.png" width="100%" alt="Last runs of an automation, a runaway automation and a script loop"><br><sub>Runaway automation, last runs, a script loop</sub></td>
</tr>
</table>

### Search and commands

Press `Ctrl K`, `⌘ K` or `/` anywhere, or use the Search button. Views and commands are matched in the page; devices, services, automations and hub entities are searched on the server. A comparison such as `> 20 °C` filters hub values, and the arrow to the right (or the dots on a phone) opens the actions of a hit.

<table>
<tr>
<td width="50%"><img src="docs/screenshots/palette-search-light.png" width="100%" alt="The search box with hits grouped into devices, services, automations and hub entities"><br><sub>Everything in one box, grouped by what it is</sub></td>
<td width="50%"><img src="docs/screenshots/palette-values-dark.png" width="100%" alt="The value filter listing hub sensors above 20 degrees"><br><sub>A comparison filters hub values: highest first</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/screenshots/palette-actions-light.png" width="100%" alt="The actions of a device hit: open and copy address"><br><sub>The actions of a hit; only what exists is offered</sub></td>
<td width="50%"></td>

### One component, radio networks, sensors

Devices the hub also knows are drawn as one component with a network side and a hub side (matched by hardware address, by the address of their configuration page, or suggested by name); the hub's radio networks become dashed lanes with their coordinator, and every radio device is red when its radio has not heard it. The Sensors tab lists every sensor with search, shows what needs a visit (batteries now or soon, silent radio devices) and opens a large chart per sensor for 1 hour up to 30 days. See [docs/HUB-BRIDGE.md](docs/HUB-BRIDGE.md#one-component-network-side-and-hub-side).

<table>
<tr>
<td width="50%"><img src="docs/screenshots/radio-networks-dark.png" width="100%" alt="The map with one tile per radio network on dashed lanes from the hub"><br><sub>Radio networks as lanes from the hub</sub></td>
<td width="50%"><img src="docs/screenshots/hub-merged-light.png" width="100%" alt="Card of a device with the hub side: evidence of the match, room, firmware, entities"><br><sub>One component: the hub side on the card</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/screenshots/onsite-light.png" width="100%" alt="On site: batteries to change now or soon and silent radio devices"><br><sub>On site: batteries and silent radio devices</sub></td>
<td width="50%"><img src="docs/screenshots/sensor-page-dark.png" width="100%" alt="Sensor page with a minimum-to-maximum band and the mean over 7 days"><br><sub>Sensor page: 7 days from the hub's statistics</sub></td>
</tr>
</table>

### Backup and moving to another host

Settings › Backup saves the whole configuration into one file encrypted with your passphrase (Argon2id, AES-256-GCM): accounts and second factors, connections with their credentials, every name you set, "connected via" entries, drafts, settings and the audit log, optionally history. Automatic backups run on a schedule into the data volume, keep as many files as you choose, and the nightly report says when the newest one is too old. A restore shows first what it replaces, ends every session and works on a new host too, from the first-start page and across architectures (amd64 ↔ arm64). See [docs/BACKUP.md](docs/BACKUP.md).

<table>
<tr>
<td width="50%"><img src="docs/screenshots/backup-settings-light.png" width="100%" alt="Settings, Backup: back up now and automatic backups"><br><sub>Back up now or on a schedule</sub></td>
<td width="50%"><img src="docs/screenshots/backup-restore-preview-light.png" width="100%" alt="Restore preview comparing now and the backup"><br><sub>Before a restore: what is replaced, what stays, who is signed out</sub></td>
</tr>
</table>

### Storage, counter check and a light map

Settings › Storage shows how much space each kind of data takes and how long it is kept – live history, data paths per hour and per day, availability and events, reports, audit log, scan history – each adjustable. A nightly clean-up removes what is past its retention and compacts the database; the nightly report warns before the disk runs full. Every hour a counter check compares meters that see the same traffic (the router's uplink with NetFlow, this host's interfaces with its connections) and reports traffic counted twice or not at all. The map draws at most 30 frames per second, slows down by itself on a weak device, stops when nobody looks, and is fetched only when something changed. See [docs/PERFORMANCE.md](docs/PERFORMANCE.md).

<table>
<tr>
<td width="50%"><img src="docs/screenshots/storage-light.png" width="100%" alt="Settings, Storage: space per area, retention per kind of data, nightly clean-up, memory"><br><sub>Space per area, retention, nightly clean-up</sub></td>
<td width="50%"><img src="docs/screenshots/counter-check-dark.png" width="100%" alt="Source view with the counter check and one hour counted twice"><br><sub>Counter check in the source view</sub></td>
</tr>
</table>

### Colour themes

Settings > Appearance > Colour theme switches the accent palette of the whole app: the map, the traces, the data paths arcs, the firewall views, the charts and the live grades. **Circuit green** is the default and looks as before; **Aurora** uses turquoise, blue and violet on a cool ground. Every theme works in light, dark and automatic mode, and the choice is remembered per device. Warning and error colours keep their meaning in every theme, and the text and graphic contrast of each theme and mode is checked against WCAG AA in the test suite.

<table>
<tr>
<td width="50%"><img src="docs/screenshots/theme-circuit-dark.png" width="100%" alt="The map in the Circuit green theme"><br><sub>Circuit green (default)</sub></td>
<td width="50%"><img src="docs/screenshots/theme-aurora-dark.png" width="100%" alt="The same map in the Aurora theme"><br><sub>Aurora, the same view</sub></td>
</tr>
</table>

![Colour theme setting with a preview of each theme](docs/screenshots/colour-theme-picker-light.png)

### AI

![Settings, AI: local and cloud services, order and privacy](docs/screenshots/ai-settings-light.png)

<table>
<tr>
<td width="50%"><img src="docs/screenshots/ai-explain-dark.png" width="100%" alt="Explain dialog with the answer, the service and the response time"><br><sub>Explain: a short answer, and where it was made</sub></td>
<td width="50%"><img src="docs/screenshots/ai-preview-light.png" width="100%" alt="Preview of the exact text that is sent, with placeholders"><br><sub>Preview what is sent: placeholders instead of addresses and names</sub></td>
</tr>
</table>

![Another computer: one-time setup code and one command per operating system](docs/screenshots/ai-other-computer-light.png)

![Ask your network: answers with links to the devices and views concerned](docs/screenshots/ask-light.png)

<table>
<tr>
<td width="50%"><img src="docs/screenshots/report-plain-dark.png" width="100%" alt="Nightly report with the paragraph In plain words above the rule-based report"><br><sub>In plain words: an AI paragraph above the report, which stays the reference</sub></td>
<td width="50%"><img src="docs/screenshots/identify-suggestion-light.png" width="100%" alt="AI suggestion for an unknown device with its reason, confirm or dismiss"><br><sub>Unknown device: a suggestion with its reason; nothing changes until you confirm</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/screenshots/firewall-ai-suggestions-dark.png" width="100%" alt="AI suggestions for moving devices to other networks"><br><sub>Firewall: suggestions to pick from</sub></td>
<td width="50%"><img src="docs/screenshots/firewall-ai-draft-light.png" width="100%" alt="The chosen suggestions in the draft bar, waiting for the preview"><br><sub>Taken over as a draft: the usual preview and confirmation follow</sub></td>
</tr>
</table>

### Devices and settings

![Device list](docs/screenshots/devices-light.png)

![Settings](docs/screenshots/settings-light.png)

### On the phone

<table>
<tr>
<td><img src="docs/screenshots/phone-overview.png" width="200" alt="Phone: overview start page"></td>
<td><img src="docs/screenshots/phone-rail.png" width="200" alt="Phone: the Right now rail"></td>
<td><img src="docs/screenshots/phone-map-light.png" width="200" alt="Phone map, light"></td>
<td><img src="docs/screenshots/phone-map-dark.png" width="200" alt="Phone map, dark"></td>
<td><img src="docs/screenshots/phone-detail.png" width="200" alt="Phone component card"></td>
<td><img src="docs/screenshots/phone-connection.png" width="200" alt="Phone: connection of a device"></td>
<td><img src="docs/screenshots/phone-identity.png" width="200" alt="Phone: two addresses that may be one device"></td>
<td><img src="docs/screenshots/phone-devices.png" width="200" alt="Phone device list"></td>
<td><img src="docs/screenshots/phone-paths.png" width="200" alt="Phone: data paths"></td>
<td><img src="docs/screenshots/phone-paths-full.png" width="200" alt="Phone: world map in full screen"></td>
<td><img src="docs/screenshots/phone-netflow-settings.png" width="200" alt="Phone: NetFlow / IPFIX receiver settings"></td>
<td><img src="docs/screenshots/phone-onsite.png" width="200" alt="Phone: sensors on site, batteries and silent radio devices"></td>
<td><img src="docs/screenshots/phone-sensor-page.png" width="200" alt="Phone: sensor page with the course of one sensor"></td>
<td><img src="docs/screenshots/phone-reports.png" width="200" alt="Phone: nightly report"></td>
<td><img src="docs/screenshots/phone-storage.png" width="200" alt="Phone: storage settings"></td>
<td><img src="docs/screenshots/phone-ai-explain.png" width="200" alt="Phone: AI explanation of a device"></td>
<td><img src="docs/screenshots/phone-ask.png" width="200" alt="Phone: Ask your network"></td>
<td><img src="docs/screenshots/phone-palette-search.png" width="200" alt="Phone: search and commands"></td>
<td><img src="docs/screenshots/phone-palette-values.png" width="200" alt="Phone: value filter in the search"></td>
<td><img src="docs/screenshots/phone-report-plain.png" width="200" alt="Phone: nightly report in plain words"></td>
<td><img src="docs/screenshots/phone-wizard-scan.png" width="200" alt="Phone wizard scan step"></td>
<td><img src="docs/screenshots/phone-firewall-move.png" width="200" alt="Phone: move a device to another network"></td>
<td><img src="docs/screenshots/phone-firewall-preview.png" width="200" alt="Phone: firewall preview"></td>
<td><img src="docs/screenshots/phone-firewall-rules.png" width="200" alt="Phone: firewall rules read from the controller"></td>
</tr>
</table>

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

<table>
<tr>
<td><img src="docs/screenshots/identity-light.png" width="380" alt="Device card with maker, model, firmware and a second interface, light"></td>
<td><img src="docs/screenshots/identity-dark.png" width="380" alt="The same card, dark"></td>
</tr>
</table>

## Live data and integrations

Dots appear only where bytes are actually measured:

- **This host's own TCP flows** come from the kernel's socket diagnostics (netlink) when running with host networking. No extra privileges needed. They also feed the data paths world map.
- **NetFlow v5 / v9 / IPFIX** from your router or firewall (optional, off by default, UDP 2055): the traffic of every device for the data paths world map. Only packets from the exporter addresses you allow are read; only country, device and bytes are kept. See [docs/DATA-PATHS.md](docs/DATA-PATHS.md#setting-up-netflow--ipfix).
- **Router or switch (SNMP v2c or v3)** gives interface counters, which animate the internet uplink, and LLDP neighbours plus the switch address table for the cabling. Only Get, GetNext and GetBulk are used.
- **Switch controller** (the local controller application of a switch and access point product line) tells which client sits on which switch port or access point. A local view-only account is enough. Lanloom signs in and then only reads two lists; one code path makes every request and refuses everything else.
- **Device-family adapters** read a controller's configuration and state through its local APIs (an API key and/or a local view-only account): zones, networks, firewall rules with hit counters, ports, wireless networks, clients and their ports. They only send read requests through one allow-listed code path, refuse an account that can change settings where that is checkable, and cap sign-ins. A hypervisor adapter reads nodes, guests, their network cards, bridges, firewall, storage and backups with a token that can only read (checked against the token's own permission list). Which families exist: [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md).
- **MQTT broker** topics are grouped; Lanloom only counts messages and lengths per group. It connects, subscribes to `#`, and cannot publish. Payloads are never stored. TLS is optional.
- **Home-automation hub (WebSocket API)** adds rooms, devices and entities, state changes and automation runs, using a long-lived access token (a non-admin user is enough). A strict allow-list limits it to read commands; it cannot call services and never renders templates. Optional **admin mode** also reads which entities each automation and script uses and, on request, their last runs – within budgets per minute, never looking up scripts that call each other in a loop. People, trackers, zones, cameras, calendars and notifications are kept out by default. See [docs/HUB-BRIDGE.md](docs/HUB-BRIDGE.md).

Supported integrations: [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md).

All integrations are optional. Credentials are encrypted at rest with a key file (`secret.key`, mode 0600) in the data volume, never displayed again and never logged.

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

## Security and privacy

Sign-in is mandatory; there is no open mode. The first account can only be created with the setup code. Further people join by one-time invitation with a viewer, operator or admin role, enforced on the server for every route; a second factor (authenticator app or passkey) is required for admins by default, critical actions ask for a fresh confirmation, and every change is written to a tamper-evident audit log ([docs/ACCESS.md](docs/ACCESS.md)). Sign-in through your own authentication proxy is optional and off by default. Passwords use argon2id, sessions use random 256-bit tokens in `HttpOnly`, `SameSite=Strict` cookies, and state-changing requests are protected against cross-site use. The container runs as an unprivileged user with a read-only root file system and no capabilities. There is no telemetry. The optional update check against the public release feed is the only outbound connection of its own; everything else happens only when you switch it on: the delivery of the nightly report summary and of notifications (web push to the push service of your own browsers), the optional heartbeat to an address you enter, a cloud AI service (which only ever receives placeholders instead of addresses and names) and the download of the local AI service and its models. Countries on the data paths map come from a database inside the image; no address is ever looked up online, and only countries and byte counts are stored. Put Lanloom behind a TLS reverse proxy for passkeys and remote access (list it in `LANLOOM_TRUSTED_PROXIES`, see [docs/INSTALL.md](docs/INSTALL.md#behind-a-reverse-proxy)), and never expose it directly to the internet. Details and how to report a vulnerability: [SECURITY.md](SECURITY.md).

## Updating

- **With the updater sidecar:** an "Update" button appears when a new version is available. It pulls the image, runs a self-test without network, starts a trial on a copy of your data, backs up the database and tags the previous image, swaps the container, checks health, and rolls back automatically on failure.
- **Without it:** `docker compose pull && docker compose up -d`.

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

- [docs/manual/views.md](docs/manual/views.md): the views one by one (overview, map, network, data paths, devices, firewall, nightly report, settings), the rails, keys, live state and deep links
- [docs/INSTALL.md](docs/INSTALL.md): installation on each platform, configuration, backup, moving to another host, uninstall
- [docs/BACKUP.md](docs/BACKUP.md): backup and restore in the app, automatic backups, moving to another host, the file format
- [docs/NOTIFICATIONS.md](docs/NOTIFICATIONS.md): notification rules per person, quiet hours, planned maintenance, flap protection, web push, the heartbeat
- [docs/ACCESS.md](docs/ACCESS.md): users and roles, invitations, two-step sign-in, sessions, audit log, authentication proxy, sub-path
- [docs/DISCOVERY.md](docs/DISCOVERY.md): what the scan asks, maker and model recognition, the address register, one device with several interfaces
- [docs/HUB-BRIDGE.md](docs/HUB-BRIDGE.md): the home-automation bridge: what is read, admin mode, sparing the hub, privacy filter
- [docs/TOPOLOGY.md](docs/TOPOLOGY.md): physical topology, sources, view-only account, how shared ports are read
- [docs/DATA-PATHS.md](docs/DATA-PATHS.md): the data paths world map, what is counted where, unusual behaviour, privacy, country data ([IP Geolocation by DB-IP](https://db-ip.com), CC BY 4.0)
- [docs/PERFORMANCE.md](docs/PERFORMANCE.md): hardware requirements with measured values, storage and retention, the nightly clean-up, the counter check, the frame budget of the map
- [docs/NIGHTLY-REPORT.md](docs/NIGHTLY-REPORT.md): the nightly report, its sections, the archive, optional delivery, adding a section
- [docs/PALETTE.md](docs/PALETTE.md): search and commands: keys, what is searched, the value filter, actions, adding a command
- [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md): supported integrations and AI services, what each can read and write
- [docs/integrations/ai.md](docs/integrations/ai.md): AI, local or cloud: setup in three ways, privacy layer, hardware, troubleshooting
- [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md): how device-family adapters are built (read and write halves, capabilities, plan steps, credentials)
- [docs/FIREWALL-EDITOR.md](docs/FIREWALL-EDITOR.md): the firewall editor, the executor service and its safety checks
- [docs/FAQ.md](docs/FAQ.md): frequently asked questions
- [docs/ARCHITECTURE-CORE.md](docs/ARCHITECTURE-CORE.md): how the board engine is structured as a versioned core
- [SECURITY.md](SECURITY.md): security model and reporting
- [CHANGELOG.md](CHANGELOG.md): release notes
- [CONTRIBUTING.md](CONTRIBUTING.md): ideas, bug reports and adapter requests (as issues)

## License

Lanloom is **source-available**, not open source. You may run it for personal or internal use on machines you own or control and read the source. Modification and redistribution require prior written permission. It is provided without warranty of any kind. See [LICENSE](LICENSE). Third-party components and their licenses are listed in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

© 2026 Robert Manuwald
