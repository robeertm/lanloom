# Third-party notices

Lanloom itself is distributed under the terms in [LICENSE](LICENSE). It includes or runs on the components below, each licensed by its own authors under its own terms. Nothing in the Lanloom license limits the rights those licenses grant you. Full license texts are available from each project and are included in the installed packages. The Python package table lists exactly the packages installed in the image (`requirements.txt`, checked by a test).

## Runtime and base image

| Component | License |
| --- | --- |
| Python runtime | Python Software Foundation License |
| Container base image: Debian-based slim Python image | Debian packages under their individual licenses (see `/usr/share/doc/*/copyright` in the image) |

## Python packages

| Package | Version | License |
| --- | --- | --- |
| fastapi | 0.165.0 | MIT (Sebastián Ramírez) |
| starlette | 1.10.0 | BSD-3-Clause (Encode OSS Ltd.) |
| pydantic | 2.14.4 | MIT (Pydantic Services Inc. and contributors) |
| pydantic_core | 2.50.5 | MIT (Pydantic Services Inc. and contributors) |
| annotated-types | 0.8.0 | MIT |
| annotated-doc | 0.0.7 | MIT (Sebastián Ramírez) |
| typing_extensions | 4.16.0 | PSF-2.0 (Python Software Foundation) |
| typing-inspection | 0.4.5 | MIT |
| uvicorn | 0.54.0 | BSD-3-Clause (Encode OSS Ltd.) |
| httptools | 0.9.0 | MIT (MagicStack Inc.) |
| uvloop | 0.23.0 | MIT OR Apache-2.0 (MagicStack Inc.) |
| watchfiles | 1.3.0 | MIT (Samuel Colvin) |
| websockets | 17.4 | BSD-3-Clause (Aymeric Augustin and contributors) |
| python-dotenv | 1.2.4 | BSD-3-Clause |
| PyYAML | 6.0.4 | MIT (Kirill Simonov) |
| click | 8.5.1 | BSD-3-Clause (Pallets) |
| h11 | 0.16.0 | MIT (Nathaniel J. Smith) |
| anyio | 4.16.0 | MIT (Alex Grönholm) |
| idna | 3.20 | BSD-3-Clause (Kim Davies and contributors) |
| opentelemetry-api | 1.45.2 | Apache-2.0 |
| argon2-cffi | 25.1.0 | MIT (Hynek Schlawack) |
| argon2-cffi-bindings | 26.2.0 | MIT (Hynek Schlawack) |
| cffi | 2.1.1 | MIT (Armin Rigo, Maciej Fijalkowski) |
| pycparser | 3.12 | BSD-3-Clause (Eli Bendersky) |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause (the cryptography developers) |
| webauthn | 3.0.1 | BSD-3-Clause (Duo Labs and contributors) |
| cbor2 | 6.3.0 | MIT (Alex Grönholm) |
| pyOpenSSL | 26.4.0 | Apache-2.0 (the pyOpenSSL developers) |
| pyasn1 | 0.6.5 | BSD-2-Clause (Ilya Etingof) |
| pyasn1_modules | 0.4.2 | BSD-2-Clause (Ilya Etingof) |
| pip | 26.3 | MIT (the pip developers) |

Where a package offers a choice of licenses ("OR"), the choice is yours. Copyright holders are named where well known; for others see the respective project.

## Fonts

| Component | License |
| --- | --- |
| Inter typeface (`lanloom/web/fonts/InterVariable.woff2`), Copyright (c) 2016 The Inter Project Authors | SIL Open Font License, Version 1.1. The license text is in `lanloom/web/fonts/LICENSE.txt`. |

## Data

| Component | License |
| --- | --- |
| IP to Country Lite database by DB-IP (`lanloom/data/geo-country.bin.gz`, converted to a compact format by `scripts/build_geodb.py`). Attribution: [IP Geolocation by DB-IP](https://db-ip.com) | Creative Commons Attribution 4.0 International ([CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)). The attribution is also shown in the app (Data paths view). |
| Hardware address register (`lanloom/data/oui.bin.gz`, converted to a compact format by `scripts/build_oui.py`) from the public MA-L, MA-M and MA-S listings of the [IEEE Registration Authority](https://standards-oui.ieee.org/). Organisation names are shortened (legal-form suffixes removed). | Public listing of the IEEE Registration Authority, redistributed as factual data |
| Vulnerability feed (`lanloom/data/vulnfeed.json.gz`, converted to a compact format by `scripts/build_vulnfeed.py`), part 1: the [Known Exploited Vulnerabilities Catalog](https://www.cisa.gov/known-exploited-vulnerabilities-catalog) of the U.S. Cybersecurity and Infrastructure Security Agency (vulnerability identifiers, names, dates added, known ransomware use). Use does not imply endorsement by the agency. | Creative Commons Zero 1.0 Universal ([CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/)) |
| Vulnerability feed, part 2: affected-version ranges (CPE match criteria), CVSS severities and descriptions of the [National Vulnerability Database](https://nvd.nist.gov/) of the U.S. National Institute of Standards and Technology. This product uses data from the NVD API but is not endorsed or certified by the NVD. | Public domain in the United States (work of the U.S. government), used under the NVD terms of use |
| World base map (`lanloom/web/js/worldmap.js`), simplified from Natural Earth admin-0 countries, 1:110m shapes and 1:50m label points, built with `scripts/build_worldmap.py` | Public domain (Natural Earth) |
| Fine base map (`lanloom/web/js/worldmap_fine.js`, Natural Earth admin-0 countries 1:50m) and places for orientation (`lanloom/web/js/worldplaces.js`, Natural Earth populated places 1:10m), built with `scripts/build_worldmap.py` | Public domain (Natural Earth) |
| Optional city and network operator data: *IP to City Lite* and *IP to ASN Lite* databases by DB-IP, fetched at run time only when an administrator switches on *Cities and network operators* and condensed into a local file (not part of the image). Attribution: [IP Geolocation by DB-IP](https://db-ip.com) | Creative Commons Attribution 4.0 International ([CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)) |
| Network routes, places in router names (`lanloom/data/route-places.json`, built with `scripts/build_route_places.py` from the airport list of [OurAirports](https://ourairports.com/data/) at build time; coordinates of the main airport of each city, no run-time download) | Public domain (OurAirports) |
| Network routes, optional exchange point lists: [PeeringDB](https://www.peeringdb.com), fetched at run time only when an administrator switches on *Exchange points from PeeringDB*; not part of the image or the repository. Kept only for the instance's own diagnosis, never passed on, exported or bundled, in line with the acceptable use policy: "The PeeringDB data may not be passed on in bulk to any other person or organization unless approved by PeeringDB." | PeeringDB terms and acceptable use policy |
| Real cables of the data paths map (`lanloom/data/cables-osm.json.gz`, sea and land telecommunication cables, built with `scripts/build_cables.py` from [OpenStreetMap](https://www.openstreetmap.org/) data at build time; filtered, bundled by name, simplified and projected, see `lanloom/data/CABLES-LICENSE.md`). Attribution: © OpenStreetMap contributors. The file is a derivative database and stays under the ODbL; it is kept separate from the code, the ODbL does not cover the rest of the app. The app shows the attribution beside the map whenever cables are drawn. No run-time download. | [Open Database License 1.0 (ODbL)](https://opendatacommons.org/licenses/odbl/1-0/) |
