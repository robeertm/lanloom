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
| `LANLOOM_COOKIE_SECURE` | `auto` | `auto`, `always` or `never` |
| `LANLOOM_SECRET_KEY_FILE` | in data directory | location of the encryption key file |
| `LANLOOM_DEMO` | `0` | `1` = fictional demo network, nothing is scanned (add `LANLOOM_DEMO_SEED=1` to seed it) |

## Behind a reverse proxy

Put Lanloom behind a TLS reverse proxy for remote access and passkeys. Two settings matter:

- `LANLOOM_TRUSTED_ORIGINS`: the public origin the browser sees, for example `https://lanloom.example.org`.
- `LANLOOM_TRUSTED_PROXIES`: the address (or network) the proxy connects from, for example `127.0.0.1` or `172.18.0.0/16`.

With `LANLOOM_TRUSTED_PROXIES` set, the sign-in rate limit counts the client address the proxy reports in `X-Forwarded-For` (read from the right: the right-most entry that is not one of your proxies) or `X-Real-IP`, and `X-Forwarded-Proto: https` marks the session cookie `Secure`. The headers are ignored from every other address. IPv6 clients are counted per /64.

Leave it empty when nothing sits in front of Lanloom. Without it, all sign-ins through a proxy share the proxy's address: one person typing wrong passwords then locks everybody out for a while. List only proxies you control. A wrong entry lets anybody who can reach Lanloom from that address choose their own client address and slip past the rate limit.

## Image tags

`:1.0.0` is immutable. `:1` and `:latest` move only after the published image has been tested. `:edge` is the latest build of the main branch.

## Updating

With the updater sidecar, an "Update" button appears when a new version is available. It pulls the image, runs a self-test without network, starts a trial on a copy of the data, backs up the database to `/data/backups` (5 are kept) and tags the old image `:previous`, swaps the container, checks health and rolls back automatically on failure. Only one update runs at a time; a second request while one is waiting or running is refused. The update check takes the highest stable version from the list of releases, so a later bug-fix release for an older line never hides a newer version.

Without the sidecar:

```sh
docker compose pull && docker compose up -d
```

## Backup

Copy the data volume, or the database backups in `/data/backups`. Keep `secret.key` together with the database, otherwise stored integration credentials cannot be decrypted.

## Forgot password

Stop the container, delete `lanloom.db` in the data volume and start again. This loses the inventory and settings and is the only way to reset access.

## Uninstall

```sh
docker compose down
docker volume rm lanloom_lanloom-data    # deletes all data
```
