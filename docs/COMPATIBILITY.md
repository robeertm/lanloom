# Compatibility

Which device families Lanloom can read from and which changes it can make through the optional executor service. Every family is built in; there is no way to load an adapter from elsewhere. Requests for a family are welcome as issues, see [CONTRIBUTING.md](../CONTRIBUTING.md); problems with an existing one are covered in [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## At a glance

Family by stage by checked firmware. **Read** is stage A (Lanloom only reads); **Write** is stage B or B* through the optional executor (see *Stages* below); "none" means no write path exists for that family. The last column says how the versions were checked; it is not a promise for versions that are not listed.

| Family | Read | Write | Checked firmware and versions | How it was checked |
|---|---|---|---|---|
| This host | yes | none | Linux kernels with socket diagnostics | automatic tests; a desktop container runtime lacks the interface and reports it |
| Host agent and container source | yes | none | amd64 and arm64 (the same image as Lanloom); a host kernel with socket diagnostics over netlink; optional: the service manager over the system message bus, a container runtime API, an overlay VPN's status interface (see below) | automatic tests with stand-ins for the kernel files, the bus and the runtime |
| SNMP v2c / v3 | yes | none (Get, GetNext, GetBulk only) | protocol level: v2c, v3 with MD5, SHA, SHA-256 and AES-128; not tied to a firmware | automatic protocol tests with stand-ins |
| MQTT 3.1.1 broker | yes | none (cannot publish) | brokers speaking MQTT 3.1.1 | protocol tests with a stand-in broker |
| Switch controller (local JSON API) | yes | none | console and classic API layouts | automatic adapter tests with stand-ins |
| Home-automation hub (WebSocket API) | yes | none (cannot call services) | releases with the documented WebSocket API | protocol tests with a stand-in hub |
| Sample network (built in) | yes | yes, B | – | simulation, tests |
| UniFi Network | yes | yes, B (wireless clients: network, fixed address, name) | classic API: Network 11.0; integration API: 9.3, 10.0, 10.1; write: self-hosted 10.0.162 | 11.0 on a live console (read); published specifications; adapter tests; self-hosted application (write) |
| Proxmox VE | yes | yes, B and B* (guests only, executor only) | 9.2 and 7.3 (schema); 8.2 (adapter tests) | published API schema, adapter tests, stateful stand-in; no real host yet for writing |
| Sophos Firewall | yes | none yet | SFOS 19.0, 20.0, 21.0, 21.5, 22.0 | published API reference of each release; the free home edition reads the same |

Reading a new firmware version usually works without a change, because Lanloom reads documented fields and ignores what it does not know. If a family stops working after a firmware update, open a [test report or an adapter request](../.github/ISSUE_TEMPLATE) with the version and the diagnostic file ([TROUBLESHOOTING.md](TROUBLESHOOTING.md#the-diagnostic-export)). Writing is only ever offered for versions listed here.

## Generic sources (every vendor)

These work with any device that speaks the protocol; they only read.

| Source | Reads | Writes |
|---|---|---|
| This host (kernel socket diagnostics) | own TCP flows, interface counters | never |
| Host agent (optional, other hosts) | TCP conversations with bytes, listening services, load, memory, disks, failed services and timers, containers, overlay VPN peers; never contents, environment variables or command lines ([details](HOST-AGENT.md)) | never |
| Container source | the container list of a host through the runtime API, `GET` only | never |
| SNMP v2c / v3 | interface counters, LLDP neighbours, switch address table | never (Get, GetNext, GetBulk only) |
| Switch controller (local JSON API, view-only account) | which client sits on which switch port or access point | never |
| MQTT broker | message and byte counts per topic group | never (cannot publish) |
| Home-automation hub (WebSocket API) | rooms, devices, entities, state changes, automation runs; admin mode: entities used by automations and scripts, related items, last runs ([details](HUB-BRIDGE.md)) | never (cannot call services, never renders templates) |

## Host agent

The optional [host agent](HOST-AGENT.md) and the container source only read. What each part needs:

| Part | Needs | Without it |
|---|---|---|
| Image | amd64 or arm64 (the same signed image as Lanloom, same version) | not available on other architectures |
| TCP conversations and bytes | a host kernel with socket diagnostics over netlink; host networking | flows without bytes, reported as not available |
| Listening services and process names | host networking; process names also need the host's process view | services without a process name |
| Failed services and timers | a service manager that answers on the system message bus (read only: list units, read properties) | units "not available" |
| Containers | a container runtime API of the common kind; bytes per container from the byte counters of the container's network namespace, otherwise the one-shot statistics call (API version 1.41 and newer) | containers "not available", or no bytes for a container |
| Overlay VPN peers | the VPN daemon's local status interface (optional) | no peers listed |

A container source reads only the container list through the same API, over a socket or an address you enter (a read-only socket proxy is the safer choice).

## Device-family adapters

| Family | Tested versions | Read (capabilities) | Write through the executor | Guide |
|---|---|---|---|---|
| Sample network (simulation, built in) | – | networks, zones, clients | move a client to another network, rename it, give it a fixed address (stage B) | [FIREWALL-EDITOR.md](FIREWALL-EDITOR.md) |
| Proxmox VE (local API) | 9.2 and 7.3 (against the published API schema); 8.2 in the adapter tests | nodes and cluster, guests (virtual machines and containers) with configuration, addresses and state, guest network cards (bridge, VLAN, firewall flag, rate), bridges and bonds, software-defined network, firewall at datacenter, node and guest level, storage, backup jobs and the newest backup of each guest, traffic per guest; guests below their node in the topology; read-only token check | guests (stage B, executor only): start and clean shutdown, network card bridge/VLAN/firewall flag, guest firewall on/off, guest firewall rules (switch, change, add, remove Lanloom's own); guests that keep the network running are refused; checked against the published API schema of 8.2 and 9.2 and a stateful stand-in, no real host yet | [integrations/proxmox.md](integrations/proxmox.md) |
| Sophos Firewall (local XML API) | SFOS 19.0, 20.0, 21.0, 21.5 and 22.0 (against the published API reference of each release); the free home edition reads the same | zones (with management services allowed on them), interfaces, VLANs and aliases as networks, address and service objects, firewall rules in order (network and user rules) and rule groups, NAT rules (port forwarding, source NAT), DHCP servers with fixed addresses, static routes, IPsec and SSL VPN; plain checks and a night report section; query documents that can only *get* | none yet (stage A, read only) | [integrations/sophos.md](integrations/sophos.md) |
| UniFi Network (local API) | classic API: Network 11.0 (read on a live console); official integration API: 9.3, 10.0 and 10.1 (against the published specifications) | inventory, clients, topology, networks, zones, policies (with hit counters through the classic API), ACL and DNS rules (titles), ports and port profiles, wireless networks, WAN, VPN, fixed addresses, port forwarding, statistics; known clients that are offline but carry a name, a fixed address or a network override | clients (stage B, classic API, site-administrator account of the executor): move a wireless client to another network, fixed address on/off/change, name - tested against a self-hosted Network application 10.0.162 and the adapter tests (console and self-hosted layout) | [integrations/unifi.md](integrations/unifi.md) |

The read adapters show the configuration they read in the Firewall tab (read only) and feed the physical topology; a hypervisor adapter also adds its nodes and guests to the component cards. Writing goes through the executor only, family by family and area by area; what is not listed here is not written. The foundation (capability lists, the neutral configuration model, plan steps version 2, separate read and write credentials, one send path with an allow-list per adapter) is described in [INTEGRATIONS.md](INTEGRATIONS.md).

**Stages:** A = read only · B = change after the safety checks and a passkey signature, with backup, watch and automatic rollback · B* = like B plus its own confirmation with a plain warning (for example turning off power over Ethernet or blocking a client) · C = mass or structural changes, not available.

Each adapter publishes its capability list at start (and may narrow it after checking the device version). The interface only shows controls the list covers.

## AI services

Optional. They only explain devices, notes of the nightly report and firewall rules; they never change anything. Cloud services receive placeholders instead of addresses and names (see [integrations/ai.md](integrations/ai.md)). Lanloom reads the model list from the service itself and pre-selects a recommended model.

| Service | Kind | Address | Key | Model list | Notes |
|---|---|---|---|---|---|
| Ollama | local | `http://<host>:11434` (default port) | none | yes | native API; one-click setup on this computer or on another one |
| OpenAI-compatible local server (LM Studio, llama.cpp server, vLLM) | local | for example `http://127.0.0.1:1234/v1` | optional | yes | address with or without `/v1` |
| OpenAI | cloud | `https://api.openai.com/v1` | required | yes | |
| Anthropic | cloud | `https://api.anthropic.com` | required | yes | |
| Google Gemini | cloud | `https://generativelanguage.googleapis.com/v1beta` | required | yes | |
| Mistral | cloud | `https://api.mistral.ai/v1` | required | yes | |
| OpenRouter | cloud | `https://openrouter.ai/api/v1` | required | yes | |
| Other OpenAI-compatible service | cloud | the address you enter | optional | yes | treated as cloud: placeholders and daily limit apply |

A local address that leads to the internet is treated as a cloud service. The test asks the model a real question and shows the answer and the response time.

---

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.
