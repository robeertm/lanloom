# Performance, storage and the browser budget

Lanloom is meant to run on a small single-board computer with 2–4 GB of memory and to stay light in the browser. This
page lists what was measured, how to measure it yourself, how long each kind of data is kept, and what the map does to
spare the graphics of the viewing device.

## Hardware requirements

| Network size | Host | Memory for Lanloom | CPU (one core) | Database |
|---|---|---|---|---|
| up to 250 devices | a 4-core arm64 single-board computer with 2 GB (1.5 GHz class) or any small amd64/arm64 machine | about 80 MB | about 3 % at rest | about 2 MB |
| up to 1000 devices | a 4-core arm64 single-board computer with 4 GB, or better | about 100–150 MB | about 3 % at rest, up to 16 % with live traffic on every device | about 7 MB |

Plus about 300 MB for the image. The database sizes assume two days of hourly and 30 days of daily data paths for a
third of the devices and a month of events; longer retentions (Settings › Storage) grow it accordingly. The viewing
device needs no graphics acceleration.

## Measured values

Measured on a current 4-core arm64 single-board computer (2.4 GHz class) limited to one core, with other jobs running
on the same machine (so these are upper values; a 1.5 GHz board is roughly two to three times slower per core). Medians in milliseconds.

**Server time of the main requests** (`scripts/benchmark.py`, generated test network, Python 3.12):

| | 50 devices | 250 devices | 1000 devices |
|---|---|---|---|
| Core start over the database | 7 | 28 | 117 |
| Live loop, one second, every device's trace live | 8 (0.8 % CPU) | 39 (3.9 % CPU) | 158 (16 % CPU) |
| Map, overview | 11 | 59 | 239 |
| Map, network view | 20 | 92 | 396 |
| Device list | 10 | 50 | 192 |
| Device list, first read after a start or a scan | 126 | 467 | 1057 |
| Component card, refresh while open | 23 | 43 | 128 |
| Component card, first opening | 88 | 297 | 1503 |
| Data paths, 24 hours / 30 days | 15 / 17 | 71 / 82 | 303 / 384 |
| Python heap of the core (incl. 10 min live history) | 4.4 MB | 5.8 MB | 22 MB |

**Container at rest** (published image layout, limited to one CPU, no network, after two minutes):

| | 50 devices | 250 devices | 1000 devices |
|---|---|---|---|
| Start until the health check answers | 3.7 s | 5.2 s | 4.7 s |
| CPU, mean over two minutes | 2.2 % | 2.6 % | 2.6 % |
| Resident memory | 77 MB | 75 MB | 65 MB |

**Browser** (two different browser engines, headless and without graphics acceleration, on the same machine, demo
network): with *Smooth* the dot layer drew 20 and 32 frames per second; *Automatic* stepped itself down to 10 and 18
(its cap without acceleration is 20), *Saver* stayed at or below 15; *Off* and a board out of view drew no frames. In
70 seconds without a change the map was not requested once.

Measure your own host:

```
python3 scripts/benchmark.py                        # 50, 250 and 1000 devices
python3 scripts/benchmark.py --devices 500 --json   # one size, as JSON
```

Times depend on the CPU; the script also prints a calibration time (a fixed workload), and `tests/test_budget.py` keeps
every value within a multiple of it, so a change that makes something much slower fails the tests on any machine.

## What keeps it light

- **A DNS filter is read every two minutes** with a handful of small requests (block lists and version every 15 minutes); Lanloom keeps per filter one latest reading in memory and two numbers per hour (35 days) and day (400 days).
- **Host agents are light on both sides.** An optional [host agent](HOST-AGENT.md) sends one report of a few kB every 10 seconds; Lanloom keeps per host one latest state and one 5-minute sample for 35 days, and a container source is read once every 30 seconds.

