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
| Home-automation hub (WebSocket API) | rooms, devices, entities, state changes, automation runs; admin mode: entities used by automations and scripts, related items, last runs ([details](HUB-BRIDGE.md)) | never (cannot call services, never renders templates) |

## Device-family adapters

| Family | Tested versions | Read (capabilities) | Write through the executor | Guide |
|---|---|---|---|---|
| Sample network (simulation, built in) | – | networks, zones, clients | move a client to another network (stage B) | [FIREWALL-EDITOR.md](FIREWALL-EDITOR.md) |
| Proxmox VE (local API) | 9.2 and 7.3 (against the published API schema); 8.2 in the adapter tests | nodes and cluster, guests (virtual machines and containers) with configuration, addresses and state, guest network cards (bridge, VLAN, firewall flag, rate), bridges and bonds, software-defined network, firewall at datacenter, node and guest level, storage, backup jobs and the newest backup of each guest, traffic per guest; guests below their node in the topology; read-only token check | none yet (stage A, read only) | [integrations/proxmox.md](integrations/proxmox.md) |
| UniFi Network (local API) | classic API: Network 11.0 (read on a live console); official integration API: 9.3, 10.0 and 10.1 (against the published specifications) | inventory, clients, topology, networks, zones, policies (with hit counters through the classic API), ACL and DNS rules (titles), ports and port profiles, wireless networks, WAN, VPN, fixed addresses, port forwarding, statistics | none yet (stage A, read only) | [integrations/unifi.md](integrations/unifi.md) |

The read adapters show the configuration they read in the Firewall tab (read only) and feed the physical topology; a hypervisor adapter also adds its nodes and guests to the component cards. Writing through the executor comes per family in later releases. The foundation (capability lists, the neutral configuration model, plan steps version 2, separate read and write credentials, one send path with an allow-list per adapter) is described in [INTEGRATIONS.md](INTEGRATIONS.md).

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
