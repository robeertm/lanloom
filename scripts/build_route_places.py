#!/usr/bin/env python3
"""Build the router-place table lanloom/data/route-places.json (stdlib only).

Usage:  python3 -I scripts/build_route_places.py <airports.csv> [--exclude-places FILE] [--out PATH]
        --exclude-places FILE   leaves out every city whose name or any key contains one of the words in FILE
                                (one per line, '#' comments, whole words, any case); the output stays deterministic.
        --out PATH              output file (default: lanloom/data/route-places.json, resolved relative to the repository root)
Source: OurAirports, public domain (https://ourairports.com/data/). The CSV is not downloaded by this script and is
untrusted data: it is only parsed with csv, never executed.

The table maps tokens found in router host names to the place of a city (used at run time to guess where a traceroute hop is).
Per city of CITIES: name, country, main airport (IATA, gives the coordinates) and extra codes. Keys:
  * all IATA/router codes (3 letters, lower case: fra, nyc, lax, sjc, ...),
  * LOCODE-like 5-letter keys (country code + code: defra, nlams, gblon, usnyc; "uk" + code for GB),
  * the English city name in lower case without spaces (frankfurt, newyork, saopaulo).
On a duplicate key the first entry of the list wins (the list is sorted by importance).
Checks run before anything is written; on failure nothing is written and the exit code is 1. The file is written atomically.
"""
import argparse
import csv
import json
import math
import os
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "lanloom" / "data" / "route-places.json"

