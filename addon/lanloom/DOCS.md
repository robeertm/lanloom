# Lanloom add-on

Lanloom draws your local network as a circuit board: every device is a component, cables and wireless links are
traces, live traffic runs along them as dots. It only reads - it never changes a device.

## Opening it

After the start, open **Lanloom** in the side panel. You are signed in with your hub account; there is no extra
password. The first person who opens it should be an administrator of the hub (only administrators see the panel).
Lanloom's setup assistant then suggests the networks to scan and the connections it found.

## Options

| Option | Default | What it does |
|---|---|---|
| `ingress_role` | `admin` | Lanloom role of people who open the side panel: `viewer` (look only), `operator` (rename, scan, tick report items) or `admin` (also settings and connections). |
| `direct_access` | `false` | Also answer requests from the network on port 7380, with Lanloom's own accounts. Off: Lanloom answers only the side panel. |

## Network

The add-on uses the host network, because it has to see the local network (neighbour table, multicast discovery,
this host's connections). It gets no extra privileges: no added capabilities, no devices, no privileged mode. Lanloom
itself runs as an unprivileged user; the start script only prepares its folder and then drops every privilege.

Port 7380 of the host is used. Without `direct_access` it refuses everybody except the side panel.

## Updates

Updates come through the add-on system like for every other add-on. Lanloom's own one-click updater and its release
check are switched off in the add-on; *Settings › Updates* says so and shows what is new in the running version.

## Data and backups

Lanloom keeps everything in `/data/lanloom` inside the add-on volume, so the backups of the add-on system include it.
Lanloom's own backup (*Settings › Backup*) works as well, for moving to a plain container install.

## Sensors over MQTT (optional)

*Settings › MQTT output* publishes a few of Lanloom's figures as sensors (devices online, new devices, internet line,
open issues, health, vulnerability hints, nightly report) through MQTT discovery. It is off by default. In the add-on
you can pick the broker the add-on system offers - no address or password to type. Only counts, a level, a flag and a
time are published: never a device name, address, person or place.

## Help

Handbook: <https://github.com/robeertm/lanloom/blob/main/docs/manual/README.md> ·
Questions and reports: <https://github.com/robeertm/lanloom/issues>
