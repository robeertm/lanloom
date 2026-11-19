# Cable data: licence and source

`cables-osm.json.gz` (next to this file) holds the sea and land telecommunication cables of the *Real cables* view of the
data paths map.

- **Source:** [OpenStreetMap](https://www.openstreetmap.org/), © OpenStreetMap contributors
  ([copyright and licence](https://www.openstreetmap.org/copyright)).
- **Licence:** [Open Database License 1.0 (ODbL)](https://opendatacommons.org/licenses/odbl/1-0/). The file is a
  *Derivative Database* of OpenStreetMap and is made available under the ODbL. This licence covers the data file only;
  it does not cover the program code of Lanloom (see `LICENSE` in the repository root) or any other bundled data.
- **Built with:** `scripts/build_cables.py`, which runs the Overpass queries listed in `lanloom/cables.py` (`QUERIES`,
  also stored in the file itself under `queries`) at build time. The app never downloads cable data by itself.
- **Date of the data:** stored in the file (`date`).
- **Checksum:** `cables-osm.sha256` (written by the build script; a test compares it).

## What was changed (ODbL 4.6: alterations are stated)

The file is not a copy of the OpenStreetMap data. The build script:

1. keeps ways tagged `communication=line` with `location=underwater|submarine`, ways tagged `seamark:type=cable_submarine`,
   and land ways tagged `communication=line` or `telecom=line` that carry a name (or an operator);
2. leaves out power cables, pipelines and every way marked disused, abandoned, removed or decommissioned, and retired
   cable systems;
3. bundles ways of the same name (or reference) into one cable;
4. leaves out unnamed sea pieces shorter than 5 km, unnamed land lines without an operator or shorter than 100 km, and land
   cables shorter than 30 km;
5. simplifies the lines (Douglas-Peucker, 0.05 map units), projects them onto the world map of the app (Natural Earth I,
   width 1000) and rounds coordinates to 0.01;
6. keeps per cable only: an identifier derived from the smallest OpenStreetMap way id, the name, the operator, the line,
   the length in km and the two ends furthest apart.

## Collective database or derivative database

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

## Completeness

OpenStreetMap is **incomplete, community-mapped**: not every cable is mapped, many cables have no name, and land cables are
rare. The app says so wherever the cables are shown and never claims to show all cables. Which cable a measured route used
is always an **estimate**, never a measurement.