# (name, country, main IATA, extra codes). Cities without an airport entry in the CSV never get a place (the build reports them).
CITIES = [
    # Europe
    ("Frankfurt", "DE", "FRA", ""), ("Amsterdam", "NL", "AMS", ""), ("London", "GB", "LHR", "lon lgw stn ltn lcy"),
    ("Paris", "FR", "CDG", "par ory"), ("Berlin", "DE", "BER", "txl sxf"), ("Munich", "DE", "MUC", "muenchen"),
    ("Hamburg", "DE", "HAM", ""), ("Duesseldorf", "DE", "DUS", "dusseldorf"), ("Cologne", "DE", "CGN", "koeln koln"),
    ("Stuttgart", "DE", "STR", ""), ("Nuremberg", "DE", "NUE", "nuernberg"), ("Leipzig", "DE", "LEJ", ""),
    ("Dresden", "DE", "DRS", ""), ("Hannover", "DE", "HAJ", "hanover"), ("Bremen", "DE", "BRE", ""),
    ("Dortmund", "DE", "DTM", ""), ("Erfurt", "DE", "ERF", ""), ("Saarbruecken", "DE", "SCN", ""),
    ("Vienna", "AT", "VIE", "wien"), ("Zurich", "CH", "ZRH", "zuerich"), ("Geneva", "CH", "GVA", "geneve"),
    ("Basel", "CH", "BSL", ""), ("Milan", "IT", "MXP", "mil lin bgy milano"), ("Rome", "IT", "FCO", "rom cia roma"),
    ("Turin", "IT", "TRN", "torino"), ("Bologna", "IT", "BLQ", ""), ("Venice", "IT", "VCE", "venezia"),
    ("Palermo", "IT", "PMO", ""), ("Madrid", "ES", "MAD", ""), ("Barcelona", "ES", "BCN", ""), ("Valencia", "ES", "VLC", ""),
    ("Lisbon", "PT", "LIS", "lisboa"), ("Porto", "PT", "OPO", ""), ("Dublin", "IE", "DUB", ""),
    ("Manchester", "GB", "MAN", ""), ("Edinburgh", "GB", "EDI", ""), ("Glasgow", "GB", "GLA", ""),
    ("Birmingham", "GB", "BHX", ""), ("Brussels", "BE", "BRU", "bruxelles"), ("Antwerp", "BE", "ANR", ""),
    ("Rotterdam", "NL", "RTM", ""), ("Eindhoven", "NL", "EIN", ""), ("Groningen", "NL", "GRQ", ""),
    ("Luxembourg", "LU", "LUX", ""), ("Copenhagen", "DK", "CPH", "kobenhavn"), ("Stockholm", "SE", "ARN", "sto bma"),
    ("Gothenburg", "SE", "GOT", ""), ("Malmo", "SE", "MMX", ""), ("Oslo", "NO", "OSL", ""), ("Helsinki", "FI", "HEL", ""),
    ("Reykjavik", "IS", "KEF", ""), ("Warsaw", "PL", "WAW", "warszawa"), ("Krakow", "PL", "KRK", ""),
    ("Gdansk", "PL", "GDN", ""), ("Wroclaw", "PL", "WRO", ""), ("Poznan", "PL", "POZ", ""), ("Prague", "CZ", "PRG", "praha"),
    ("Bratislava", "SK", "BTS", ""), ("Budapest", "HU", "BUD", ""), ("Bucharest", "RO", "OTP", "buc"),
    ("Sofia", "BG", "SOF", ""), ("Athens", "GR", "ATH", ""), ("Belgrade", "RS", "BEG", ""), ("Zagreb", "HR", "ZAG", ""),
    ("Ljubljana", "SI", "LJU", ""), ("Kyiv", "UA", "KBP", "iev kiev"), ("Moscow", "RU", "SVO", "mow dme vko moskva"),
    ("Saint Petersburg", "RU", "LED", "spb"), ("Riga", "LV", "RIX", ""), ("Vilnius", "LT", "VNO", ""),
    ("Tallinn", "EE", "TLL", ""), ("Istanbul", "TR", "IST", "saw"), ("Marseille", "FR", "MRS", ""), ("Lyon", "FR", "LYS", ""),
    ("Nice", "FR", "NCE", ""), ("Bordeaux", "FR", "BOD", ""), ("Strasbourg", "FR", "SXB", ""), ("Lille", "FR", "LIL", ""),
    ("Toulouse", "FR", "TLS", ""), ("Chisinau", "MD", "RMO", "kiv"), ("Sarajevo", "BA", "SJJ", ""), ("Skopje", "MK", "SKP", ""),
    ("Tirana", "AL", "TIA", ""), ("Nicosia", "CY", "LCA", "larnaca"), ("Valletta", "MT", "MLA", "malta"),
    # North America
    ("New York", "US", "JFK", "nyc ewr lga nwk newark"), ("Ashburn", "US", "IAD", "was dca washington reston"),
    ("Los Angeles", "US", "LAX", ""), ("San Francisco", "US", "SFO", ""), ("San Jose", "US", "SJC", ""),
    ("Palo Alto", "US", "SJC", "pao"), ("Chicago", "US", "ORD", "chi mdw"), ("Dallas", "US", "DFW", "dal"),
    ("Houston", "US", "IAH", "hou"), ("Atlanta", "US", "ATL", ""), ("Miami", "US", "MIA", ""), ("Seattle", "US", "SEA", ""),
    ("Denver", "US", "DEN", ""), ("Phoenix", "US", "PHX", ""), ("Las Vegas", "US", "LAS", ""),
    ("Salt Lake City", "US", "SLC", ""), ("Minneapolis", "US", "MSP", ""), ("Kansas City", "US", "MCI", ""),
    ("St. Louis", "US", "STL", "saintlouis"), ("Boston", "US", "BOS", ""), ("Philadelphia", "US", "PHL", ""),
    ("Detroit", "US", "DTW", ""), ("Cleveland", "US", "CLE", ""), ("Columbus", "US", "CMH", ""), ("Pittsburgh", "US", "PIT", ""),
    ("Charlotte", "US", "CLT", ""), ("Tampa", "US", "TPA", ""), ("Orlando", "US", "MCO", ""), ("Portland", "US", "PDX", ""),
    ("San Diego", "US", "SAN", ""), ("Sacramento", "US", "SMF", ""), ("Austin", "US", "AUS", ""),
    ("San Antonio", "US", "SAT", ""), ("Nashville", "US", "BNA", ""), ("Indianapolis", "US", "IND", ""),
    ("Cincinnati", "US", "CVG", ""), ("Raleigh", "US", "RDU", ""), ("Honolulu", "US", "HNL", ""), ("Anchorage", "US", "ANC", ""),
    ("Albuquerque", "US", "ABQ", ""), ("Omaha", "US", "OMA", ""), ("New Orleans", "US", "MSY", ""),
    ("Jacksonville", "US", "JAX", ""), ("Baltimore", "US", "BWI", ""), ("Toronto", "CA", "YYZ", "tor"),
    ("Montreal", "CA", "YUL", "mtl"), ("Vancouver", "CA", "YVR", "van"), ("Calgary", "CA", "YYC", ""), ("Ottawa", "CA", "YOW", ""),
    ("Winnipeg", "CA", "YWG", ""), ("Edmonton", "CA", "YEG", ""), ("Mexico City", "MX", "MEX", ""), ("Queretaro", "MX", "QRO", ""),
    ("Guadalajara", "MX", "GDL", ""), ("Monterrey", "MX", "MTY", ""), ("Panama City", "PA", "PTY", ""),
    ("San Juan", "PR", "SJU", ""), ("San Jose Costa Rica", "CR", "SJO", ""),
    ("Santo Domingo", "DO", "SDQ", ""), ("Guatemala City", "GT", "GUA", ""),
    # South America
    ("Sao Paulo", "BR", "GRU", "sao cgh vcp"), ("Rio de Janeiro", "BR", "GIG", "rio"), ("Brasilia", "BR", "BSB", ""),
    ("Fortaleza", "BR", "FOR", ""), ("Porto Alegre", "BR", "POA", ""), ("Curitiba", "BR", "CWB", ""),
    ("Belo Horizonte", "BR", "CNF", "bhz"), ("Buenos Aires", "AR", "EZE", "bue aep"), ("Santiago", "CL", "SCL", ""),
    ("Lima", "PE", "LIM", ""), ("Bogota", "CO", "BOG", ""), ("Quito", "EC", "UIO", ""), ("Caracas", "VE", "CCS", ""),
    ("Montevideo", "UY", "MVD", ""), ("Asuncion", "PY", "ASU", ""), ("La Paz", "BO", "LPB", ""),
    # Asia
    ("Tokyo", "JP", "HND", "tyo nrt"), ("Osaka", "JP", "KIX", "osa itm"), ("Nagoya", "JP", "NGO", ""), ("Fukuoka", "JP", "FUK", ""),
    ("Sapporo", "JP", "CTS", ""), ("Seoul", "KR", "ICN", "sel gmp"), ("Busan", "KR", "PUS", ""), ("Hong Kong", "HK", "HKG", ""),
    ("Singapore", "SG", "SIN", ""), ("Taipei", "TW", "TPE", "tsa"), ("Shanghai", "CN", "PVG", "sha hqa"),
    ("Beijing", "CN", "PEK", "bjs pkx"), ("Guangzhou", "CN", "CAN", ""), ("Shenzhen", "CN", "SZX", ""),
    ("Chengdu", "CN", "CTU", ""), ("Wuhan", "CN", "WUH", ""), ("Macau", "MO", "MFM", ""), ("Mumbai", "IN", "BOM", "bombay"),
    ("Delhi", "IN", "DEL", "newdelhi"), ("Chennai", "IN", "MAA", "madras"), ("Bangalore", "IN", "BLR", "bengaluru"),
    ("Hyderabad", "IN", "HYD", ""), ("Kolkata", "IN", "CCU", "calcutta"), ("Pune", "IN", "PNQ", ""),
    ("Bangkok", "TH", "BKK", "dmk"), ("Jakarta", "ID", "CGK", "jkt"), ("Kuala Lumpur", "MY", "KUL", ""),
    ("Manila", "PH", "MNL", ""), ("Ho Chi Minh City", "VN", "SGN", "saigon hcm"), ("Hanoi", "VN", "HAN", ""),
    ("Phnom Penh", "KH", "PNH", ""), ("Yangon", "MM", "RGN", ""), ("Dhaka", "BD", "DAC", ""), ("Karachi", "PK", "KHI", ""),
    ("Lahore", "PK", "LHE", ""), ("Islamabad", "PK", "ISB", ""), ("Colombo", "LK", "CMB", ""), ("Kathmandu", "NP", "KTM", ""),
    ("Dubai", "AE", "DXB", ""), ("Abu Dhabi", "AE", "AUH", ""), ("Doha", "QA", "DOH", ""), ("Riyadh", "SA", "RUH", ""),
    ("Jeddah", "SA", "JED", ""), ("Kuwait City", "KW", "KWI", ""), ("Manama", "BH", "BAH", ""), ("Muscat", "OM", "MCT", ""),
    ("Tel Aviv", "IL", "TLV", ""), ("Amman", "JO", "AMM", ""), ("Beirut", "LB", "BEY", ""), ("Tehran", "IR", "THR", ""),
    ("Tbilisi", "GE", "TBS", ""), ("Yerevan", "AM", "EVN", ""), ("Baku", "AZ", "GYD", ""), ("Almaty", "KZ", "ALA", ""),
    ("Tashkent", "UZ", "TAS", ""), ("Ulaanbaatar", "MN", "ULN", ""), ("Vladivostok", "RU", "VVO", ""),
    ("Novosibirsk", "RU", "OVB", ""), ("Yekaterinburg", "RU", "SVX", ""),
    # Oceania
    ("Sydney", "AU", "SYD", ""), ("Melbourne", "AU", "MEL", ""), ("Brisbane", "AU", "BNE", ""), ("Perth", "AU", "PER", ""),
    ("Adelaide", "AU", "ADL", ""), ("Canberra", "AU", "CBR", ""), ("Auckland", "NZ", "AKL", ""), ("Wellington", "NZ", "WLG", ""),
    ("Christchurch", "NZ", "CHC", ""), ("Suva", "FJ", "SUV", ""), ("Noumea", "NC", "NOU", ""),
    # Africa
    ("Johannesburg", "ZA", "JNB", ""), ("Cape Town", "ZA", "CPT", ""), ("Durban", "ZA", "DUR", ""), ("Lagos", "NG", "LOS", ""),
    ("Abuja", "NG", "ABV", ""), ("Nairobi", "KE", "NBO", ""), ("Mombasa", "KE", "MBA", ""), ("Cairo", "EG", "CAI", ""),
    ("Casablanca", "MA", "CMN", ""), ("Tunis", "TN", "TUN", ""), ("Algiers", "DZ", "ALG", ""), ("Accra", "GH", "ACC", ""),
    ("Dakar", "SN", "DSS", ""), ("Addis Ababa", "ET", "ADD", ""), ("Dar es Salaam", "TZ", "DAR", ""),
    ("Kampala", "UG", "EBB", ""), ("Kigali", "RW", "KGL", ""), ("Luanda", "AO", "LAD", ""), ("Maputo", "MZ", "MPM", ""),
    ("Lusaka", "ZM", "LUN", ""), ("Harare", "ZW", "HRE", ""), ("Windhoek", "NA", "WDH", ""), ("Port Louis", "MU", "MRU", ""),
    ("Djibouti", "DJ", "JIB", ""), ("Abidjan", "CI", "ABJ", ""), ("Kinshasa", "CD", "FIH", ""),
]

