# Vulnerability hints

Lanloom lists published vulnerabilities that match the maker, model and firmware it knows for a device, from an offline
feed that comes with every release. This page explains the rules; the view itself is described in the manual
([manual/vulnerabilities.md](manual/vulnerabilities.md)).

**In short:** no cloud, no download at run time, no test of the device. A hint is information about published data,
phrased with what to do, never an alarm. Lanloom prefers no hint to a wrong one.

## The feed

The image carries `lanloom/data/vulnfeed.json.gz` (about 0.3 MB). It is built from two public sources:

- the **catalog of known exploited vulnerabilities** of the U.S. Cybersecurity and Infrastructure Security Agency
  (CC0 1.0): which vulnerabilities are exploited in the wild, since when, and whether ransomware uses them;
- the **affected-version ranges of the national vulnerability database** (NVD): the machine-readable configurations
  (CPE match criteria) that vendors and analysts published for each record, and its CVSS severity.

Only **device firmware** is kept: a firmware product or a hardware product with its firmware ranges, with a model name
that contains a digit. Records of operating systems and applications on computers are left out, because Lanloom cannot
read their exact build from the network. The bundled feed holds every catalog entry of known exploited vulnerabilities
that names device firmware, plus the **critical** device-firmware records published since 2020.

A new feed arrives with each Lanloom release. The nightly report notes once when the feed is older than 180 days.

*This product uses data from the NVD API but is not endorsed or certified by the NVD.* Licences: see
[THIRD-PARTY-NOTICES.md](../THIRD-PARTY-NOTICES.md).

### Building a feed file

Download the inputs yourself (they are only parsed as JSON, never executed), then run the build script of the
repository on any computer with Python 3:

```sh
# the catalog of known exploited vulnerabilities (JSON feed)
curl -o kev.json https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json
# every NVD record of that catalog, plus critical records in periods of at most 120 days (respect the public rate limit)
curl -o nvd-kev.json 'https://services.nvd.nist.gov/rest/json/cves/2.0?hasKev&resultsPerPage=2000'
curl -o nvd-crit-1.json 'https://services.nvd.nist.gov/rest/json/cves/2.0?cvssV3Severity=CRITICAL&pubStartDate=2026-07-01T00:00:00.000&pubEndDate=2026-10-28T23:59:59.999&resultsPerPage=2000'
python3 -I scripts/build_vulnfeed.py --kev kev.json --nvd nvd-*.json --out feed.json.gz
```

The script validates every entry with the same rules the app uses and reads the result back before it writes it. The
output is the same for the same input (no time stamp in the file). Import it under *Vulnerabilities › Vulnerability feed ›
Import feed file …* (admins); its entries are added to the bundled ones and an entry with the same identifier replaces
the bundled one. *Remove imported feed* goes back to the bundled feed. A broken file is refused; a broken imported file
on disk is ignored at start.

### Format

```json
{"format": 1, "as_of": "2026-11-18", "sources": ["..."],
 "entries": [{"id": "CVE-2026-0001", "sev": "critical", "score": 9.8, "kev": "2026-02-01", "ransom": false,
              "title": "...", "action": "update",
              "match": [{"v": "examplenet", "p": "nas32", "r": [{"lt": "5.21(aazf.10)c0"}]}]}]}
```

- `v`, `p`: maker and model in normalised form (lower case, letters and digits only, `+` written as `plus`).
- `r`: affected firmware ranges. `ge`/`gt` is a lower bound, `lt`/`le` an upper bound, `eq` one version; an empty list
  means every version is affected.
- `kev`: the date the catalog of known exploited vulnerabilities listed it (empty when not listed).
- `action`: `replace` when the catalog says the product is at its end of life and no fixed firmware exists, else
  `update`.

At most 50 000 entries, 64 models per entry and 16 ranges per model; 8 MB compressed per import.

## Matching

For each device Lanloom collects every combination of maker, model and firmware it knows: from a connected controller
family, from the hub's device registry, and from what the device itself reports (UPnP, mDNS, SNMP), with the firmware
typed into the inventory taking precedence. A hint needs all of this:

