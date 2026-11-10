# Firewall editor

The **Firewall** tab shows which zone and network every device sits in and what each zone may reach. In **Change** mode you can move a device to another network, see in plain words what that means, and apply it as one signed change.

> ## ⚠️ Read this before you let it write to a real controller
>
> - **Out of the box nothing is written anywhere.** The editor runs in **simulation** mode against a built-in sample network. Real changes need a separate executor service, a controller adapter that you provide, and an explicit arming step on the host. All three are off by default.
> - **Moving a wireless client can reconfigure every access point.** Many controllers re-provision all access points when a client's network assignment changes. The wireless network can be gone for 2 to 4 minutes in the whole building. The preview says so; plan changes for a quiet moment and set `LANLOOM_FW_QUIET_HOURS`.
> - **You can lock yourself out.** A device that loses its internet or management access can cut off the very path you need to undo the change. The gate refuses moves of protected devices, moves into management networks and moves that take away the internet of the device you are using, and it keeps a local backup so the executor can roll back on its own. It cannot know every path in your network. Keep local console access to your controller.
> - **Private (changing) hardware addresses defeat per-device rules.** Phones and tablets often use a random address per network. A move that is tied to the address does not last. The preview asks you to switch that off on the device first.
> - **Things that use a device by its address break.** Automations, printers set up by address and similar links have to be updated. The preview lists known dependencies, but only those the controller state reports.
> - **The executor holds controller credentials.** Use a dedicated local account with the least rights your controller offers, never an owner account, and keep its options file readable only by the executor.

## Modes

| | simulation (default) | external |
|---|---|---|
| Where plans run | inside the app, against a sample network in memory | in the separate executor service |
| What it writes | nothing | whatever the controller adapter writes, after the gate |
| Approval | one click, nothing is written | passkey signature over the plan hash (user verification required) |
| Watch time after a change | 20 s (`LANLOOM_FW_SIM_WATCH_S`) | 60 min (`LANLOOM_EXECUTOR_WATCH_S`) |

The app itself never writes to a network device in either mode. It keeps the draft, computes the preview and builds the plan.

## How a change runs

```
browser ── draft ──▶ app: preview (plain words, warnings, blocks) ──▶ plan (ids only) + hash + nonce + 5 min expiry
   │  passkey: WebAuthn assertion, challenge = SHA-256(canonical plan)
   └──────────────▶ app pre-checks the signature with the stored public key and queues the plan
executor (pull every 10 s, bearer token) ◀── GET /api/executor/plans
   verify shape · target · expiry · nonce unused · signature against ITS OWN pinned keys (rp id, origin, user verified, counter)
   read the controller fresh · gate · backup of every touched object (0600) · inverse plan
   apply through the adapter · let the controller settle · watch · done, or roll back on its own
   └──▶ POST /api/executor/report (progress) and /api/executor/state (fresh state)
```

**Plan format** (canonical JSON: sorted keys, no spaces, no floating-point numbers, so the hash is the same in the browser, the app and the executor):

```json
{"v": 1, "id": "p_1762790000_1a2b3c4d", "target": "controller", "base": "fp:…", "created": 1762790000,
 "expires": 1762790300, "nonce": "…", "requester": {"user": "admin", "device": null},
 "steps": [{"kind": "move", "device": "d10",
            "before": {"network": "n_main", "ip": "192.168.50.22"},
            "after":  {"network": "n_work", "ip": "192.168.40.22"}}],
 "warnings": ["fw.warn.new_ip", "fw.warn.reprovision"], "confirmed": [], "costs": {"objects": 1}, "undo_of": null}
```

Device and network ids are opaque. The adapter derives them (for example as a salted hash) and resolves them again from its own fresh read; hardware addresses never leave the executor.

## The gate

Right before writing, against a fresh controller read:

1. Plan shape valid, target is the executor's own target, not expired, nonce never seen.
2. Signature valid for a pinned key (simulation in the app: no signature, nothing is written).
3. At most **10 objects** per change.
4. The controller state still matches the plan (`base` fingerprint and every `before`), otherwise "the network changed in the meantime".
5. **Lockout protection:** protected devices (gateway, network equipment, servers others depend on, the host running Lanloom) never move; management networks are no target; wired devices that share a port with others do not move alone; the device you approve from must keep its internet.
6. Marked points (private address, your own device, quiet hours) were confirmed in the preview.
7. The adapter reports the controller healthy and no other change is still running or being watched.

## Backup, watch, rollback, history

