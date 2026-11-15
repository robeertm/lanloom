# Data paths

The **Data paths** tab shows where the devices of your network send data on the internet, by country, on a calm world map.

## What you see

- **Arcs** from your home country to every destination country. The width follows the bytes of the chosen period. Destinations outside the EU are amber, those in the EU teal. Dots travel along the arcs in the direction of most of the traffic.
- **Period:** 24 hours (hourly sums), 7 days or 30 days (daily sums).
- **Key figures:** data exchanged with the internet, number of countries, share outside the EU, number of devices, number of hints.
- **Lists** of countries and devices. Click a country to see which devices talk to it; click a device to see its countries. The map follows the selection. Esc or the × on the chip clears it.
- **Full screen:** the button at the top right of the map. Esc, the close button and the back gesture or key close it without leaving the view. On a phone held upright the map is wider than the screen and can be moved sideways; it starts at the home country.

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

The first version counts the connections of the Lanloom host itself, read from the kernel's socket diagnostics with host networking. Flow sources from the gateway follow in a later release and will feed the same buckets per device.

## Privacy

Stored are only country, device id and bytes per hour (49 hours) and per day (31 days), plus the first and last day a device talked to a country. No peer addresses, host names or visited sites are kept. Nothing is looked up online.

## Country data and map

- Country data: the free *IP to Country Lite* database, [IP Geolocation by DB-IP](https://db-ip.com), licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). It ships in the image in a compact form (`lanloom/data/geo-country.bin.gz`) and is renewed with a release: download the monthly CSV and run `python3 -I scripts/build_geodb.py dbip-country-lite-YYYY-MM.csv.gz`. The script checks the input (complete, ascending without gaps, enough rows, known samples) before it replaces the file.
- Base map: Natural Earth (public domain), simplified with `scripts/build_worldmap.py`.

## Performance

The answer comes from hourly and daily sums with a 30-second cache. The dots run on a canvas only while the map is on screen and the tab is visible, never with "reduce motion", and only in the full screen while it is open. On devices without a graphics processor the drawing rate steps down by itself (every second or third frame) when frames come slowly, and steps back up after ten smooth seconds.
