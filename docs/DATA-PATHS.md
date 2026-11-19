# Data paths

The **Data paths** tab shows where the devices of your network send data on the internet, by country, on a calm world map.

## What you see

- **Arcs** from your home country to every destination country. The width follows the bytes of the chosen period. Destinations outside the EU are amber, those in the EU teal. Dots travel along the arcs in the direction of most of the traffic.
- **Period:** 24 hours (hourly sums), 7 days or 30 days (daily sums).
- **Key figures:** data exchanged with the internet, number of countries, share outside the EU, number of devices, number of hints.
- **Lists** of countries and devices. Click a country (on the map or in the list) to open its [country detail](#country-detail); click a device to see its countries. The map follows the selection. Esc or the × on the chip clears it.
- **Full screen:** the button at the top right of the map. Esc, the close button and the back gesture or key close it without leaving the view. On a phone held upright the map is wider than the screen and can be moved sideways; it starts at the home country.
- **Zooming:** the mouse wheel inside the map zooms in and out at the pointer, up to 16 times; outside the map, or when it is fully zoomed out, the page scrolls as usual. Drag to move the zoomed map (a drag never selects a country). On a phone, pinch with two fingers; one finger moves the map only while it is zoomed, otherwise the page scrolls. A double click or the reset button at the top left shows the whole world again. With the keyboard, Tab to the map, then `+` and `-` zoom and `0` resets. Lines, dots and labels keep their size on screen, and the labels are laid out again for every zoom level, so neighbouring countries get their own label once you zoom in. The zoom works in the full screen too and stays when the map refreshes every minute.
- **Overseas parts:** a country lights up only with its main area. A territory with its own country code (French Guiana, Réunion, Guadeloupe and others) is a shape or a dot of its own, and far-away parts of a country without an own code are drawn but never lit with it.

## Country detail

Click a country on the map or in the list of countries and a large sheet opens above the view (on a phone it fills the screen). Esc, the × button, a click beside it and the back gesture or key close it without leaving the view; the address while it is open is `#/paths/<country code>` (for example `#/paths/DE`), so the sheet can be bookmarked and opened from the command palette (*Data paths of …*, typed). The period switch of the sheet is the same as the one of the view. A click on *Anycast* opens no sheet: it is not a country.

- **Map** of the country, zoomed to it, with a finer base map (Natural Earth 1:50m, a file of the app itself that is loaded when the sheet opens, never from another server) and well-known places for orientation that appear with every zoom step and never lie over a label. The mouse wheel (or two fingers) zooms up to 64 times, dragging moves, a double click or the reset button returns to the start view. Lines and bubbles keep their size on the screen at every zoom step and are drawn smaller on a small map.
- **Bubbles** are the places the devices talk to, sized by the amount. **The sum rule:** the bubbles, the list of devices and the figure at the top always add up to the same number, the bytes exchanged with the country in the period. Traffic for which no city is known is not dropped: it is drawn as a dashed bubble (*approximate position*), at the largest city Lanloom knows for the same operator in that country, or in the middle of the country when there is none. Bubbles beyond the sixty biggest are merged into that middle bubble.
- **Key figures:** received and sent, share of all traffic with the internet, devices, operators, cities. Sizes use the unit that fits (B, kB, MB, GB, TB) in the number format of the language.
- **History** per hour (24 hours) or per day (7 and 30 days), sent in the darker part.
- **Cities**, **Network operators** (the number in the key figures is the number of rows in the list; more than 40 show *Show all*), **Devices** (which device talks to the country, sent and received, with its operators and cities) and **Destinations** (*operator · city*, for example *Example Cloud · Ashburn*; *location unknown* when no city is known).
- Nothing on the sheet is an address. Cities and operators exist only when the option below is on; the sheet says so when they are off, loading, or could not be fetched, and then still shows everything about the country itself.

## Cities and network operators (an option, off by default)

Lanloom's rule is that nothing leaves your house unless you switch it on. The country data is part of the image; the city data is about 90 MB, so it is not. **Settings → General → Cities and network operators** (administrators) explains exactly what happens and switches it on:

- Lanloom fetches two files from the public source *download.db-ip.com*: the city list (*IP to City Lite*) and the operator list (*IP to ASN Lite*), both [IP Geolocation by DB-IP](https://db-ip.com), licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). It checks at most **once a day** and loads a new file **once a month** (the file of a month is expected from the 2nd of that month; if the newest is missing, the month before is tried). The request carries no address, no name and nothing about the installation. This is the only connection of this feature and it is listed in `security/egress.json` (category *optional-download*).
- The files are **read as a stream** and condensed into one file of about 90 megabytes in the data folder (`geo/cities-YYYY-MM.bin`; 194,000 cities, 83,000 operators, 7.8 million source rows in the 2026-11 files), which is mapped read-only, so looking a place up costs no memory (15 microseconds on a single-board computer). A build needs free space for the download of both files and the temporary arrays (600 MB are required before it starts, about 200 MB are used) and removes its temporary files when it ends, however it ends. Measured on a single-board computer under load: about 40 minutes for the first build with memory tracing on (a few minutes without), a peak of 41 MB of Python heap and 160 MB resident memory.
- **Checked before the swap:** size window of each download, complete gzip (a cut-off file fails at its end), enough rows, ascending ranges, well-known samples. Only then the new file replaces the old one; the previous file stays as the fallback and older files are removed. A failure leaves the old file in use (or no file: the sheet then says *not available*, never 0), and the next try is the next day. The settings card shows state, month, counts, size and the last problem; **Check now** asks for an immediate check; **Delete files and stored figures** removes the files and every stored city and operator figure and switches the option off. The nightly report notes a new file or a failed fetch.
- **What is stored** (only with the option on): per hour and day, the device, the country, the city (name and region of the city centre **of the peer**, never a position of yours), the operator (AS number and name) and the bytes. At most 250 cities and 250 operators per country and day are kept apart; more are condensed into *other*. Anycast, own networks and unassigned traffic never get a city or an operator. A city that the file puts into another country than the country data does is dropped (the country wins). Addresses, host names and NetFlow records are not stored; an address is looked up in memory and forgotten (a bounded cache in memory only helps repeated lookups).
- **Approximate by nature:** the source assigns whole networks to one place, often the seat of a data centre, not the server itself. Treat cities as a hint.

## Where does it talk to? (on every card)

The card of a device has the section **Where does it talk to?** with the period switch (24 hours, 7 days, 30 days, the same choice as the map): *On the internet* lists the countries with the bytes sent and received and whether the country is in the EU (own networks and tunnels, anycast and traffic without a country are listed as their own lines), and *In your network* lists the devices it talks to, by name, with the bytes in each direction. A click on a name opens that device's card. The card of the **Internet** tile has *Who talks to the internet?*: every device that exchanged data with the internet in the period, with its sums and the number of countries. Route: `GET /api/paths/device/<id>?window=` (`internet` for the tile).

## Who talks to whom (on the map)

On the map and on the network board the chip **Who talks to whom** (first in the row of filters; `#/map?talk=1`) draws dashed grey lines in the air between the tiles of two devices that talk to each other, for the last 24 hours. The width follows the bytes. They are not cables: nothing is claimed about the wiring, and the lines of the open card light up while the rest fades. The chip shows the number of conversations. Without conversations it says so and points to NetFlow. Route: `GET /api/paths/lan?window=`.

Conversations come from the flows your router or firewall exports (see below), so they show only what crosses the router: traffic between networks (VLANs), and traffic to the router itself. **Devices that talk inside one switch never reach the router, and no flow exists for it.** Only flows whose two ends are both known devices are counted; an address that is not a known device never becomes a line. The conversations are not data paths, are not counted in the figures of the internet and not in the EU share, and the Lanloom host's own traffic is left out here as everywhere in flows.

## What the adapter statistics can and cannot do

Before the flow receiver was extended, the statistics of the controller adapters were checked as a second source for traffic by destination. The result for the read adapter of the switch controller family (checked against the recording of a real console and the published specifications of three releases): the client lists carry **byte totals per client and per port**, but no destination, no country and no peer; the official integration API offers the application and category **catalogues** and the country list, but no statistics; the classic per-client application statistics count by application category, not by country, are not part of the allow-list of the read adapter and need a view-only account plus DPI switched on in the controller. None of them answers *which country*, and an application category cannot be turned into a country honestly. So the adapters are not fed into the data paths; **NetFlow / IPFIX from the gateway is the source for traffic by destination and for conversations between devices**. Per-client totals stay what they are: the live traffic of the map and the counter check.

## What is counted where

Every connection partner beyond your local network falls into exactly one bucket:

| Bucket | What | Counted in the EU share? |
| --- | --- | --- |
| A country | the country from the offline database | yes |
| Own networks and tunnels | the address range used by overlay VPNs (`100.64.0.0/10`), special and reserved ranges, and every network you list under *Settings → Data paths → Own networks and tunnels* (for example a second site or a VPN provider) | no, shown separately |
| Anycast (nearest location) | well-known anycast networks: the published ranges of the largest content delivery network (also its time and DNS services) and the networks of three public DNS resolvers. They answer from the nearest of many locations worldwide, while the database only knows the country a block is registered in, so a download from a server next door would otherwise be booked under a far-away country. The list ships with the image and is renewed with releases; a network you list as your own wins | no, its own line in the countries list (click it to see which devices use it) |
| Without a country | a valid address the database has no country for | no, shown separately |

The share outside the EU therefore only refers to traffic with a known country; anycast is never part of it. If there is none, it shows **no data** instead of 0 %. Partners inside your local network are not data paths and are not counted.

Without loaded country data nothing is booked at all, so a start-up race can never turn real traffic into "without a country".

## Hints

Hints are calm and never change anything. Lanloom never creates a firewall rule from them.

- **New country:** a device talks to a country for the first time in 30 days, with at least 1 MB in 24 hours. Only after the device has three days of history, so a fresh installation does not flood you with "new".
- **Unusually much:** a device uploaded at least three times its typical day (median of up to 14 previous days, at least 3 of them with data) and at least 200 MB in the last 24 hours.
- **Network without internet:** a device in a network listed under *Settings → Data paths → Networks without internet* exchanged data with the internet (own tunnels do not count).

## Sources

The footer of the view names the source of the numbers.

- **Connections of this host** (always on): the TCP connections of the Lanloom host itself, read from the kernel's socket diagnostics with host networking.
- **NetFlow / IPFIX from your router** (off by default): your firewall or router reports the traffic of every device on the network, and Lanloom books it into the same buckets, per device. The footer then says *connections of this host and NetFlow from* the exporter address.

### Setting up NetFlow / IPFIX

1. In Lanloom, open *Settings → NetFlow / IPFIX from your router*. Enter the router's address under **Allowed exporter addresses** (required; up to 8, one per line; while the field is empty, a button offers the gateway Lanloom detected), keep the UDP port **2055** or choose another one from 1024 to 65535, tick **Receive NetFlow / IPFIX** and save. Without an allowed exporter the receiver cannot be switched on: packets from any other address are dropped and only counted, so no device in the network can send made-up flows.
2. On the router or firewall, enable flow export (often called NetFlow, IPFIX, flow monitoring or traffic accounting) to the address of the Lanloom host and that port. Supported are NetFlow v5 (IPv4 only), NetFlow v9 and IPFIX (both with IPv6). Where you can choose:
   - export from the **LAN side, before address translation**, so the records carry the devices' own addresses (after translation every device looks like the router);
   - set the **active timeout** to about 60 seconds. Most routers send a flow only when the connection ends or the active timeout runs out, so without it a long download or a video stream shows up late and in one piece;
   - some firewalls export only traffic of rules that have logging enabled; enable it on the rule from the LAN to the internet.
3. The settings card shows the receiver's state: the port it listens on, the last packet, flows received and booked, packets dropped because they came from another address, and records that wait for a template (NetFlow v9 and IPFIX send the description of their records every few minutes; until it arrives, records cannot be read).

With the standard `compose.yaml` Lanloom uses host networking, so the port is open on the host directly; allow it in a host firewall if you use one. With a bridge network instead, publish it: `ports: ["2055:2055/udp"]`. No second container is needed.

How flows are booked: a flow from a local address to an address on the internet counts as upload of that device, the reverse as download. Local addresses are the networks Lanloom detects on the host, private and unique-local ranges, every address of a known device (also its IPv6 addresses) and the networks you add under *Extra own networks* (for example your public IPv6 prefix). An address no device is known for is booked as **Unknown device**. Traffic between two local devices is not a data path and is not counted. The Lanloom host's own traffic is skipped in flows, because the host source above already counts it exactly. An exporter that samples (looks at only 1 of N packets) says so in the NetFlow v5 header, in the v9/IPFIX sampling interval of a record or in its options data (also IPFIX packet interval and space); Lanloom scales the bytes up by that rate.

## Privacy

Stored are only country, device id and bytes per hour (49 hours) and per day (31 days) (with the option for cities and network operators also the city and the operator, see above), plus the first and last day a device talked to a country, and for conversations between two devices only the two device ids and the bytes per direction (same retention). No peer addresses, ports, protocols, host names or visited sites are kept, also not from NetFlow records (they are read in memory and dropped). Nothing is looked up online.

## Country data and map

- Country data: the free *IP to Country Lite* database, [IP Geolocation by DB-IP](https://db-ip.com), licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). It ships in the image in a compact form (`lanloom/data/geo-country.bin.gz`) and is renewed with a release: download the monthly CSV and run `python3 -I scripts/build_geodb.py dbip-country-lite-YYYY-MM.csv.gz`. The script checks the input (complete, ascending without gaps, enough rows, known samples) before it replaces the file.
- City and operator data (optional): *IP to City Lite* and *IP to ASN Lite* by DB-IP, CC BY 4.0, fetched only with the option on (see above); the attribution is shown in the country detail and in the settings card.
- Fine base map and places: Natural Earth 1:50m countries and *populated places* (public domain), `lanloom/web/js/worldmap_fine.js` and `worldplaces.js`, generated by `scripts/build_worldmap.py <110m countries> <50m countries> <10m populated places>`; the files are loaded when a country sheet opens. A test checks that every country code of the country data can be drawn and that the sizes stay in budget.
- Base map: Natural Earth (public domain), simplified with `scripts/build_worldmap.py`. The script also moves far-away parts out of a country's main shape (a territory with its own code becomes its own shape) and adds label points for territories the country data knows but the base map has no shape for; `--refine` repeats only this step on the generated file. A test checks that every country code of the country data can be drawn.

## Performance

The answer comes from hourly and daily sums with a 30-second cache. The dots run on a canvas only while the map is on screen and the tab is visible, never with "reduce motion", and only in the full screen while it is open. On devices without a graphics processor the drawing rate steps down by itself (every second or third frame) when frames come slowly, and steps back up after ten smooth seconds.