- Before the first write the executor stores a raw snapshot of every touched object in `<state>/backup/<plan>/before.json` (mode 0600, created exclusively, never through a symbolic link) and builds the inverse plan.
- After writing it lets the controller settle, then watches (default 60 minutes, one probe per minute). If the adapter reports the network unhealthy, **everything** goes back. If single devices do not come back in their new network on two probes in a row, **only those** go back. There is at most one automatic rollback.
- If the process dies mid-change, the next start restores the backup before doing anything else.
- `<state>/protocol.jsonl` keeps one line per run (plan hash, key, user, times, result; no addresses, no secrets). The app shows the recent changes, and a successful change can be **undone for 24 hours**; the undo is a new plan with its own preview and signature.

## Controller adapters

Lanloom ships no vendor adapter. An adapter is a small Python class that maps your controller to the neutral state and back. It subclasses `lanloom.firewall.adapter.ControllerAdapter`:

| method | contract |
|---|---|
| `read()` | fresh neutral state: zones (internet, discovery), allowed zone pairs, networks (cidr, vlan, management), devices (opaque id, name, kind, network, address, wired, alone on its port, private address, protected reason), dependencies, side effects |
| `snapshot(ids)` | raw backup of the touched objects; a `devices` list allows rolling back single devices |
| `apply(step)` | one move; idempotent (already moved: do nothing); read the object fresh and only replace the planned fields |
| `restore(snapshot)` | put the objects back exactly as in the snapshot |
| `health()` | `(ok, reason)`: is the controller fit for a change right now |
| `settle(steps)` | wait until provisioning finished |
| `probe(steps)` | `{"global_ok": bool, "missing": [ids not back in their new network]}` |

See `SimulatedController` in `lanloom/firewall/adapter.py` for a complete reference and `tests/test_firewall.py` for stand-ins that fail on purpose. Keep the write paths of your adapter to an allow-list: client network assignment and fixed address, and nothing else (no firmware, restarts, accounts, keys or remote access settings).

## Turning on the external executor

1. Create a token and put the settings in a `.env` file next to `compose.yaml`:

   ```sh
   echo "LANLOOM_FW_EXECUTOR=external" >> .env
   echo "LANLOOM_EXECUTOR_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" >> .env
   echo "LANLOOM_EXECUTOR_ORIGINS=https://lanloom.example" >> .env
   docker compose --profile executor up -d
   ```

   The executor starts with `LANLOOM_EXECUTOR_TARGET=simulation`: it rehearses the complete signed path against its own sample network. The app shows "Executor connected".
2. Signing needs a passkey and therefore HTTPS (or localhost). Add a passkey under **Settings**. Open **Firewall → Executor**, copy the signing key and pin it in the executor:

   ```sh
   docker compose exec -T executor python -m lanloom.executor trust-key < key.json
   ```

   The command prints a fingerprint. Compare it with the one shown in the app; only then is the key trusted. The executor never takes keys from the app on its own.
3. Rehearse a few changes in the rehearsal target. Every change asks for the passkey.
4. Only then, to write for real: mount your adapter and its options file into the executor, set `LANLOOM_EXECUTOR_TARGET=controller`, `LANLOOM_EXECUTOR_ADAPTER=yourpackage.module:YourAdapter`, `LANLOOM_EXECUTOR_ADAPTER_OPTIONS=/path/options.json` and `LANLOOM_EXECUTOR_ARM=I-UNDERSTAND`, and create the arming file by hand:

   ```sh
   docker compose exec executor touch /state/armed
   ```

   Without both the phrase and the file the executor refuses to start with a real adapter. Remove the file to disarm.

The executor is updated by hand (`docker compose pull executor && docker compose --profile executor up -d executor`) on purpose: the one-click updater of the app does not touch it, so the app cannot replace the component that holds the controller credentials.

## Settings

| variable | where | default | meaning |
|---|---|---|---|
| `LANLOOM_FW_EXECUTOR` | app | `simulation` | `external` hands plans to the executor service |
| `LANLOOM_EXECUTOR_TOKEN` / `_FILE` | app and executor | – | shared token (at least 24 characters) for the executor routes |
| `LANLOOM_FW_QUIET_HOURS` | app and executor | off | for example `21-7`: changes then need an explicit confirmation |
| `LANLOOM_FW_SIM_WATCH_S` | app | `20` | watch time of the simulation |
| `LANLOOM_EXECUTOR_APP` | executor | `http://127.0.0.1:7380` | address of the app |
| `LANLOOM_EXECUTOR_TARGET` | executor | `simulation` | `controller` for real writes (needs adapter and arming) |
| `LANLOOM_EXECUTOR_ORIGINS` | executor | – | origins the app is opened from; required for `controller` |
| `LANLOOM_EXECUTOR_WATCH_S` | executor | `3600` | watch time after a real change |
