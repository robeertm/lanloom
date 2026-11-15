# Compatibility

Which device families Lanloom can read from and which changes it can make through the optional executor service. Every family is built in; there is no way to load an adapter from elsewhere. Requests for a family are welcome as issues, see [CONTRIBUTING.md](../CONTRIBUTING.md).

## Generic sources (every vendor)

These work with any device that speaks the protocol; they only read.

| Source | Reads | Writes |
|---|---|---|
| This host (kernel socket diagnostics) | own TCP flows, interface counters | never |
| SNMP v2c / v3 | interface counters, LLDP neighbours, switch address table | never (Get, GetNext, GetBulk only) |
| Switch controller (local JSON API, view-only account) | which client sits on which switch port or access point | never |
| MQTT broker | message and byte counts per topic group | never (cannot publish) |
| Home-automation hub (WebSocket API) | rooms, devices, entities, state changes | never (cannot call services) |

## Device-family adapters

| Family | Tested versions | Read (capabilities) | Write through the executor | Guide |
|---|---|---|---|---|
| Sample network (simulation, built in) | – | networks, zones, clients | move a client to another network (stage B) | [FIREWALL-EDITOR.md](FIREWALL-EDITOR.md) |

No vendor adapter ships in this release yet. The foundation they build on (capability lists, the neutral configuration model, plan steps version 2, separate read and write credentials, one send path with an allow-list per adapter) is in place; see [INTEGRATIONS.md](INTEGRATIONS.md).

**Stages:** A = read only · B = change after the safety checks and a passkey signature, with backup, watch and automatic rollback · B* = like B plus its own confirmation with a plain warning (for example turning off power over Ethernet or blocking a client) · C = mass or structural changes, not available.

Each adapter publishes its capability list at start (and may narrow it after checking the device version). The interface only shows controls the list covers.

---

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.
