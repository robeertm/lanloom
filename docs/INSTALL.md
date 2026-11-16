# Installation

Lanloom runs as a container. You need a container runtime with compose v2. Images are published for amd64 and arm64.

Read the notice at the top of the [README](../README.md) before installing.

## One-line install

```sh
curl -fsSL https://raw.githubusercontent.com/robeertm/lanloom/main/deploy/install.sh | sh
```

It installs into `~/lanloom`, uses port 7380 and enables the updater sidecar. Options (pass after `sh -s --`):

| Option | Meaning |
| --- | --- |
| `--dir <path>` | install directory (default `~/lanloom`) |
| `--port <n>` | web port (default 7380) |
| `--no-updater` | do not enable the updater sidecar |
| `--no-update-check` | never contact the release feed |
| `--with-ai` | also run a local AI service next to Lanloom (adds `compose.override.yaml`; a model of several GB is downloaded only when you pick one in Settings, AI). See [integrations/ai.md](integrations/ai.md) |

The installer is idempotent: running it again updates the installation and never deletes data.

## Docker Compose

Use `compose.yaml` from the repository:

```sh
docker compose up -d                      # app only
docker compose --profile updater up -d    # app + one-click updater
```

## Plain docker run

```sh
docker run -d --name lanloom --network host --restart unless-stopped \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  --label org.lanloom.role=app -v lanloom-data:/data \
  ghcr.io/robeertm/lanloom:latest
```

## First start

Open `http://<host>:7380`. Enter the one-time setup code from the log:

```sh
docker logs lanloom 2>&1 | grep -i "setup code"
```

It is also in `setup-code.txt` in the data volume. The wizard then guides you through account, networks, scan, review, integrations and finishing. Passwords need at least 10 characters. Passkeys are offered on HTTPS or localhost.

### The setup assistant

1. **Networks.** Lanloom reads the host's network interfaces, routes, neighbour table, `resolv.conf` and DHCP lease files; no packet is sent. It shows the gateway, DNS servers and DHCP server it found and proposes the ranges to map: private addresses only, at most a /22 each. If an interface network is bigger, it is narrowed to the smallest /24 to /22 around this host that holds all known neighbours. Routed private networks behind a router are listed but never pre-ticked. You must tick the consent box before anything is scanned. *Start gentle scan* begins directly; *Scan options ...* leads to the choice of the deep scan.
2. **First picture.** After the quick phases (neighbour table, mDNS, SSDP, WS-Discovery) the devices already appear as a preview, before ports and banners are read. *Open the map now* leaves the wizard while the scan continues in the background. The time from the first setup screen to the first picture is shown at the end; the target is under five minutes.
3. **Review and connect.** The automatic classification can be corrected. In the connect step Lanloom suggests integrations based only on what the gentle scan saw (open ports, web page titles, mDNS and SSDP announcements). Each suggestion has a guide for a read-only account or token, a form prefilled with address and port (never credentials) and a *Test* button; a failed test says what to look at: rejected credentials, certificate, not reachable, or other. Everything else stays under *Other sources*.
4. **Done.** Rescan interval and update check.

**Sample network.** *Explore a sample network* (networks step, and Settings › Setup assistant) opens a read-only demo with a fictional network. It lives in a separate in-memory store beside your real inventory, per browser session (cookie), under a *Sample network* banner with *Leave the sample*. Every change is refused there. It ends with sign-out and stops after 15 minutes without activity. Your data is not read or changed.

**Run it again.** Administrators can start the assistant again under Settings › Setup assistant › *Run the assistant again*. Devices, names and connections stay. *Back to the map* in the wizard header leaves it at any time.

**No network detected?** The assistant shows no ranges when the container only sees its own isolated network. Run the container with host networking (`network_mode: host` in compose, `--network host` with `docker run`) so it sees the interfaces and neighbours of the host, then reload the wizard. Docker Desktop on macOS and Windows cannot do this; see Platforms below.

## Platforms

### Linux server or small single-board computer

Full feature set with host networking (neighbour table, mDNS, SSDP, byte counters). Use the one-line install or compose. If you get a permission error, run with `sudo` or add your user to the container runtime's group.

### NAS with a container manager

Create a project from `compose.yaml` and keep the host network. If you replace the named volume with a bind mount, the folder must be writable by uid 10001. Or simply keep the named volume.

### macOS and Windows desktop container runtimes

Host networking is limited on these systems. Run in bridge mode instead, without `--network host`, and publish the port:

```sh
docker run -d --name lanloom -p 7380:7380 --restart unless-stopped \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  -v lanloom-data:/data ghcr.io/robeertm/lanloom:latest
```

Enter your network range manually in the wizard (for example `192.168.1.0/24`). Only TCP probes work: there is no neighbour table, no mDNS or SSDP, and no byte counters of the host. This setup is recommended only for trying Lanloom.

## Configuration

Set environment variables in the compose file.

