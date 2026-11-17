# Map

[Manual](README.md) · [Overview](overview.md) · Map · [Network](network.md) · [Devices](devices.md) · [Inventory](inventory.md) · [Firewall](firewall.md) · [Data paths](data-paths.md) · [Nightly report](nightly-reports.md) · [Automations](automations.md) · [Settings](settings.md) · [AI](ai.md)

## Purpose

Your network drawn like a circuit board: every device is a component, every connection a trace. It shows what is
there, what talks to what, and what is down, in one picture.

## What you see

![Map, light theme](../screenshots/map-light.png)

- **Components** for devices, hubs, controllers and the internet edge; the dot at a component is its live grade (see
  the pill at the top right).
- **Traces** for connections. Moving dots are measured bytes; nothing is animated that was not measured. The line
  styles tell the kind of link (cable, radio, service, flow, integration, uplink).
- **Zones** are the areas of the board, one per network or VLAN, titled with its name, VLAN number and range.
- **Chips above the board** filter by **area** (one zone of the board; the internet edge stays for orientation) or
  show **problems only** (components that are down or whose data stopped).
- **Hover window.** Rest the mouse on a tile for half a second: the traffic of the last hour and the availability of
  the last 24 hours.
- **Card.** A click on a component opens its card with identity, status, services, availability, traffic and what can be changed
  (operators).

![Map filtered to one area](../screenshots/map-filter-light.png)

## How to use it

- Click a component to open its card; the address follows (`#/map/<component>`), so the card can be shared and the
  back key closes it.
- Use the area chips to look at one network or one cabinet at a time (`#/map?zone=<area>`), or *Problems only*
  (`#/map?only=problems`).
- Magnify the board with the mouse wheel or two fingers.
- Search (`Ctrl K`) finds components and devices and offers actions such as *Show availability* or *Show traffic
  history*.
- On the card, operators can rename a device, correct its type, hide it from the map, join two interfaces of one
  device, set a protection class for the scan, and set a manual connection.
- The card sections (availability, traffic, connections, *Simulate maintenance*) fold and keep their state while the
  page updates.

![The card of a device](../screenshots/detail-light.png)

## Limits

- Dots show measured bytes only. A trace without measurement stays still; it does not mean there is no traffic.
- Where no cabling is known, traces run from this host's point of view directly; the [Network](network.md) view shows
  the physical order when sources for it exist.
- Large networks are drawn with many traces bundled into trunks; the animation steps down on weak graphics (see
  *Map animation* under [Settings](settings.md#general)).
- *Simulate maintenance* is a simulation read from the known topology; nothing is switched off.

## Privacy

- **Read:** the device list, the live traces of the last minutes, the availability history, the topology Lanloom has
  resolved.
- **Stored in the browser:** the map animation setting, theme and palette (this browser only).
- **Sent:** nothing outside your Lanloom. Zooming, filtering and the hover window are computed locally or from Lanloom's
  own database.
- **Roles:** every role looks at the map and its cards. Operators change names, types, hidden state, manual connections,
  protection classes and groupings; every change is written to the audit log. Admins also see the location fields of
  the site.

## See also

- [DISCOVERY.md](../DISCOVERY.md) - how devices are recognised and grouped
- [AVAILABILITY.md](../AVAILABILITY.md) - availability, traffic plots, hover window and *Simulate maintenance*
- [TOPOLOGY.md](../TOPOLOGY.md) - where the traces come from
- [PERFORMANCE.md](../PERFORMANCE.md#the-map-in-the-browser) - the frame budget of the map
- [PALETTE.md](../PALETTE.md) - search and commands
