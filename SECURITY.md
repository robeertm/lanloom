# Security

## Reporting a vulnerability

Please report vulnerabilities privately by opening a security advisory on the repository:
<https://github.com/robeertm/lanloom/security/advisories/new>

Do not open a public issue for security problems. Include the version, a description, and steps to reproduce if you can. Reports are handled on a best-effort basis.

## Security model

- **Sign-in is mandatory.** There is no open mode. The first account can only be created with the one-time setup code printed to the log and written to `setup-code.txt` in the data volume.
- **Passwords** are at least 10 characters and hashed with argon2id. Passkeys (WebAuthn) are available in secure contexts (HTTPS or localhost).
- **Sessions** are random 256-bit tokens in an `HttpOnly`, `SameSite=Strict` cookie (`Secure` on HTTPS). They last 30 days and expire after 14 days of inactivity.
- **Login rate limit:** after 5 failed sign-ins from the same client address, that address has to wait, starting at 60 seconds and doubling up to 1 hour.
- **Cross-site protection:** state-changing requests need the header `X-Lanloom: 1` and a matching `Origin`. Behind a reverse proxy, list the public origin in `LANLOOM_TRUSTED_ORIGINS`.
- **Strict Content Security Policy** on the web interface.
- **Container hardening:** runs as an unprivileged user (uid 10001) with a read-only root file system, all capabilities dropped and `no-new-privileges`.
- **Read-only by construction.** The scanner only sends the probes described in the README, never targets public ranges and never attempts logins. Integrations use strict allow-lists: the hub connection can only authenticate, read states, configuration and registries, and subscribe to state-change events; the MQTT client can only connect, subscribe, ping and disconnect; SNMP uses Get, GetNext and GetBulk only.
- **Credentials** for integrations are encrypted with AES-256-GCM using a key file (`secret.key`, mode 0600) in the data volume. They are never shown again and never logged. Anyone with access to the data volume can read both the key and the encrypted values, so protect the host and its backups.
- **No telemetry.** The only outbound connection is the optional update check against the public release feed (`api.github.com`), at most every 6 hours and only while the page is open. Turn it off with `LANLOOM_UPDATE_CHECK=0` or in the settings.

## Things to be aware of

- Host networking means the container shares the host's network stack.
- The optional updater sidecar mounts the container-runtime socket, which is root-equivalent on the host. Leave it out if you do not want that.
- Only scan networks you own or are allowed to scan. The deep scan may trigger security alerts.
- Lanloom serves plain HTTP. For remote access or passkeys, put it behind a TLS reverse proxy. Never expose it directly to the internet.
- Classifications and measurements are observations, not guarantees.
