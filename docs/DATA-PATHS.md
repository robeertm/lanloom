# Data paths

The **Data paths** tab shows where the devices of your network send data on the internet, by country, on a calm world map.

## What you see

- **Arcs** from your home country to every destination country. The width follows the bytes of the chosen period. Destinations outside the EU are amber, those in the EU teal. Dots travel along the arcs in the direction of most of the traffic.
- **Period:** 24 hours (hourly sums), 7 days or 30 days (daily sums).
- **Key figures:** data exchanged with the internet, number of countries, share outside the EU, number of devices, number of hints.
- **Lists** of countries and devices. Click a country to see which devices talk to it; click a device to see its countries. The map follows the selection. Esc or the × on the chip clears it.
- **Full screen:** the button at the top right of the map. Esc, the close button and the back gesture or key close it without leaving the view. On a phone held upright the map is wider than the screen and can be moved sideways; it starts at the home country.
- **Zooming:** the mouse wheel inside the map zooms in and out at the pointer, up to 16 times; outside the map, or when it is fully zoomed out, the page scrolls as usual. Drag to move the zoomed map (a drag never selects a country). On a phone, pinch with two fingers; one finger moves the map only while it is zoomed, otherwise the page scrolls. A double click or the reset button at the top left shows the whole world again. Lines, dots and labels keep their size on screen, and the labels are laid out again for every zoom level, so neighbouring countries get their own label once you zoom in. The zoom works in the full screen too and stays when the map refreshes every minute.
- **Overseas parts:** a country lights up only with its main area. A territory with its own country code (French Guiana, Réunion, Guadeloupe and others) is a shape or a dot of its own, and far-away parts of a country without an own code are drawn but never lit with it.

## What is counted where

Every connection partner beyond your local network falls into exactly one bucket:

| Bucket | What | Counted in the EU share? |
| --- | --- | --- |
| A country | the country from the offline database | yes |
| Own networks and tunnels | the address range used by overlay VPNs (`100.64.0.0/10`), special and reserved ranges, and every network you list under *Settings → Data paths → Own networks and tunnels* (for example a second site or a VPN provider) | no, shown separately |
| Without a country | a valid address the database has no country for | no, shown separately |

The share outside the EU therefore only refers to traffic with a known country. If there is none, it shows **no data** instead of 0 %. Partners inside your local network are not data paths and are not counted.

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

1. In Lanloom, open *Settings → NetFlow / IPFIX from your router*. Enter the router's address under **Allowed exporter addresses** (required; up to 8, one per line), keep the UDP port **2055** or choose another one from 1024 to 65535, tick **Receive NetFlow / IPFIX** and save. Without an allowed exporter the receiver cannot be switched on: packets from any other address are dropped and only counted, so no device in the network can send made-up flows.
2. On the router or firewall, enable flow export (often called NetFlow, IPFIX, flow monitoring or traffic accounting) to the address of the Lanloom host and that port. Supported are NetFlow v5 (IPv4 only), NetFlow v9 and IPFIX (both with IPv6). Where you can choose:
   - export from the **LAN side, before address translation**, so the records carry the devices' own addresses (after translation every device looks like the router);
   - set the **active timeout** to about 60 seconds. Most routers send a flow only when the connection ends or the active timeout runs out, so without it a long download or a video stream shows up late and in one piece;
   - some firewalls export only traffic of rules that have logging enabled; enable it on the rule from the LAN to the internet.
3. The settings card shows the receiver's state: the port it listens on, the last packet, flows received and booked, packets dropped because they came from another address, and records that wait for a template (NetFlow v9 and IPFIX send the description of their records every few minutes; until it arrives, records cannot be read).

With the standard `compose.yaml` Lanloom uses host networking, so the port is open on the host directly; allow it in a host firewall if you use one. With a bridge network instead, publish it: `ports: ["2055:2055/udp"]`. No second container is needed.

How flows are booked: a flow from a local address to an address on the internet counts as upload of that device, the reverse as download. Local addresses are the networks Lanloom detects on the host, private and unique-local ranges, every address of a known device (also its IPv6 addresses) and the networks you add under *Extra own networks* (for example your public IPv6 prefix). An address no device is known for is booked as **Unknown device**. Traffic between two local devices is not a data path and is not counted. The Lanloom host's own traffic is skipped in flows, because the host source above already counts it exactly. Sampling is not corrected.

## Privacy

Stored are only country, device id and bytes per hour (49 hours) and per day (31 days), plus the first and last day a device talked to a country. No peer addresses, host names or visited sites are kept, also not from NetFlow records (they are read in memory and dropped). Nothing is looked up online.

## Country data and map

- Country data: the free *IP to Country Lite* database, [IP Geolocation by DB-IP](https://db-ip.com), licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). It ships in the image in a compact form (`lanloom/data/geo-country.bin.gz`) and is renewed with a release: download the monthly CSV and run `python3 -I scripts/build_geodb.py dbip-country-lite-YYYY-MM.csv.gz`. The script checks the input (complete, ascending without gaps, enough rows, known samples) before it replaces the file.
- Base map: Natural Earth (public domain), simplified with `scripts/build_worldmap.py`. The script also moves far-away parts out of a country's main shape (a territory with its own code becomes its own shape) and adds label points for territories the country data knows but the base map has no shape for; `--refine` repeats only this step on the generated file. A test checks that every country code of the country data can be drawn.

## Performance

The answer comes from hourly and daily sums with a 30-second cache. The dots run on a canvas only while the map is on screen and the tab is visible, never with "reduce motion", and only in the full screen while it is open. On devices without a graphics processor the drawing rate steps down by itself (every second or third frame) when frames come slowly, and steps back up after ten smooth seconds.
