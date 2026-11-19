# Resilience: risk lens, weak spots, emergency scenarios

How well the network copes when something fails. The page lives under the control room (`#/resilience`,
`#/resilience/scenarios`, `#/resilience/layers`, `#/resilience/part/<id>`), the risk lens on the Overview and Network maps,
and a section on the component card. The user guide is [manual/resilience.md](manual/resilience.md).

Everything is computed in `lanloom/resilience.py` from data the core already holds. Nothing is switched, nothing is sent
anywhere, and no reading is invented: an axis without a source is *unknown* and counts as half weak.

## Sources

| What | From |
|---|---|
| What fails with a part | the dependency graph of the [failure chain](manual/failure-chain.md) (`lanloom/failure.py`), its breadth-first search over *sure* edges |
| Power source, spare, note | own fields per part (`resilience_parts` in the settings), written on the card or under *Why?* |
| Battery runtime | the battery backups of the [control room](CONTROL-ROOM.md) (NUT or UPS-MIB), live |
| Reserve line | the internet watch of the control room |
| Backups and restore checks | the backup chains of the control room |
| Availability | 5-minute slots of the last 35 days ([AVAILABILITY.md](AVAILABILITY.md)) |
| Early warnings | host health ([HOST-HEALTH.md](HOST-HEALTH.md)) |
| Vulnerabilities, end of updates, certificates | [VULNERABILITIES.md](VULNERABILITIES.md), the inventory, the services probe |

## The seven axes

Each axis gives a weakness from 0 (good) to 1, or *unknown*, with its reasons.

| Axis | Weakness |
|---|---|
| Impact | weighted number of parts that fail with it (device, infrastructure, container, connection, hub device, automation 1; script 0.5; entity 0.05): `a = log(1 + n) / log(1 + 80)`, at most 1 |
| Spare | own field: second unit takes over 0, spare on the shelf 0.35, partly covered 0.5, none 1; nothing depends on it 0; the internet with a reserve line 0; not noted: unknown |
| Backup | covering chain (directly or through its host): ok with a restore tried in the last 90 days 0, ok or running 0.25, late or unknown 0.5, failed 0.9; a part that keeps data (storage, server, hub, broker, container, Lanloom's host, a host with guests) without a chain 1; network gear without a chain: unknown; anything else does not count |
| Power | UPS 0.1 (0.35 when the UPS asks for a new battery), own battery 0.15, mains only 1, unknown. PoE follows the switch it hangs on; guests, containers and connections follow their host; the device a UPS reports through is taken as on the UPS (derived, the card says so) |
| Up to date | open vulnerability (exploited 1, critical/high 0.8, medium 0.5, low 0.2), no more updates 0.8 (ending soon 0.3), certificate red/expired 1, yellow 0.6 |
| Watch | reachability (presence check by address) and a second source that reports from it (a connection, a host agent, the hub, an adapter): both 0, one 0.5, none 1 |
| Health | availability over 35 days (>= 99.9 % 0, >= 99 % 0.2, >= 97 % 0.5, >= 90 % 0.8, below 1), +0.15 from three outages in 7 days; an early warning at least 0.6; no measurement: unknown |

`weakness` is the weighted mean of the six axes after impact (spare 0.22, backup 0.20, power 0.16, health 0.16, watch 0.14,
up to date 0.12; unknown counts as 0.5; an axis that does not count is left out). `risk = 100 × weakness × (0.3 + 0.7 × a)`.
Levels: below 15 low, below 30 moderate, below 45 elevated, below 60 high, from 60 critical. A part nothing depends on stays
at 30 or below. Hub devices are only rated by their impact (`60 × a`), so the lens shows the bridges everything hangs on.

The **recommendation** is the axis with the largest share in the risk (a missing spare only for parts with an impact of 0.55
or more), worded per case with an effort of small, medium or large.

## Score and grade

The score rates the parts that matter: infrastructure (router, switch, access point, storage, server, hub, broker, Lanloom's
host, topology nodes) and every part something depends on. `mix = 0.6 × impact-weighted mean of the risks + 0.4 × mean of the
three highest`, `score = 100 − mix`. Grades: A from 85, B from 72, C from 60, D from 48, E from 36, below F. One value per day
(the last of the day) is kept for 90 days (`resilience_history`).

## Scenarios

All scenarios run several roots at once through the failure chain; the answer of a single scenario has the format of a chain
(stages, certainties, reasons), so the map shows it in the failure bar.

- **Power cut**: roots are the parts on the mains at once, the parts on a UPS once its runtime minus the reserve has passed.
  The runtime is the live value of the UPS; without one the runtime from the settings (marked as such); without both the parts
  on the UPS are taken as running. Any minute from 0 to 240 (`?minute=`). Parts without a power source are listed as open,
  never guessed.
- **Internet down**: the internet node and everything noted as needing it (a manual dependency with the target *Internet*);
  the reserve line and the automations that keep running locally are named.
- **A part off**: the gateway, storage boxes, servers, the hub host, then the parts with the highest impact (six at most).

## Layers

Power (the UPS units), internet, gateway, switches and access points, servers and storage, platforms and connections,
services and devices. For each: the members, what fails above it when the whole layer fails (by layer), the highest risk and
the weakest part.

## HTTP

| Route | Role | |
|---|---|---|
| `GET /api/resilience` | viewer | score, history, weak spots, scenarios, layers, open power sources, UPS units, settings |
| `GET /api/resilience/lens?view=overview\|network` | viewer | `{tiles: {tile: {r, l}}}` for the lens |
| `GET /api/resilience/scenario/{id}?minute=` | viewer | one scenario as a chain (`power`, `internet`, `node:<id>`) |
| `GET /api/resilience/part/{node}` | viewer | the seven axes with reasons, recommendation, own fields |
| `PUT /api/resilience/part/{node}` | operator, audit `resilience.part` | `{power, spare, note}`; empty values remove them |
| `PUT /api/resilience/settings` | operator, audit `resilience.settings` | `{runtime_min, reserve_min}` |

Texts are i18n keys with variables (`rs.why.*`, `rs.rec.*`). The result is cached for a minute and computed again when the
graph or the own fields change (about 0.2 s for the sample network on a small board).

## Nightly report

A section *Resilience* only when there is something to tell: the score moved by five points or more since the last day
(a drop needs a look), or a part is at the critical level (with what helps most). A calm night leaves it out.

## Sample data

The demo notes power sources (the gateway, switch and home server on the UPS, the access points on PoE, the backup server and
the virtualization host on the mains), spares, a reserve of five minutes, a hub that needs the internet for weather and voice
control, and two months of score history.
