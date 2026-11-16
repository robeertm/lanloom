# Home-automation bridge

Lanloom reads a home-automation hub over its WebSocket API: devices, entities and areas for the map, state changes
for the live grade, and – in admin mode – which entities each automation and script uses and their last runs. It
never switches, changes or reloads anything, and it never runs code inside the hub (no add-on, no custom component).

![The bridge section on the hub's component card](screenshots/hub-bridge-light.png)

## Setting it up

Settings › Connections › *Home-automation hub*: the hub's address (`http://…:8123` or `https://…`) and a
long-lived access token. A token of a normal (non-admin) user is enough for the map.

| Option | Default | What it does |
|---|---|---|
| Accept a self-signed certificate | off | For a hub with its own certificate on `https://`. |
| Admin mode | off | Also read automation and script configuration, their runs and related items. These requests need a token of an administrator on the hub. Still strictly read only. |
| Show people, trackers and zones | off | See [Privacy](#privacy). |
| Show cameras and pictures | off | |
| Show calendars and to-do lists | off | |
| Show notifications | off | |

The bridge appears on the component card of the hub (open its tile on the map).

## What is read

There is exactly one function that sends anything to the hub. It lets through only these message types, each with
exactly its fields, and checks every value:

| Message type | Fields | When |
|---|---|---|
| `auth` | access token | at connect |
| `get_states`, `get_config` | – | every 10 minutes, connection test |
| `config/device_registry/list`, `config/entity_registry/list`, `config/area_registry/list` | – | every 10 minutes |
| `config_entries/get` | – | every 10 minutes (which integration a device belongs to) |
| `subscribe_events` | `state_changed`, `automation_triggered` or `script_started` | at connect |
| `automation/config`, `script/config` | the entity id (only `automation.*` / `script.*`) | admin mode, within a budget |
| `search/related` | item type `automation`/`script` and its entity id | admin mode, within a budget |
| `trace/list`, `trace/get` | domain `automation`/`script`, item id, run id | admin mode, only on request |

Anything else – service calls, firing events, template rendering, registry changes, reloads – cannot be sent; the
attempt is refused and counted ("No message outside the allow-list was attempted" on the card). Static tests check the
source code of the bridge for this on every build. A message type the hub does not know (an older version) is not
asked again in that session.

From configurations Lanloom keeps **only entity ids**, per role (trigger, condition, action; ids that only appear
inside a template are marked as weaker). Aliases, descriptions, templates, values and service data are discarded
right away. From traces it keeps the start and end time, the result, the last step and the step path of the newest
run – never variables, trigger descriptions or error texts (only "error: yes/no").

## Sparing the hub

- Full inventory every 10 minutes; state changes as they come, at most 20 per second passed on (the latest value of
  an entity wins).
- **Templates are never rendered** – rendering costs the hub work on every call.
- Configuration reads at most 30 per minute, related-item lookups at most 20 per minute, one at a time. Answers stay
  fresh for an hour (configuration) or two (related items), longer for big installations, spread so that not everything
  falls due at once. A changed configuration is read again earlier.
- Traces only when someone asks for the last runs of one automation, at most 6 lookups per minute.
- **Scripts that call each other in a loop** (found with Tarjan's algorithm from the configurations) are never looked
  up: the hub would follow the loop until it gives up. Related items are only looked up after every configuration has
  been read once, so loops are known first; scripts are read before automations.
- An item the hub answers with an error is paused – 24 hours, then doubling up to a week; a changed configuration lifts
  the pause. Three timeouts in a row count as one error. The card lists paused lookups.

## Privacy

Off by default, dropped inside the bridge before anything is stored:

| Group | Entities |
|---|---|
| People, trackers and zones | `person.*`, `device_tracker.*`, `zone.*` |
| Cameras and pictures | `camera.*`, `image.*` |
| Calendars and to-do lists | `calendar.*`, `todo.*` |
| Notifications | `persistent_notification.*`, `notify.*` |

- A tracker only lends its hardware and IP address for matching hub devices with network devices – never its state
  ("home"), a place, coordinates or its name.
- Events carry the entity id, its state and the time. When an automation or script caused a change, the event names
  it; when a person did, it only says `user`. Service data, user ids and context ids never leave the bridge.
- Of attributes only the unit and the name are kept.
- Hub data lives in memory only. It is never written to the database, so backups never contain it.
- Every way out of Lanloom applies the filter again, whatever you switched on for the map: the AI features see the
  home-automation section of the nightly report only as its summary (counts), and "Explain" on one of its notes hides
  the automation's name. Exports and diagnostic files do not contain hub data today; when they do, they go through the
  same filter, which drops private domains and blacks out names.

## On the card

- Mode, inventory, automations and scripts, devices with a hardware address (for matching).
- Privacy: which groups are kept out and how many private items were dropped.
- Sparing the hub: the limits above and the refused-message counter.
- Busiest automations (last hour, 24 hours); **runaway** marks an automation or script that ran 60 times or more within
  one clock hour. In admin mode operators and admins can pick a name to see its last runs.
- Script loops and paused lookups.

![Last runs of an automation](screenshots/hub-runs-dark.png)

## Nightly report

The section *Home automation* (one per hub connection) appears when the hub is connected. It lists runaway
automations and script loops as points to look at, paused lookups and the busiest automations of the night as notes,
and – should it ever happen – messages the bridge refused to send. The summary has counts only. Run counts are kept in
memory in hourly buckets for 26 hours; after a restart the section covers the time since the start.

## API

| Route | Role | |
|---|---|---|
| `GET /api/hub/{id}/bridge` | viewer | the card |
| `GET /api/hub/{id}/automations` | viewer | automations and scripts: mode, last run, runs in the last hour and day, reference counts, loop/pause flags |
| `GET /api/hub/{id}/traces/{entity_id}` | viewer | the last runs fetched for an automation (or `null`) |
| `POST /api/hub/{id}/traces/{entity_id}` | operator | ask the bridge for the last runs (admin mode; audit action `hub.traces`) |
