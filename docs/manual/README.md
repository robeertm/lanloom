# Lanloom manual

One page for every view of the app, each with the same layout: **Purpose**, **What you see**, **How to use it**,
**Limits**, **Privacy** (what is read, stored and sent, and which role may do what) and **See also** (links to the deep
guides in [docs/](../)). The *About this view* rail of the app (key `I`) links to these pages.

| View | Page | In one line |
|---|---|---|
| Overview | [overview.md](overview.md) | How the network is doing in three seconds: banner, 24-hour strip, key figures, cards. |
| Map | [map.md](map.md) | The network as a circuit board: components, traces with measured bytes, area filters. |
| Network | [network.md](network.md) | The same devices ordered by cabling: switch ports, access points, hypervisor guests. |
| Devices | [devices.md](devices.md) | Every device with type, certainty, maker and services, plus the stability list. |
| Inventory | [inventory.md](inventory.md) | Purpose, room, warranty, firmware and life cycle of every device. |
| Export and import | [export.md](export.md) | The network plan as PDF, SVG or draw.io, the inventory as CSV or JSON, import of own fields, a redact option for sharing. |
| Vulnerabilities | [vulnerabilities.md](vulnerabilities.md) | Published vulnerabilities that match a device's maker, model and firmware, from an offline feed, with what to do. |
| Firewall | [firewall.md](firewall.md) | Zones, networks and rules read from the controller; drafts with a dry run. |
| Data paths | [data-paths.md](data-paths.md) | Which countries your devices exchange data with, on a world map. |
| Nightly report | [nightly-reports.md](nightly-reports.md) | A short report of every night with points to tick off. |
| Failure chain | [failure-chain.md](failure-chain.md) | What fails if a part fails, what it depends on, simulated maintenance, and the outage automatic with dashed affected parts. |
| Rewind and incidents | [incidents.md](incidents.md) | The last 24 hours or 7 days played back on the map; faults bundled into incident files with root, kind, what changed before; 90-day calendar and timeline & data state. |
| Automations | [automations.md](automations.md) | What the hub does on its own, in plain words, with findings, a dependency board and a day plan. |
| Running | [running.md](running.md) | What runs on the other hosts: containers, services, timers, failed units, with an optional host agent. |
| Sites | [sites.md](sites.md) | Several places in one Lanloom: switcher, all sites at a glance, who sees which site, reports per site or combined. |
| Add-on and MQTT output | [add-on.md](add-on.md) | Lanloom in the side panel of a home-automation hub, and its figures as sensors there over MQTT. |
| Updating | [updating.md](updating.md) | Update notice, one-click update, channels, hold, going back, signature check, What's new. |
| Settings | [settings.md](settings.md) | Scanning, connections, appearance, notifications, users, security, backup, storage, AI. |
| API | [api.md](api.md) | Tokens for scripts, the public API, webhooks and external reports. |
| AI | [ai.md](ai.md) | Optional explanations, questions and suggestions, local or cloud, read-only. |
| The parts around the views | [views.md](views.md) | Right now, About this view, live state and live grade, deep links, keys. |

Keyboard, screen reader and the tree view of the Network view: [../ACCESSIBILITY.md](../ACCESSIBILITY.md).

When something does not work, see [../TROUBLESHOOTING.md](../TROUBLESHOOTING.md). Installation is in
[../INSTALL.md](../INSTALL.md), the security model in [../../SECURITY.md](../../SECURITY.md).
