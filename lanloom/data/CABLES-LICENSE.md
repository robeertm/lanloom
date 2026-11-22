# Cable data: licence and source

Two separate files next to this one hold the cables of the *Real cables* view of the data paths map:

- `cables-osm.json.gz`: the sea and land telecommunication cables of the community map (ODbL, sections below up to
  "Completeness");
- `cables-open.json.gz`: more cable lines and cable facts from open sources in the public domain or under CC0 1.0 (section
  "Open sources" at the end).

## Community-mapped cables (`cables-osm.json.gz`)

- **Source:** [OpenStreetMap](https://www.openstreetmap.org/), © OpenStreetMap contributors
  ([copyright and licence](https://www.openstreetmap.org/copyright)).
- **Licence:** [Open Database License 1.0 (ODbL)](https://opendatacommons.org/licenses/odbl/1-0/). The file is a
  *Derivative Database* of OpenStreetMap and is made available under the ODbL. This licence covers the data file only;
  it does not cover the program code of Lanloom (see `LICENSE` in the repository root) or any other bundled data.
- **Built with:** `scripts/build_cables.py`, which runs the Overpass queries listed in `lanloom/cables.py` (`QUERIES`,
  also stored in the file itself under `queries`) at build time. The app never downloads cable data by itself.
- **Date of the data:** stored in the file (`date`).
- **Checksum:** `cables-osm.sha256` (written by the build script; a test compares it).

### What was changed (ODbL 4.6: alterations are stated)

The file is not a copy of the OpenStreetMap data. The build script:

1. keeps ways tagged `communication=line` or `telecom=line` with `location=underwater|submarine` or `submarine=yes`, ways
   tagged `seamark:type=cable_submarine`, and land ways tagged `communication=line` or `telecom=line` that carry a name (or an
   operator);
2. leaves out power cables, pipelines and every way marked disused, abandoned, removed or decommissioned, and retired
   cable systems;
3. bundles ways of the same name (or reference) into one cable;
4. leaves out unnamed sea pieces shorter than 5 km, unnamed land lines without an operator or shorter than 100 km, and land
   cables shorter than 30 km;
5. simplifies the lines (Douglas-Peucker, 0.05 map units), projects them onto the world map of the app (Natural Earth I,
   width 1000) and rounds coordinates to 0.01;
6. keeps per cable only: an identifier derived from the smallest OpenStreetMap way id, the name, the operator, the line,
   the length in km and the two ends furthest apart.

### Collective database or derivative database

- `cables-osm.json.gz` is a **Derivative Database** (ODbL 1.0, section 1): it was made by filtering and transforming
  OpenStreetMap data. It stays under the ODbL, carries its notice in the file (`notice`, `license`, `attribution`), and is
  offered in full: it is part of the repository and of the container image, and the build script states how it was made
  (ODbL 4.6).
- Lanloom ships this file **next to** its own code and other data (base map, location data) without merging them: together
  they form a **Collective Database** in the sense of ODbL 4.5 (a), so the ODbL does not extend to the rest of Lanloom.
- The pictures of the map that use the cables are a **Produced Work** (ODbL 4.3): the app shows the notice
  "© OpenStreetMap contributors (ODbL)" beside the map whenever cables are drawn, and the README names the source.
- An administrator may import an Overpass answer of their own (Settings > Data paths). The imported file is again a
  derivative database of OpenStreetMap under the ODbL; it stays on that instance.

### Completeness

OpenStreetMap is **incomplete, community-mapped**: not every cable is mapped, many cables have no name, and land cables are
rare. The app says so wherever the cables are shown and never claims to show all cables. Which cable a measured route used
is always an **estimate**, never a measurement.

## Open sources (`cables-open.json.gz`)

This file is **not** derived from the community map and is not under the ODbL. It holds data from two sources, both
dedicated to the public domain under [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/); every line names its
source (`src`), every cable fact its item (`wd`). The file lists its sources itself (`sources`). CC0 asks for no attribution;
Lanloom names both sources anyway, beside the map and in the settings.

| Source | What is used | Licence |
|---|---|---|
| NOAA Office for Coastal Management and BOEM (U.S. Bureau of Ocean Energy Management), *Submarine Cables* of MarineCadastre.gov ([catalogue entry](https://catalog.data.gov/dataset/submarine-cables)) | Cable corridors in U.S. waters (mainland, Alaska, Hawaii, Caribbean and Pacific territories, Great Lakes): name, owner, status, corridor | CC0 1.0 (U.S. public data, as published in the catalogue) |
| [Wikidata](https://www.wikidata.org/wiki/Q506572), items that are a *submarine communications cable* | Label and aliases, owners or operators, year in service, length, design capacity, countries, year out of service | CC0 1.0 ([data licence](https://www.wikidata.org/wiki/Wikidata:Licensing)) |

- **Built with:** `scripts/build_cables.py` (step `open`): one download of the national data set (a zip with a GeoPackage) and
  one SPARQL query (`lanloom/cables_open.py`, `WIKIDATA_QUERY`), at build time only. The app never downloads cable data.
- **Checksum:** `cables-open.sha256` (written by the build script; a test compares it).

### What was changed

1. National data: only named cable systems in service or under construction. Power, utility, telegraph, research, range and
   military cables, and cables out of service or abandoned, are left out. Owner fields keep company names only.
2. The corridor of each cable (a thin area around it) becomes a centre line, simplified (Douglas-Peucker, 0.05 map units),
   projected onto the world map of the app and rounded to 0.01.
3. Only the part in U.S. waters is known: where the national data stops out at sea, the line ends there. Such an end is not
   treated as a landing point (`part`), only ends near land are.
4. A line whose cable the community map already holds (same name, alias or the same Wikidata item) is left out: every cable is
   drawn once, the community line wins.
5. Cable facts only fill gaps (an operator of the community map is never replaced). A cable that Wikidata lists as out of
   service is left out of the lines, so it never becomes a candidate of the route estimate.

### Not used, and why

Databases that are only for paying subscribers, data that may not be passed on in bulk, data whose licence is unclear or
missing on the publisher's own page, sources under a non-commercial or share-alike licence that would bind the code, and
encyclopaedia articles (the facts come from the CC0 knowledge base instead). The view says what it shows: incomplete,
partly community-mapped, and which cable a route used is always an estimate.