1. **The model matches exactly.** The device's model in normalised form (also without a leading maker name) equals the
   feed's model. `NAS3` and `NAS320` never match `NAS32`; a model without a digit (`Router`) is too general and is never
   used. A model known only from the hardware address register never counts.
2. **The maker matches.** The feed's maker equals the first one to four words of the device's maker, joined
   (`EX-LINK Technologies` → `exlink`). Never a part of a word: `hp` does not match `hpe`.
3. **The firmware lies inside a range** - or the feed lists every version as affected. Versions are compared part by
   part (numbers as numbers), starting at the first dotted number (`V5.21(AAZF.13)C0` → `5.21(aazf.13)c0`). Two versions
   are only compared when their letter parts agree: `5.21(aasz.3)c0` is another hardware branch than
   `5.21(aazf.10)c0`, and `1.0` is not compared with `1.0rc1`. When they cannot be compared, Lanloom says so and gives
   no hint.

| Result | Rule |
|---|---|
| **certain** | 1-3, the firmware from a person (inventory), a connected controller, the hub or SNMP, and the maker not only from the hardware address |
| **possible** | 1-3 with the firmware read from an announcement (UPnP, mDNS), or the maker only from the hardware address, or every version affected |
| not checked | maker, model or firmware unknown (when the feed lists the model), or a firmware that cannot be compared |
| not affected | the firmware lies outside every range the feed lists for the model |
| none listed | the feed lists nothing for the model |

Retired (decommissioned) devices, secondary interfaces of a device and Lanloom's own host are not checked. The result is
kept for 20 seconds and computed again after a change of the inventory firmware, a scan or an import.

Hints are sorted by severity, then *exploited in the wild*, then certain before possible, then the score.

## What to do

Each hint says one of:

- **Update the firmware to X or newer** - when the matching range has an upper bound (the first fixed version).
- **Look for a firmware update on the maker's support page** - when the range has no fixed version but the catalog does
  not say the product is at its end.
- **Disconnect or replace the device**, at least keep it away from the internet - when no fixed firmware is known
  (every version affected, or the catalog says the product is at its end of life).

*Does not apply …* (operators) sets a hint aside with a note; it stays visible under *Set aside*.

## Notifications and nightly report

- **Event** `vulnerability` (*Vulnerability hint*, group *Incidents*): checked hourly (first four minutes after the
  start), announced once per device and hint (it is not repeated after a restart). Certain or exploited hints are a
  warning, the rest information; like every event it goes into the nightly report unless someone asks for more.
  See [NOTIFICATIONS.md](NOTIFICATIONS.md).
- **Nightly report** section *Vulnerability hints* (only when there are open hints or a note about the feed's age): the
  hints first seen in that night, each with what to do (certain or exploited ones are points to tick off), and the open
  total. See [NIGHTLY-REPORT.md](NIGHTLY-REPORT.md).

## API and roles

| Route | Role | What |
|---|---|---|
| `GET /api/vulns?show=&severity=&q=` | viewer | the hints (`show`: `exploited`, `certain`, `possible`, `dismissed`), the summary, the devices not checked with their reason, the feed |
| `GET /api/devices/{id}/vulns` | viewer | the result of one device |
| `GET /api/vulns/feed` | viewer | date, number of entries, sources, imported feed |
| `POST /api/vulns/dismiss` `{device, id, note}` | operator | set a hint aside (audit `vulns.dismiss`) |
| `POST /api/vulns/restore` `{device, id}` | operator | bring it back (audit `vulns.restore`) |
| `POST /api/vulns/feed` (body: the feed file) | admin | import a feed file (audit `vulns.feed_import`) |
| `DELETE /api/vulns/feed` | admin | remove the imported feed (audit `vulns.feed_remove`) |

The audit log records the length of a note, never its text.

## Limits

- No guarantee: an empty list is no proof that the network is safe.
- No test of the device: Lanloom compares facts, it never probes.
- Only device firmware with machine-readable ranges; computers' operating systems and applications are not covered.
- As good as what devices report and as new as the feed.
