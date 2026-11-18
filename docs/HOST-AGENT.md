# Host agent and container source

An optional, small program for the other hosts of your network. It reports what runs there - containers, listening
services, failed services and timers, load, memory, disks - and which hosts talk to which. Lanloom shows it in the
[Running view](manual/running.md), on the device card and as folded containers on the map.

It is optional in two ways: Lanloom works exactly as before without it, and every part of what it reads can be left
out by not mounting the matching file or socket. It is metadata only. It never reads contents, environment variables
or command lines.

- [What it is](#what-it-is)
- [What it reads, and what it never reads](#what-it-reads-and-what-it-never-reads)
- [The secret filter](#the-secret-filter)
- [Installation](#installation)
- [What every flag and mount is for](#what-every-flag-and-mount-is-for)
- [Environment variables](#environment-variables)
- [Checking what leaves the host](#checking-what-leaves-the-host)
- [Pairing, token and revoking](#pairing-token-and-revoking)
- [Transport and the buffer](#transport-and-the-buffer)
- [Updates](#updates)
- [A container source without an agent](#a-container-source-without-an-agent)
- [What Lanloom keeps, and for how long](#what-lanloom-keeps-and-for-how-long)
- [Conversations](#conversations)
- [Resources](#resources)
- [Limits](#limits)
- [Protocol summary](#protocol-summary)
- [Troubleshooting](#troubleshooting)

## What it is

- The **same signed image** as Lanloom, `ghcr.io/robeertm/lanloom:<version>`, started with another command:
  `python -m lanloom.agent run`. There is nothing else to download and nothing to install on the host.
- It uses only the **standard library** of the image: no extra package, no program that is started to read something.
- It **opens no port** and **takes no commands**. It sends its report to the one Lanloom it was paired with; the
  answer can only say "fine", "revoked" or which version Lanloom runs.
- It runs next to your other containers, one per host. A host that cannot run the agent can still be read through a
  [container source](#a-container-source-without-an-agent), containers only.

## What it reads, and what it never reads

| Data | From where | Needs |
|---|---|---|
| Host name, kernel, operating system label, addresses of the host's own interfaces | the kernel's files (`/proc`), the host's release file | nothing; the release file needs the `/:/host:ro` mount |
| Load, memory, swap, uptime | the kernel's files | nothing |
| Disk usage (total and free) | file system statistics of the paths in `LANLOOM_AGENT_DISKS` | the `/:/host:ro` mount (without it only the agent's own file system is measured) |
| TCP conversations with bytes (local address, peer address, service port, bytes each way) | the kernel's socket diagnostics over netlink, summed between two reports | `network_mode: host` |
| Listening services (address, port, short process name) | the kernel's TCP and UDP tables; the process name from `/proc/<pid>/comm` (at most 15 characters) | `network_mode: host`; process names also need `pid: host` |
| Failed services and the result of timers (name, result, since when, last and next run) | the host's service manager over the system message bus, read only | the bus socket mount |
| Containers (short id, name, image, state, health, times, exit code, restarts, published ports, networks, compose project and service) | the container runtime's API, GET requests only | the runtime socket mount |
| Bytes per container | the byte counters of each container's network namespace (`/proc/<pid>/net/dev`); otherwise the runtime's one-shot statistics | `pid: host` (the host network gives a container no counters of its own) |
| Peers of an overlay VPN (name, overlay addresses, online, last seen, operating system label) | the VPN daemon's local status interface, GET only | its socket mounted, or `LANLOOM_AGENT_OVERLAY` |

**What it never reads or sends:** file contents, environment variables, command lines and arguments, entry points,
container labels other than the compose project and service name, mounts and volumes, the output of a health check,
descriptions of services, and - from an overlay VPN - keys, user names, log-in names and routes. These are not removed
afterwards; they are never copied in the first place (see the next section).

## The secret filter

Two layers, applied on the agent before anything is sent and once more by Lanloom when a report arrives:

1. **Positive lists.** Every record is reduced to a fixed list of fields. Whatever a runtime, the service manager or
   the kernel answers beyond that list is dropped right after parsing. Container labels pass only for the compose
   project and service names.
2. **Value filter.** Every string that is kept still runs through a filter: the password part of an address such as
   `scheme://user:password@host` and long token-like runs are replaced, `key=value` pairs whose key names a secret
   (password, secret, token, key, credential ...) lose their value, and every string is cut to a short length.

Before a report is queued, the agent also checks it for forbidden field names (`env`, `command`, `labels`, `mounts`,
`args` and the like); if a collector ever had a bug that produced one, the field is dropped and the event is logged.
Lanloom applies the same check and the same filter to every incoming report. The filter has no switch. A test feeds a
container whose environment, command line and labels carry known secrets and checks that none of them reaches a
report; `python -m lanloom.agent selftest` runs the same check on the host.

## Installation

Per host, in a few steps. Needs an administrator of Lanloom.

1. In Lanloom open **Settings > Agents** and choose **Pair a host agent**. Optionally give the host a name.
2. Lanloom shows a **one-time code**. It is valid for **30 minutes** and works once. The window also shows both
   snippets below with your address and the code filled in; each has a copy button.
3. Run one of them on the host. The agent pairs itself with the code and from then on keeps a token of its own in its
   state volume. The window says "paired and reporting" when the first report arrived.

Compose file (also in [deploy/agent-compose.yaml](../deploy/agent-compose.yaml)):

```yaml
services:
  lanloom-agent:
    image: ghcr.io/robeertm/lanloom:<version>
    container_name: lanloom-agent
    command: ["python", "-m", "lanloom.agent", "run"]
    restart: unless-stopped
    network_mode: host
    pid: host
    user: "0:0"
    read_only: true
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    environment:
      LANLOOM_AGENT_SERVER: "http://<lanloom-address>:7380"
      LANLOOM_AGENT_CODE: "<code>"
    volumes:
      - lanloom-agent:/var/lib/lanloom-agent
      - /:/host:ro
      - /run/dbus/system_bus_socket:/run/dbus/system_bus_socket:ro
      - /var/run/docker.sock:/var/run/docker.sock:ro
volumes:
  lanloom-agent:
```

Or as one command:

```sh
docker run -d --name lanloom-agent --restart unless-stopped --network host --pid host --user 0:0 --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true -e LANLOOM_AGENT_SERVER=http://<lanloom-address>:7380 -e LANLOOM_AGENT_CODE=<code> \
  -v lanloom-agent:/var/lib/lanloom-agent -v /:/host:ro -v /run/dbus/system_bus_socket:/run/dbus/system_bus_socket:ro \
  -v /var/run/docker.sock:/var/run/docker.sock:ro ghcr.io/robeertm/lanloom:<version> python -m lanloom.agent run
```

Use the exact version your Lanloom runs instead of `<version>` (the pairing window fills in the right image). The last
two mounts are optional: leave out the bus socket and failed services and timers show as "not available"; leave out
the runtime socket and containers show as "not available". The code is only needed on the first start; afterwards the
agent uses its token and ignores a code it has already used.

Remove the agent: delete the entry in **Settings > Agents** (the token is revoked at once), then stop and delete the
container and its volume on the host.

## What every flag and mount is for

| Setting | Why |
|---|---|
| `network_mode: host` | the kernel's socket diagnostics and TCP tables are those of the network the program runs in; with the host network the agent sees every TCP conversation of the host, and the host's addresses |
| `pid: host` | the byte counters per container are read from each container's network namespace through `/proc/<pid>/net/dev`, and process names of listening services need the process to be visible. Without it the agent still works, but containers show no bytes and services no process name |
| `user: "0:0"` | the system bus and the other containers' namespaces are open to the host's super user id only; this is the only reason. Everything else is taken away, see the next three rows |
| `cap_drop: [ALL]` | the agent holds no capability at all |
| `read_only: true` | the agent's own file system cannot be written; only the state volume can |
| `no-new-privileges:true` | the process cannot gain privileges |
| `lanloom-agent:/var/lib/lanloom-agent` | the state volume: the token (file mode 0600) and the buffer of reports waiting for Lanloom |
| `/:/host:ro` | file system statistics of the host's disks and its release file. Read only. The agent asks for the size and free space of the paths in `LANLOOM_AGENT_DISKS` and reads one file there, the host's release file for the system label; it opens nothing else |
| `/run/dbus/system_bus_socket:...:ro` | the system message bus, to list failed services and read timers. The client can send exactly two kinds of requests, listing units and reading properties; a test checks that no other method name appears in its code |
| `/var/run/docker.sock:...:ro` | the container runtime's API. **`:ro` on a socket file does not stop writes through it**; it is the agent that never sends anything but `GET` (its request function has no method parameter, and a test checks that no other HTTP method appears in the code). If you want the separation to be enforced outside the agent, put a **socket proxy** in front of the runtime that allows `GET` on containers and the version only, and point `LANLOOM_AGENT_CONTAINERS` at it |
| an overlay VPN's status socket (optional) | only mounted if you want the peers of the overlay network listed |

Note that `pid: host`, `user: "0:0"` and the runtime socket together give a container far more reach than Lanloom
itself needs. Lanloom's own container runs without any of them. If that is too much for a host, read it through a
[container source](#a-container-source-without-an-agent) instead.

## Environment variables

| Variable | Meaning | Default |
|---|---|---|
| `LANLOOM_AGENT_SERVER` | address of Lanloom, `http://` or `https://`, without user name, password or query | required |
| `LANLOOM_AGENT_CODE` | the one-time pairing code (first start only) | none |
| `LANLOOM_AGENT_STATE` | folder for the token and the buffer | `/var/lib/lanloom-agent` |
| `LANLOOM_AGENT_PIN` | SHA-256 fingerprint of an own HTTPS certificate of Lanloom; the agent then accepts exactly that certificate instead of checking a certificate chain | none |
| `LANLOOM_AGENT_NAME` | host name to report instead of the host's own | the host's name |
| `LANLOOM_AGENT_DISKS` | comma-separated paths whose file systems are measured | `/host`, else `/` |
| `LANLOOM_AGENT_CONTAINERS` | address of the container runtime: `unix:///path.sock`, `http://host:port` or `tcp://host:port` | the usual runtime socket paths |
| `LANLOOM_AGENT_DBUS` | path of the system message bus socket | `/run/dbus/system_bus_socket` |
| `LANLOOM_AGENT_OVERLAY` | path of the overlay VPN's status socket | the usual socket paths, if mounted |
| `LANLOOM_AGENT_LOG` | log level (`DEBUG`, `INFO`, `WARNING`) | `INFO` |

Subcommands: `run` (the default), `once`, `selftest`, `version`.

## Checking what leaves the host

```sh
docker exec lanloom-agent python -m lanloom.agent once
```

prints the complete report the agent would send, as readable JSON, and **sends nothing**. It is the quickest way to see
exactly what leaves the host. `python -m lanloom.agent selftest` checks the secret filter with built-in samples (no
network); the command is `ok` or `FAILED`.

## Pairing, token and revoking

- An administrator creates a code in Lanloom (the route asks for a recent second confirmation of the sign-in). Up to ten
  codes can be open at once; every code is valid for 30 minutes and works once. Lanloom keeps only a hash of a code, and
  a code is never part of a backup.
- The agent sends the code once to `POST /api/agent/pair` and receives a **token** of its own. Lanloom stores only the
  **hash** of that token. The agent keeps the token in its state folder (mode 0600).
- A wrong code counts as a failed attempt for the sending address (the same lock-out as for other codes).
- **Revoking:** removing the entry in **Settings > Agents** deletes the hash. The agent's next report is answered with
  "refused", the agent drops its token and its buffer, logs that it stopped sending and then **waits quietly**
  (it does not exit, so the container runtime does not restart it in a loop). Pairing again needs a new code and a new
  start of the agent.
- Renaming an entry needs the operator role; creating a code, adding a source and removing need the administrator role.

## Transport and the buffer

- The agent talks only to the Lanloom it was paired with, at the address in `LANLOOM_AGENT_SERVER`.
- **One report every 10 seconds**: the summed TCP bytes of that interval and, on their own cadences, the container list
  (every 30 seconds), system values, listening services, units and overlay peers (once a minute), host facts
  (every 10 minutes). Each part is read on its own: one that fails reports "not available" and the rest goes on.
- **Buffer.** When Lanloom cannot be reached, reports wait: up to **720 reports, about two hours**, in memory and in
  the state volume, so a restart keeps them. The newest 30 keep all parts; older ones are thinned to bytes and system
  values. When Lanloom answers again, they are sent **oldest first**, in batches. The oldest are dropped when the
  buffer is full. A report Lanloom cannot take never blocks the ones after it.
- While Lanloom is unreachable, the agent retries with a growing pause (up to five minutes).
- With an `https://` address the certificate chain is checked as usual; with `LANLOOM_AGENT_PIN` the pinned
  fingerprint replaces that check. Plain `http://` is as safe as the network between the two hosts; use `https://`
  behind a reverse proxy when that is not a network you trust.

## Updates

The agent **never loads or downloads code**. It does not update itself.

- Lanloom compares the version in every report with its own. When they differ, the entry shows **Update** with the exact
  image to use, and the Nightly report notes it. The agent also says so once an hour in its log.
- **To update:** pull the image with the same version tag as your Lanloom shows and recreate the container, for
  example `docker compose pull && docker compose up -d` in the folder of the compose file, or remove the container and
  run the snippet again with the new tag (the state volume keeps the token; no new code is needed).
- Verify the signature of that image as described in [SECURITY.md](../SECURITY.md) before you pull it on a host that
  matters.
- A report whose protocol version Lanloom does not know is refused with a message that names the reason; update the
  agent to the same version.

## A container source without an agent

For a host that cannot or should not run the agent, Lanloom itself can read the containers through the runtime's API.

1. **Settings > Agents > Add container source.** Enter a name and the address: `unix:///path/to.sock` (a socket mounted
   into Lanloom's container), `http://host:port` or `tcp://host:port` (no user name or password). **Test** shows the
   runtime, its version and the number of containers.
2. Lanloom polls the source **every 30 seconds** with `GET` only, keeps the same list of fields as the agent (positive
   list, same filter) and shows the containers in the Running view and on the map.

A source gives containers only: no system values, no services, no timers and no conversations. For the address of a
host on the network use a **socket proxy** configured to allow `GET` on the containers and the version and nothing
else, and let only Lanloom's address reach it. Never expose the runtime's own socket to the network: with full rights
it can start, stop and create containers.

## What Lanloom keeps, and for how long

- **Per host, the latest state:** system values, disks, listening services, failed units, timers, containers, overlay
  peers. Held in memory, and copied to the database about once a minute so that a restart shows the last known state.
- **History:** one **5-minute sample** (load, memory, disk, failed units, containers, unhealthy) per host for **35 days**.
  It feeds the host health in the forecast.
- **Conversations** between devices are booked into the [data paths](manual/data-paths.md) (see below); bytes to the
  internet go into the data paths of that host, as countries.
- **Events** for the notification rules ([NOTIFICATIONS.md](NOTIFICATIONS.md)).
- **Backups** ([BACKUP.md](BACKUP.md)): the entries (name, kind, token hash, address of a source) are configuration and
  are in every backup; the 5-minute samples are history and only in a backup that includes history. Open invitation
  codes are never in a backup. After restoring on another host the agents keep working if they can reach the new
  address; otherwise pair again.

**Noise filter.** A container that lived less than 10 minutes is not listed on its own: one-off jobs and throw-away
containers only raise a counter ("short-lived containers hidden"). A container whose name was seen long-lived before,
a restarting one and an unhealthy one always show.

## Conversations

The agent reports TCP bytes per conversation: the host's address, the peer's address and the service port (the peer's
port for an outgoing connection, the own port for an incoming one). Single connections and ephemeral ports are not
kept. Lanloom matches the peer address with the devices it knows:

- bytes between the agent's host and another **known device** become a **dashed "talks to" line** on the map and a
  pair in the data paths;
- when **both ends run an agent**, both would report the same bytes; Lanloom books a conversation **once**, so no byte
  is counted twice;
- the lines fade with a half-life of about five minutes, and conversations under 2 KB are ignored.

## Resources

The agent needs a few tens of MB of memory and, apart from short readings every few seconds, no processor time worth
mentioning, also on a small single-board host. A report is a few kB, sent every 10 seconds. Lanloom keeps per host one
state and one sample per 5 minutes.

## Limits

- Only **IPv4 and IPv6 TCP** conversations carry bytes. UDP services are listed, but their bytes are not counted.
- **Process names** need the agent to see the process (`pid: host`). Without it a service shows as "unknown process".
- On a host without the bus socket, or without a service manager, **units are "not available"**.
- Containers on the host network have no counters of their own and show no bytes.
- **Without the runtime socket** there are no containers. A container whose process the agent cannot see gets its bytes
  from the runtime's one-shot statistics (runtime API version 1.41 or newer); a runtime that does not answer that call
  shows no bytes for it.
- A host that is offline cannot report; Lanloom marks it **offline** after 90 seconds without a report.
- At most 200 agents and container sources, 60 reports per call, 400 containers per host.

## Protocol summary

Version `1`, JSON over HTTP, both calls made by the agent:

| Call | Auth | Body |
|---|---|---|
| `POST /api/agent/pair` | the one-time code in the body | `{code, proto, version, host}` -> `{agent_id, token, server_version, interval}` |
| `POST /api/agent/report` | `Authorization: Bearer <token>` | `{proto, version, reports: [...]}`, at most 60 reports and 4 MB per call -> `{server_version, ...}` |

The agent sends the header `X-Lanloom: 1` as every other state-changing call does. `401` on a report means the token
was revoked. The exact egress of the agent is listed in `security/egress.json` (category `agent-link`) like every
other connection of Lanloom.

## Troubleshooting

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md#host-agent-and-container-source) for not paired, offline, units or
containers "not available", no bytes per container and a version that differs.