| Variable | Default | Meaning |
| --- | --- | --- |
| `LANLOOM_PORT` | `7380` | web port |
| `LANLOOM_HOST` | `0.0.0.0` | listen address |
| `LANLOOM_DATA` | `/data` | data directory |
| `LANLOOM_UPDATE_CHECK` | `1` | `0` = never contact the release feed |
| `LANLOOM_TRUSTED_ORIGINS` | empty | comma-separated public origins when behind a reverse proxy |
| `LANLOOM_TRUSTED_PROXIES` | empty | comma-separated addresses or networks of your reverse proxies; only they may set `X-Forwarded-For`, `X-Real-IP` and `X-Forwarded-Proto` |
| `LANLOOM_AUTH_HEADER` | empty | header with the user name set by your authentication proxy, for example `Remote-User`; empty = off. Believed only from `LANLOOM_TRUSTED_PROXIES`. See [ACCESS.md](ACCESS.md#sign-in-through-an-authentication-proxy) |
| `LANLOOM_AUTH_GROUPS_HEADER` | empty | header with the user's groups, separated by commas or spaces |
| `LANLOOM_AUTH_GROUP_ROLES` | empty | group to role map such as `net-admins=admin,net-ops=operator,family=viewer`; the highest match wins |
| `LANLOOM_AUTH_DEFAULT_ROLE` | empty | role for proxy users without a matching group; empty = refused |
| `LANLOOM_BASE_PATH` | empty | serve below a path such as `/lanloom` (see below) |
| `LANLOOM_COOKIE_SECURE` | `auto` | `auto`, `always` or `never` |
| `LANLOOM_SECRET_KEY_FILE` | in data directory | location of the encryption key file |
| `LANLOOM_DEMO` | `0` | `1` = fictional demo network, nothing is scanned (add `LANLOOM_DEMO_SEED=1` to seed it) |

## Behind a reverse proxy

Put Lanloom behind a TLS reverse proxy for remote access and passkeys. Two settings matter:

- `LANLOOM_TRUSTED_ORIGINS`: the public origin the browser sees, for example `https://lanloom.example.org`.
- `LANLOOM_TRUSTED_PROXIES`: the address (or network) the proxy connects from, for example `127.0.0.1` or `172.18.0.0/16`.

With `LANLOOM_TRUSTED_PROXIES` set, the sign-in rate limit counts the client address the proxy reports in `X-Forwarded-For` (read from the right: the right-most entry that is not one of your proxies) or `X-Real-IP`, and `X-Forwarded-Proto: https` marks the session cookie `Secure`. The headers are ignored from every other address. IPv6 clients are counted per /64.

To serve Lanloom below a path such as `/lanloom/`, either let the proxy strip the prefix (the web page derives all addresses from its own location) or set `LANLOOM_BASE_PATH=/lanloom` so Lanloom accepts the prefix itself; requests without it still work and `/lanloom` redirects to `/lanloom/`. To let the proxy sign people in, see [ACCESS.md](ACCESS.md#sign-in-through-an-authentication-proxy).

Leave it empty when nothing sits in front of Lanloom. Without it, all sign-ins through a proxy share the proxy's address: one person typing wrong passwords then locks everybody out for a while. List only proxies you control. A wrong entry lets anybody who can reach Lanloom from that address choose their own client address and slip past the rate limit.

## Image tags

`:1.0.0` is immutable. `:1` and `:latest` move only after the published image has been tested. `:edge` is the latest build of the main branch.

## Updating

With the updater sidecar, an "Update" button appears when a new version is available. It pulls the image, runs a self-test without network, starts a trial on a copy of the data, backs up the database to `/data/backups` (5 are kept) and tags the old image `:previous`, swaps the container, checks health and rolls back automatically on failure. The new container keeps the image reference of your compose file (normally `:latest`), and that tag is moved to the new version before the swap (and back on a rollback), so a later `docker compose up -d` or a restart of the host always starts the version that runs now. Only one update runs at a time; a second request while one is waiting or running is refused. The update check takes the highest stable version from the list of releases, so a later bug-fix release for an older line never hides a newer version.

Without the sidecar:

```sh
docker compose pull && docker compose up -d
```

An updater sidecar from before 1.17 created the new container with the version tag and, after a second one-click update, left `:latest` on an older version, so `docker compose up -d` could start that older version again. The settings page shows a note while such a sidecar runs. Renew it once; from then on it keeps the tag itself (and repairs a lagging `:latest` when it starts):

```sh
docker compose pull && docker compose --profile updater up -d
```

## Backup

**In the app (recommended):** *Settings › Backup* downloads the whole configuration as one file encrypted with your passphrase (accounts, second factors, connections with their credentials, names you set, "connected via" entries, drafts, settings, audit log, optionally history) and writes automatic backups on a schedule into `/data/config-backups`. The nightly report says when the newest backup is too old. Restore it on the same instance (*Settings › Backup › Restore*) or on a new one from the first-start page (*Restore from a backup instead*), also on another architecture (amd64 ↔ arm64). Details, what happens on a restore, and how to move to another host: [BACKUP.md](BACKUP.md).

**By hand:** stop the container and copy the data volume, or copy `lanloom.db` together with `secret.key` (without the key, stored credentials cannot be decrypted). The updater keeps a copy of the database from before each update in `/data/backups`.

Copy backups off the host now and then: a backup on the same disk does not survive a lost disk.

## Move to another host

1. Old host: *Settings › Backup › Back up now* (note the passphrase).
2. New host: install Lanloom (any section above), open it, choose *Restore from a backup instead* on the first-start page, enter the setup code, the file and the passphrase.
3. Sign in with an account from the backup. The new host has its own instance key; old sessions are not valid there. See [BACKUP.md](BACKUP.md#move-to-another-host) for passkeys and connections.
4. Switch the old instance off.

## Forgot password

First ask an admin: *Settings → Users* creates a one-time reset link for you (valid 24 hours). See [ACCESS.md](ACCESS.md#lost-password-or-lost-phone).

Only if no admin can sign in (for example the only admin forgot the password): stop the container, delete `lanloom.db` in the data volume and start again. This loses the inventory and settings and is the last resort.

## Uninstall

```sh
docker compose down
docker volume rm lanloom_lanloom-data    # deletes all data
```
