# Finding every device

How Lanloom finds devices, recognises who they are, and notices that two addresses belong to one device. Everything
on this page runs locally; nothing is looked up on the internet.

## What the scan asks

The default gentle scan needs no privileges. In this order:

| Step | What is sent | What comes back |
| --- | --- | --- |
| Neighbours | one empty UDP datagram to port 9 of each address, then the kernel's neighbour table is read | IPv4 address and hardware address of every device that is awake |
| IPv6 neighbours | nothing: the kernel's IPv6 neighbour cache is only read | the IPv6 addresses a device already used (link-local and others), attached to the device with the same hardware address |
| mDNS / DNS-SD | browse queries for common service types | names, services and the TXT records a device announces (model, firmware where the device tells it) |
| SSDP / UPnP | one search request; descriptions only from the private address that answered, at most 64 KiB, no DOCTYPE or ENTITY | friendly name, maker, model, unique device id |
| WS-Discovery | one multicast probe | device types (computer, printer, scanner, video transmitter) |
| Service ports | a TCP connect to 27 well-known ports (deep scan: about 218, only with your consent) | open services, web page title and `Server` header |
| NetBIOS | one node-status request to each found device (UDP 137) | computer name and workgroup |
| LLMNR | one reverse-name request, only to devices that still have no name | a host name |
| SNMP (off by default) | one read request for three system values with the factory community, only if you switch it on | system description (model, firmware), system name, object id |

Limits as before: about 40 new connections per second, 24 at once, 4 per device. Every UDP request goes out exactly
once per device and only answers from that device count. No raw sockets, no ICMP, no logins, never public ranges.

The 27 ports include the web interface of hypervisors (8006) next to the usual web, name, file-sharing, printing, broker and management ports.

### Preview and first picture

The quick phases (neighbours, IPv6 neighbours, mDNS, SSDP, WS-Discovery) run first. As soon as they are done, the devices found so far are published as a preview, before ports and banners are read, so the setup assistant can offer *Open the map now* while the rest continues in the background. The time from the first setup screen to this preview is measured.

### Gentle signs for adapter suggestions

The connect step of the assistant suggests integrations from what the gentle scan already saw: open ports (for example the home-automation hub port or an MQTT broker port), web page titles and mDNS/SSDP types, plus SNMP at the gateway and switches. Families declare their own signs in the manifest (`detect`, see [INTEGRATIONS.md](INTEGRATIONS.md)). Suggestions never involve a sign-in attempt; credentials are only used after you enter them and press *Test*.

### The SNMP switch

Settings › General › Network scan and the setup wizard have the switch *Also ask devices with the default SNMP read community*.
It is off by default because the factory community is a shared secret in name only; some security devices log such
requests. When it is on, Lanloom reads `sysDescr`, `sysObjectID` and `sysName` and nothing else. The choice is kept
for automatic rescans. For devices with their own community or SNMPv3, add an SNMP connection instead.

## Who is this device?

The device card shows:

- **Maker**, with where it comes from: what the device says itself (UPnP, mDNS, SNMP) wins over the address register.
- **Model** and **firmware**, when the device tells them.
- **Address origin**:
  - *Registered maker*: the first part of the hardware address is in the public IEEE register.
  - *Private or random address*: the address is locally administered. Phones and laptops use such addresses for
    privacy, virtual machines often do too. Lanloom never assigns a maker to them.
  - *Unregistered address*: a globally unique address whose prefix is in no register.
  - *Only an IP address is known*: the device sits in a routed network without a neighbour entry.
- **IPv6 addresses** and the **NetBIOS name**, when found.

The register is the public IEEE list of MA-L, MA-M and MA-S blocks. It ships with the image as a compact file
(`lanloom/data/oui.bin.gz`, built by `scripts/build_oui.py`), is checked by a checksum when it loads, and is
renewed with each release through the normal update. Lanloom never downloads it at run time. If you want a newer
table without updating, build it with the script and put it into the data directory as `oui/oui.bin.gz`; it is
used only when it passes the same checks and is newer than the bundled one.

The kind of a device (router, printer, camera …) still comes from the rule-based classifier. New evidence: WS-Discovery
types, a NetBIOS computer name, SNMP answers, model words in TXT records and, weakly, neutral words in the registered
maker's name (that alone never decides).

Every device record also keeps the raw identification features (address prefix, UPnP device id and model,
SNMP object id and description, WS-Discovery types, NetBIOS name) for the inventory.

## One device, several interfaces

A storage box with two network cards, a laptop on cable and radio, or the second port of a mobile router appear as
two addresses. Lanloom puts them together when the evidence is strong:

| Evidence | Weight |
| --- | --- |
| same UPnP unique device id | strong |
| same SNMP system name and object id | strong |
| same NetBIOS computer name (both seen in the latest scan) | medium |
| same mDNS host name (both seen in the latest scan) | medium |
| neighbouring hardware addresses of the same registered block | weak; stronger with the same switch port, and with first sightings within ten minutes |

