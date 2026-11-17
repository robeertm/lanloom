# Integrations: how device-family adapters are built

This page describes the foundation every device-family adapter builds on. Which families exist is listed in [COMPATIBILITY.md](COMPATIBILITY.md). Adapters are built in only: each family is a package in `lanloom/integrations/<family>/`, shipped inside the image (whose root file system is read-only). The registry (`lanloom/integrations/__init__.py`, `BUILTIN`) collects exactly the packages of its own folder and nothing from the environment or any other folder. There is no plugin mechanism. Requests are welcome as issues ([CONTRIBUTING.md](../CONTRIBUTING.md)).

## Two halves per family

```
              ┌─────────── web app (read-only root file system) ───────────┐
 device ◀─R── │ read half   lanloom/integrations/<family>/read.py           │  read-only account
              │   → neutral model + capability list → map, firewall tab   │
              │   draft → preview → plan (ids only) + hash                │
              └────────────────────────────────────────────────────────────┘
                          │ plan + passkey signature
              ┌─────────── executor service (updated by hand) ─────────────┐
 device ◀─W── │ write half  lanloom/integrations/<family>/write.py          │  write account (least rights)
              │   gate · backup · apply · watch · automatic rollback      │
              └────────────────────────────────────────────────────────────┘
```

- **Read half** (`read.py`): runs in the web app with a read-only account. It reaches the device only through `lanloom/integrations/transport.py` with a *read* allow-list: GET requests to named paths, plus at most a sign-in and sign-out request whose body has exactly the credential fields. A device whose read API takes a query document by POST gets one *query* rule instead: a form-encoded POST to one path whose every field has a mandatory check that proves the document only reads (anything else, or an exception in the check, refuses the request before a byte is sent). A static test checks that read halves and translation modules import no network module and no write half. Contract: `async run(emit)`, `async test()`, `capabilities()`, `async account_scope()` (`read_only` | `can_write` | `unknown`).
- **Write half** (`write.py`): a `ControllerAdapter` (see [FIREWALL-EDITOR.md](FIREWALL-EDITOR.md#controller-adapters)). It is loaded only in the executor process, only when real writes are armed. Every request goes through a *write* allow-list: method, path pattern, the exact body fields and a check for each value. The adapter cannot widen the list at run time.
- **Translation module** (`model.py`): maps vendor objects to the neutral model and back. Both halves use it, so the app and the executor compute the same state and the same plan fingerprint.
- **Manifest** (`__init__.py`, `IntegrationManifest`): family id, label for the selection list, capability list, the two halves, the settings and credential fields of each half, and the dialog form (field types, which fields are secret). The registry refuses a manifest whose halves point outside the family's own folder or whose form does not name every setting.
- **Detection signs** (`detect`, optional): a tuple of dicts that let the setup assistant suggest the family after a gentle scan. Allowed keys are `port` (integer), `title` (text in a web page title), `mdns` (DNS-SD service type) and `ssdp` (UPnP type or server text). `title`, `mdns` and `ssdp` must be lower case and 2 to 48 characters long. A sign matches a device when all of its keys match; the family is suggested when any sign matches. The registry validates the signs. Signs only read what the scan already collected; they never cause a request or a sign-in. Example: `detect=({"port": 8006, "title": "hypervisor"},)`.

## Events of a read half

A read half runs like any live source (`async run(emit)`, reconnect with backoff, status events with reason keys `source.family.*`) and emits:

- `{"type": "config", "source": "family", "family", "t", "state": <neutral model v2>, "capabilities"}` - the configuration it read. The app validates it, strips every write key and shows it in the Firewall tab as a read-only source next to the simulation or the executor.
- `{"type": "topology", "source": "controller", ...}` - clients and devices with their parent ports, in the same shape as the generic switch-controller source, so the topology resolver ranks and merges it the same way.
- `{"type": "topology", "source": "hypervisor", "hosts": [{name, ip}], "guests": [{mac, ip, host, bridge, vlan}]}` - a hypervisor's nodes and guest network cards; the resolver hangs every guest below its node (rank right after manual entries). Hardware addresses stay inside the core and are never sent to the browser.

A zone firewall state uses the common lists (`zones`, `networks`, `policies`, `port_forwards`, `dhcp_reservations`, `wans`, `vpn`, `raw`, `findings`); a policy whose zone is `*` applies to every zone. It adds `rule_digest` (one line per vendor rule: name, on/off, a fingerprint of what decides its effect), which the night report keeps to tell changed rules apart without keeping the whole rule list.

A hypervisor state adds `hosts`, `guests`, `guest_nics`, `guest_rules`, `fw_rules`, `fw_options`, `bridges`, `sdn_zones`, `sdn_vnets`, `storage`, `backup_jobs` and `findings` to the neutral model (see `lanloom/firewall/neutral.py`); the component card of a node and of a guest shows them.

## Night report contributions

A family may contribute a section to the night report: `report="lanloom.integrations.<family>.report:<function>"` in its manifest, collected by `integrations.night_sections()`. The function is pure - `function(previous_state, state, now, name)` - and returns `{"family", "title", "summary", "params", "look", "items": [{"level": "alert"|"warn"|"info", "key", "params"}]}` (texts are i18n keys with parameters, no internal ids), or `None` when the family's read half has delivered no state. No source, no section.

## Capability list

```json
{"family": "sample", "api": {"kind": "official-local", "version": "1.0"},
 "read":  ["networks", "zones", "policies", "ports", "ssids", "clients"],
 "write": {"move": "B", "client.set.name": "B", "port.set.native_vlan": "B", "port.set.poe": "B*"},
 "rollback": {"device_native": false, "object_snapshot": true},
 "limits": {"objects_per_plan": 10, "logins_per_hour": 4},
 "lockout": ["mgmt_network", "gateway_uplink", "self_path", "uplink_ports"]}
```

- `write` keys are a plan step kind (all its fields) or a step kind plus one field. Stages: `B` after the gate and the passkey signature; `B*` additionally its own confirmation with a plain warning; `C` (mass or structural changes) is refused in this release whatever an adapter declares.
- After the version check a read half may **narrow** the list (never widen it).
- The interface only shows a control when its key is present. `GET /api/integrations` lists the families with their capabilities; the firewall tab gets the capabilities of the connected executor. An executor of an earlier release sends none: then only moves are offered.

## Neutral model, version 2

Version 1 (since 1.2.0) has `zones`, `allow` (zone pairs), `networks` and `devices` (the end devices a move acts on). Version 2 keeps all of them with their meaning and adds optional lists:

| list | fields |
|---|---|
| `equipment` | id, name, kind, model, firmware, management address, uplink {device, port} |
| `ports` | id, device, idx, name, link, speed, poe, profile, native_vlan, tagged[], is_uplink, lldp_neighbor |
| `policies` | id, name, src {zone, net?, group?}, dst {…}, action (allow/block/reject), proto, ports, return_traffic, enabled, order, hits, builtin, vendor_ref |
| `ssids` | id, name, network, bands[], security, hidden, enabled |
| `guests`, `guest_nics`, `guest_rules`, `bridges` | virtualization hosts: guests, their network cards and firewall rules, host bridges |
| `dhcp_reservations`, `port_forwards` | reservations and forwarding rules |
| `raw` | vendor objects that do not map: shown, never written (`editable: false`) |

Rules for every list: `id` is an opaque, stable text and never a hardware address; `own: true` marks objects Lanloom created (only those may be deleted); `protected`, `builtin` and `is_uplink` keep an object out of every plan. Zone-based firewalls map directly to zones and policies; interface-based ones derive one zone per interface. `allow` can be derived from policies (`neutral.derive_allow`).

## Plan steps, version 2

A step names one object and the values of the fields it changes, as read (`before`) and as wanted (`after`). The executor compares `before` with its own fresh read and refuses on any difference.

`move` · `client.set` (name, fixed address, network) · `client.block` (B*) · `network.create|update|delete` · `zone.create|update|delete` · `policy.create|update|delete|reorder` · `port.set` (name, profile, native VLAN, tagged VLANs, power over Ethernet: B*) · `ssid.create|update|delete` · `dhcp.reserve|release` · `forward.create|update|delete` · `guest.power` (B*) · `guest.nic` · `guest.fw_rule` · `host.bridge` (stage C).

- A plan keeps the **lowest version** that can express it: moves only stay `"v": 1`, exactly as in earlier releases. Any other kind makes it `"v": 2`.
- An executor refuses an unknown step kind and a plan version above the one it understands, with a clear message; it never half-understands a plan. The app refuses to build a plan the connected executor cannot take.
- A create names a new id (`new:` + hex); the adapter returns the id the controller gave the object, and that id is recorded before anything else can go wrong, so the way back (and recovery after a crash) can remove it.
- Every plan has a way back: a backup of every touched object before the first write, the inverse steps, and at most one automatic rollback.

**Hard limits** (fixed in the code, not configurable): at most 10 objects per plan; no firmware, restarts, accounts, keys, remote access, VPN or WAN settings, port mirroring or factory resets (there is no step kind for them); no change to the management network, uplink ports, built-in rules or objects Lanloom did not create (deletes); one plan at a time per device.

## Credentials

| | web app (read half) | executor (write half) |
|---|---|---|
| where | encrypted in the data volume (`secret.key`, mode 0600) | its own options file (mode 0600, owned by the executor's user, no symbolic link) |
| account | read-only; an account that reports it can write is refused where that is checkable | least rights the device offers; tokens preferred over passwords; superuser principals refused, owner-level roles warned about |
| seen by | the read half only | the write half only; the app never sees it |

Secrets travel wrapped (`credentials.Secret`): they never appear in `repr()`, log lines or exception texts, cannot be serialised by accident, and only the send path reveals them.

## Tests every adapter needs

Static allow-list check (one send path, read halves only read), idempotent steps, rollback after a partial write, crash in the middle of a plan restores on the next start, lockout rules, at least the current and the previous major version of the device software, and secrets never in logs, exceptions or `repr()`. The in-memory `SimulatedController` accepts any neutral state, any capability list and faults on order (`fail_after_writes`, `fail_devices`, `fail_restore`).

---

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.