- **Search is indexed.** The search box ([PALETTE.md](PALETTE.md)) keeps the searchable text of every device and of every hub entity and rebuilds an entry only when it changed; a search over 3000 hub entities takes a few milliseconds, and `scripts/benchmark.py` measures it as `search_ms` (kept in budget by `tests/test_budget.py`).
- **The start page shares one pass.** *Overview* and the *Right now* rail read the inventory once per two seconds for both, keep the data-paths figures for a minute, and measure about as long as the device list (0.06 s at 250 devices, 0.2 s at 1000 on one core of the test board); `scripts/benchmark.py` reports it as `overview_ms`, kept in budget by `tests/test_budget.py`. The page asks every ten seconds and only while it is visible.
- **The Automations tab reads what the bridge already keeps.** List, findings, *Who switches what*, the timeline and the day plan are built per request from memory (no hub request); the timeline reads only the newest 400 changes, causes are classified once. With as many automations as devices and 20,000 state changes in the ring it measures 0.12 s at 250 and 0.25 s at 1000 on the test machine (`automations_ms` in `scripts/benchmark.py`, kept in budget by `tests/test_budget.py`). The pages ask every 10 to 60 seconds and only while visible.
- **Change channel instead of polling.** The live connection carries a structure number and a map change number. The
  browser fetches the map only when statuses, flow traces or sources really changed, the data paths only after new
  hourly data, and nothing at all while the tab is hidden.
- **Reused views.** Each device's view is built once and reused until its database row changes; the network detection
  is read at most every 30 seconds; the map of the last change serves an open component card for up to 5 seconds.
- **Compact live history.** One sample per second and trace takes 24 bytes; traces that went quiet are forgotten after
  the retention.

## Storage and retention

*Settings › Storage* shows the space of each area and the retention of each kind of data:

| Kind of data | Choices | Default |
|---|---|---|
| Live history (memory only) | 5, 10, 30, 60 minutes | 10 minutes |
| Data paths per hour | 1, 2, 3, 7 days | 2 days |
| Data paths per day | 31, 92, 183, 366 days | 31 days |
| Availability and events | 35, 90, 180, 365 days (the availability slots, [details](AVAILABILITY.md); the traffic per minute behind the plots is kept 8 days) | 35 days |
| Nightly reports | 7, 14, 30, 90, 365 days | 30 days |
| Audit log | 30 days to 10 years | 1 year |
| Scan history | last 50, 200, 1000 scans | last 200 |

Inventory, settings and accounts are never removed automatically. Events are kept at least as long as the nightly
reports need them.

The **nightly clean-up** runs after 03:00 in the report time zone (or at the next start after a missed night). It
deletes what is past its retention, refreshes the database statistics and compacts the file when at least a sixth of
it (and 4 MB) is free. Compacting needs room for a copy of the file; without it the step is skipped and the nightly
report says so. The report's *Storage* section names the night's clean-up, the size and growth of the database, and
raises a point when the data volume would be full within 30 days at the current growth.

## Counter check

Every hour two meters that see the same traffic are compared:

| Comparison | Reference | Compared | Judged |
|---|---|---|---|
| Internet uplink vs. NetFlow | the router's uplink counter (SNMP) | bytes from NetFlow/IPFIX of the allowed exporters | with NetFlow on and at least 50 MB in the hour |
| This host | its interface counters | its connections to LAN devices and the internet | at least 20 MB; only "too much" |

A ratio between 80 % and 125 % is plausible. Above means traffic is counted twice (for example two exporters
reporting the same flows, or a sampling rate applied twice), below means part of it is not counted (an exporter or a
network missing). Connections never contain all of an interface's traffic (other protocols), so for this host only
"too much" is a finding. Results show in the source view (the live grade button at the top) and in the nightly
report; nothing is changed automatically. Only byte sums per hour are kept, no addresses.

## The map in the browser

- **Frame budget.** *Settings › Appearance › Map animation*, stored per browser: *Automatic* draws at most 30 frames
  per second (20 when the browser renders without graphics acceleration), *Smooth* up to 60, *Saver* 15 with fewer
  dots, *Off* shows only the glow of the traces. When drawing takes more than half a frame for two seconds the rate
  steps down (30 → 20 → 15 → 10, then fewer dots) and climbs back after ten seconds with room.
- **Nothing drawn that nobody sees.** The animation stops while the tab is hidden and while the board is scrolled or
  hidden out of view; it moves only when a live frame brings measured bytes.
- **No blur over animated layers.** Translucent panels over the map use a plain background; a blur filter there makes
  the browser recompute it for every frame.
- **Canvas 2D only.** No WebGL drawing; the browser is only asked once whether it has real acceleration, to choose the
  starting frame rate. Many traces into one area are bundled into trunks, so a large zone draws as few lines.