At 60 % or more the second interface is drawn as part of the first device: the map shows one component, its card
lists the interface with the reasons. Between 30 % and 60 % the card shows a suggestion with *Same device* and
*Different devices*. *Show separately* undoes an automatic grouping. Your choice is kept until you change it;
nothing is deleted. Name-based evidence counts only while both devices are current, so a replaced device is not
mistaken for its successor. Private (random) addresses never count as neighbours.

Changing a grouping needs the operator role and is written to the audit log.

## New devices in the nightly report

A device seen for the first time is listed with its address and, where known, the registered maker; a private
address or an address in no register is named as such. A second interface of a device that is already known is not
reported as a new device.

## Networks and VLANs

*Settings › Network* lists every network Lanloom knows: the networks of this host's own interfaces, the networks and
VLANs a controller adapter publishes (with name, VLAN number, address service and isolation) and private networks
behind a router of this host. Each one has a *reach*:

| Reach | Meaning |
| --- | --- |
| Scanned directly | on a link of this host: the scan may sweep it (private range of at most /22) |
| Scan allowed across router | behind a router, and an administrator agreed to a gentle scan across it (revocable, kept per range) |
| Adapter data only | behind a router, no agreement: **nothing is sent into the network**; its devices come from the adapter's data |
| Never scanned | public, carrier-grade NAT, larger than /22, loopback or multicast ranges, with the reason |

Automatic rescans include the on-link networks you chose and the ranges you agreed to, never anything else. On the
network map every network and VLAN is a zone titled with its name, VLAN number and range.

## Devices only an adapter knows, and devices without an IP address

Clients of a controller (also in other VLANs), the address table of a switch (read over SNMP) and LLDP neighbours
join the inventory without any scan. Their cards say *Controller or switch data* as the way they were found. A switch
often learns a hardware address that no scan ever saw an IP address for: such a device is kept under its hardware
address and shown without an IP address.

A hardware address without an IP address is folded into the device it belongs to (a second network card, a docking
station, a second port of a router) when the evidence is strong enough:

| Evidence | Weight |
| --- | --- |
| announced over LLDP: chassis address and port address of one neighbour | strong |
| several hardware addresses of one device in the hub's device registry | strong |
| same registered block of hardware addresses, same switch port, first seen within ten minutes of each other, while the other device has an IP address | medium (65 %: grouped, undoable) |

Private (random) addresses never fold into another device by block. The *Show separately* button and your own
choices work as before. The switch *Add devices that only a controller or switch knows* (Settings › Network) turns
this off; the first booking after it was switched on does not report its devices as new in the nightly report.

## Scan protection

Some devices must not be poked by a scan. Lanloom keeps three classes with fixed rules and lists every protected
device in *Settings › Network*:

| Class | Rule |
| --- | --- |
| Printer | never touched on the raw printing ports 9100–9102 and 515, not even by a connection that is closed right away; everything else as usual |
| Industrial | gentle scan: only the web ports 80 and 443, no name or SNMP requests. The industrial ports (102, 502, 20000, 44818, 47808) are probed only by a deep scan with your consent, and never on a protected device |
| Passive only | no packet at all: no port, no banner, no name request, no neighbour nudge; only what the device announces itself (neighbour table, mDNS, SSDP, WS-Discovery) is read. Medical and payment systems and everything you mark |

A class is chosen from what the device announced or what an earlier scan saw (open ports such as 631 or 9100,
DNS-SD printer types, UPnP printer words, industrial and medical ports, industrial and medical DNS-SD types) and
stays until you set another one on the device card (*Printer*, *Industrial*, *Passive only*, *No protection* or
back to *Automatic*). The same rules apply to the presence check and to the sweep of routed ranges. Setting a class
needs the operator role and is written to the audit log (`device.protect`).

## What can be connected

`lanloom/integrations/catalog.json` lists the device classes Lanloom recognises and what can be connected to each:
the built-in sources (home-automation hub, MQTT broker, SNMP), the device-family adapters of the registry (read-only;
*read and configure* where the family has an executor half), storage devices and mobile-network routers over SNMP,
and classes that are **planned**: a UPS over a UPS monitoring service, printers, cameras, container hosts and DNS
filters. A planned class shows what it will read and a read-only account guide, but no button that would do nothing.
The catalog is built in and extended only in the repository (no plugins). It is derived from stored scan data: nothing
is sent to a device to build a suggestion.

The device card has a section *Can be connected* (with *Connect*, which opens the same dialog as the setup assistant,
pre-filled with the address only) and *Settings › Network* lists everything, not yet connected first, sorted by
benefit. Lanloom adds no outgoing connection with any of this.
