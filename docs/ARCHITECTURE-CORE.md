# Core and curation: architecture decision

Status: accepted (Lanloom 1.1.0)

## Context

Lanloom is the public, generic product. Its author also maintains a private *downstream* map built on the same board engine, with private data, private views, private collectors and its own server. Under the license, modification and redistribution need prior written permission, so this document describes how the author's own downstream map is structured. It does not invite forks. Before 1.1.0 the downstream map carried a translated copy of the board engine, so every improvement had to be made twice. That doubled the work, and the copies drifted apart.

## Decision

**Lanloom is the single source of the core.** The downstream map consumes the core as a **pinned, vendored copy guarded by a lock file**. They never edit it.

1. **What the core is (stage 1).** The browser board engine. Its modules are framework-free, have no dependencies, have no knowledge of the host application, and run in a window, in a Web Worker and under Node.

   | File | Namespace | Purpose |
   |---|---|---|
   | `lanloom/web/board/router.js` | `Lanloom.Router` | circuit-board trace router (octilinear runs, pads, buses, vias) |
   | `lanloom/web/board/bundles.js` | `Lanloom.Bundles` | trunk bundling: traces that end at the same pad share one trunk |
   | `lanloom/web/board/endmarks.js` + `.css` | `Lanloom.Endmarks` | labels where traces leave the board towards off-board peers |
   | `lanloom/web/board/portmarks.js` + `.css` | `Lanloom.Portmarks` | port / protocol marks at trace ends |

   The machine-readable list is `core-manifest.json` (path, namespace, API level).

2. **How a downstream map consumes it.**
   - A small fetcher in the downstream repository reads `core-manifest.json` from a Lanloom release tag and copies the files byte-for-byte into the downstream tree.
   - It records version, commit and the SHA-256 of every file in a lock file.
   - A mandatory test fails on any difference.
   - Because the copy is committed, a downstream build needs no network access and no registry, and its own release path stays as it is: building from its own tag, selecting tests from changed files, and its own updater.

3. **How the downstream layer talks to the core.** Only through interfaces:
   - **Host object.** Tiles, traces, obstacles, scale, events and per-end facts (port, uplink port, own port, radio band/SSID, service port, protocol). The core never reads host globals.
   - **Options** per view, e.g. the bundling port key. `null` means never bundle at that end.
   - **i18n callback** `t(key, params)`. The core ships English and German keys.
   - **Theme.** CSS custom properties `--ll-*`, each with a fallback to Lanloom's own tokens.

   Data catalogs, private views, private probe kinds, bridges and access control stay in the downstream layer and its own server. The core is the engine, not the server.

4. **Where changes are made.** A fix or feature in a core module is made in Lanloom first. It goes through Lanloom's gate (tests, leak and brand check, CI candidate, test network, updater bench) and is released. The downstream map then fetches the new tag. A downstream hot-fix never edits the vendored copy.

## Alternatives considered

| Option | Why not |
|---|---|
| Downstream image `FROM ghcr.io/…/lanloom:<v>` plus overlay | The downstream map has its own server and many private modules, so the base image would only add weight. Builds would depend on a registry pull, and test selection by changed file would not see core changes. |
| Python/npm package | Same registry dependency at build time. Also the browser modules are plain scripts with no bundler. |
| Git subtree of the whole Lanloom repo | Brings the whole application into the downstream tree and makes merges noisy. A manifest with a lock is explicit and easy to diff. |
| Keep two copies | That is the problem being solved. |

## Consequences

- Every core change costs one Lanloom release before the downstream map can use it. Lanloom patch releases are cheap, and the gate runs its container stages in CI.
- The core API is versioned (`API` constant per module, `api` in the manifest). Breaking changes raise it.
- **Later stages** can move further generic parts into the core the same way: the SNMP client, the byte quantizer, the WebGL dot layer and test helpers.