MIN_CITIES = 250
MIN_KEYS = 600
# well-known samples: key -> (lat, lon, max distance in degrees)
SAMPLES = {
    "fra": (50.03, 8.56, 0.5), "lhr": (51.47, -0.45, 0.5), "lon": (51.47, -0.45, 0.5), "jfk": (40.64, -73.78, 0.5),
    "nyc": (40.64, -73.78, 0.5), "sin": (1.36, 103.99, 0.5), "syd": (-33.94, 151.18, 0.5), "gru": (-23.43, -46.47, 0.5),
}
KEY_RE = re.compile(r"^[a-z]{3,20}$")
CC_RE = re.compile(r"^[A-Z]{2}$")


def _slug(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return "".join(c for c in text if c.isalnum())


def _excluder(path):
    """A predicate for the words listed in the file at ``path`` (whole words, any case); None means nothing is excluded."""
    if not path:
        return lambda text: False
    words = [w.strip() for w in Path(path).read_text(encoding="utf-8").splitlines() if w.strip() and not w.lstrip().startswith("#")]
    rx = re.compile(r"(?<!\w)(?:%s)(?!\w)" % "|".join(re.escape(w) for w in words), re.I) if words else None
    return lambda text: bool(rx and rx.search(text))


def read(path):
    """IATA -> (lat, lon): only airports with an IATA code, larger type first, then scheduled service. Pure parsing."""
    rank = {"large_airport": 0, "medium_airport": 1, "small_airport": 2}
    best = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            iata = (row.get("iata_code") or "").strip().upper()
            if len(iata) != 3 or row.get("type") not in rank:
                continue
            try:
                lat, lon = float(row["latitude_deg"]), float(row["longitude_deg"])
            except (KeyError, ValueError):
                continue
            r = (rank[row["type"]], 0 if row.get("scheduled_service") == "yes" else 1)
            if iata not in best or r < best[iata][0]:
                best[iata] = (r, round(lat, 4), round(lon, 4))
    return {k: (v[1], v[2]) for k, v in best.items()}


def build(airports, exclude=None):
    """-> (places, missing). ``exclude``: predicate on a text; a city is left out if its name or any key matches."""
    skip = exclude or (lambda text: False)
    places, missing = {}, []
    for name, cc, iata, extra in CITIES:
        coord = airports.get(iata)
        if not coord:
            missing.append("%s (%s)" % (name, iata))
            continue
        extras = [e for e in extra.split() if e]
        codes = [iata.lower()] + extras
        three = [c for c in codes if len(c) == 3]
        keys = list(codes) + [_slug(name)] + [e for e in extras if len(e) > 3]
        keys += [cc.lower() + c for c in three[:2]]
        if cc == "GB":
            keys += ["uk" + c for c in three[:2]]
        if skip(name) or any(skip(k) for k in keys):
            continue
        entry = {"name": name, "cc": cc, "lat": coord[0], "lon": coord[1]}
        for k in keys:
            places.setdefault(k, dict(entry))
    return places, missing


def _dist(lat1, lon1, lat2, lon2):
    return max(abs(lat1 - lat2), abs(((lon1 - lon2) + 180) % 360 - 180))


def check(places, min_cities=MIN_CITIES, min_keys=MIN_KEYS, samples=SAMPLES):
    """Raise ValueError with a clear message if the table is not plausible."""
    if len(places) < min_keys:
        raise ValueError("too few keys: %d < %d" % (len(places), min_keys))
    cities = {(p["name"], p["cc"]) for p in places.values()}
    if len(cities) < min_cities:
        raise ValueError("too few cities: %d < %d" % (len(cities), min_cities))
    for key, p in places.items():
        if not KEY_RE.match(key):
            raise ValueError("bad key: %r" % key)
        if not CC_RE.match(str(p.get("cc"))):
            raise ValueError("bad country code for %s: %r" % (key, p.get("cc")))
        lat, lon = p.get("lat"), p.get("lon")
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (lat, lon)):
            raise ValueError("bad coordinate for %s" % key)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("coordinate out of range for %s: %s/%s" % (key, lat, lon))
    for key, (lat, lon, tol) in (samples or {}).items():
        p = places.get(key)
        if p is None:
            raise ValueError("sample key missing: %s" % key)
        if _dist(p["lat"], p["lon"], lat, lon) > tol:
            raise ValueError("sample %s is off: %s/%s, expected near %s/%s" % (key, p["lat"], p["lon"], lat, lon))


def write(places, out):
    """Atomic, deterministic write (tmp + os.replace)."""
    head = {"format": "route-places/1",
            "source": "OurAirports (https://ourairports.com/data/), coordinates of the main airport of each city; codes assigned by hand",
            "license": "Public domain",
            "note": "key = code or city name in lower case without spaces -> place of the city, not of a single router"}
    lines = ",\n".join(json.dumps(k) + ": " + json.dumps(v, ensure_ascii=False, separators=(", ", ": ")) for k, v in sorted(places.items()))
    text = '{"head": ' + json.dumps(head, ensure_ascii=False) + ',\n"places": {\n' + lines + "\n}}\n"
    out = Path(out)
    tmp = out.with_name(out.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build lanloom/data/route-places.json from an OurAirports airports.csv")
    ap.add_argument("airports")
    ap.add_argument("--exclude-places", default=None)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args(argv)
    try:
        places, missing = build(read(args.airports), _excluder(args.exclude_places))
        if missing:
            raise ValueError("no airport coordinates for: " + ", ".join(missing))
        check(places)
        write(places, args.out)
    except (ValueError, OSError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    print("%d keys, %d cities, %d bytes" % (len(places), len({(p["name"], p["cc"]) for p in places.values()}), os.path.getsize(args.out)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
