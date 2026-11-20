"""Infrastructure routes (data paths, view "Real cables"): every data path runs over infrastructure, never over an arc.

Pure functions and classes, standard library only, no network, no files (except ``load_world``, which reads the bundled world map).
Inputs are plain data: the cable data set (``cables.py``, community-mapped, ODbL), the countries of the world map, the home country
and the stored routes (``routes.condense``: kind, cc, place {name, cc, lat, lon, sure}, rtt - never an address). The same logic as the
template it was adapted from, with English names; the output carries no sentences (the view words it in every language).

A graph is built and the most plausible way is searched for every destination:

  Nodes   * hubs of large cities (HUBS: own list, only city, country and coordinates - places with large exchange points or data
            centres, on every continent and in almost every country),
          * the nodes of the submarine cables (ends of the pieces, branches; ``cableroute.CableGraph``),
          * the start (the home) and the country points (the same points as on the world map).
  Edges   * "land"    land routes along known long-distance corridors (CORRIDORS; great circle, via points on coasts),
          * "cable"   submarine cables, course from the cable data,
          * "sea"     known cable corridors that the data does not (completely) hold (SEA_ROUTES; course estimated),
          * "landing" landing point -> nearest hub (land route, course estimated),
          * "access"  hub -> country point and home -> hub (course estimated),
          * "radio"   island without a cable close to a hub (at most RADIO_MAX_KM): microwave link,
          * "sat"     island or destination without a reachable line, satellite networks (AS number) and the latency jump of a
                      geostationary satellite: earth station (nearest hub) -> satellite -> destination.
  Costs   km x factor (cable 1.0 - unnamed 1.15 + 50 km -, land 1.1 x corridor factor, landing 1.5, access 1.2, radio 2.0). Long
          corridors through Russia and Central Asia cost more (most traffic to Asia runs over the sea today).

One way per country: a Dijkstra search from the home over the whole graph (one tree - equal corridors share their edges, the view
bundles them into one strand). Measured routes: the places of a route are put on hubs (at most 250 km, outliers without a sure place
drop out) and joined in order over the graph; estimated cables of a sea crossing (``cableroute``) are preferred. If the latency
excludes the destination country (anycast, database country differs from the location), the way ends where it was measured.

Origin of a way: "route" (backed by measured places), "cable" (submarine cable likely), "land" (land route estimated), "radio", "sat".
"""
import hashlib
import heapq
import json
import math
import re

from . import cableroute as CR

VERSION = "1.0.0"
SOURCES_ALWAYS = ["hubs", "corridors"]

# ---------------------------------------------------------------------------------------------------------- hubs
# Hubs: cities only. Key -> (name, country, latitude, longitude); the coordinates are those of the city centre.
HUBS = {
    # Central Europe
    "fra": ("Frankfurt", "DE", 50.11, 8.68), "ber": ("Berlin", "DE", 52.52, 13.40), "ham": ("Hamburg", "DE", 53.55, 9.99),
    "muc": ("Munich", "DE", 48.14, 11.58), "dus": ("Dusseldorf", "DE", 51.23, 6.78), "ams": ("Amsterdam", "NL", 52.37, 4.90),
    "bru": ("Brussels", "BE", 50.85, 4.35), "lux": ("Luxembourg", "LU", 49.61, 6.13), "par": ("Paris", "FR", 48.86, 2.35),
    "mrs": ("Marseille", "FR", 43.30, 5.37), "lyo": ("Lyon", "FR", 45.76, 4.84), "bod": ("Bordeaux", "FR", 44.84, -0.58),
    "lon": ("London", "GB", 51.51, -0.13), "man": ("Manchester", "GB", 53.48, -2.24), "brs": ("Bristol", "GB", 51.45, -2.59),
    "edi": ("Edinburgh", "GB", 55.95, -3.19), "dub": ("Dublin", "IE", 53.35, -6.26), "ork": ("Cork", "IE", 51.90, -8.47),
    "mad": ("Madrid", "ES", 40.42, -3.70), "bcn": ("Barcelona", "ES", 41.39, 2.17), "bio": ("Bilbao", "ES", 43.26, -2.93),
    "vlc": ("Valencia", "ES", 39.47, -0.38), "lis": ("Lisbon", "PT", 38.72, -9.14), "zrh": ("Zurich", "CH", 47.38, 8.54),
    "gva": ("Geneva", "CH", 46.20, 6.14), "mil": ("Milan", "IT", 45.46, 9.19), "rom": ("Rome", "IT", 41.90, 12.50),
    "pmo": ("Palermo", "IT", 38.12, 13.36), "bri": ("Bari", "IT", 41.12, 16.87), "vie": ("Vienna", "AT", 48.21, 16.37),
    "prg": ("Prague", "CZ", 50.08, 14.44), "waw": ("Warsaw", "PL", 52.23, 21.01), "bts": ("Bratislava", "SK", 48.15, 17.11),
    "bud": ("Budapest", "HU", 47.50, 19.04), "lju": ("Ljubljana", "SI", 46.06, 14.51), "zag": ("Zagreb", "HR", 45.81, 15.98),
    "beg": ("Belgrade", "RS", 44.79, 20.45), "sof": ("Sofia", "BG", 42.70, 23.32), "buh": ("Bucharest", "RO", 44.43, 26.10),
    "ath": ("Athens", "GR", 37.98, 23.73), "skg": ("Thessaloniki", "GR", 40.64, 22.94), "ist": ("Istanbul", "TR", 41.01, 28.98),
    "ank": ("Ankara", "TR", 39.93, 32.86), "cph": ("Copenhagen", "DK", 55.68, 12.57), "aar": ("Aarhus", "DK", 56.16, 10.20),
    "sto": ("Stockholm", "SE", 59.33, 18.07), "got": ("Gothenburg", "SE", 57.71, 11.97), "osl": ("Oslo", "NO", 59.91, 10.75),
    "hel": ("Helsinki", "FI", 60.17, 24.94), "tll": ("Tallinn", "EE", 59.44, 24.75), "rix": ("Riga", "LV", 56.95, 24.11),
    "vno": ("Vilnius", "LT", 54.69, 25.28), "kbp": ("Kyiv", "UA", 50.45, 30.52), "ods": ("Odesa", "UA", 46.48, 30.73),
    "kiv": ("Chișinău", "MD", 47.01, 28.86), "msq": ("Minsk", "BY", 53.90, 27.56), "kef": ("Reykjavík", "IS", 64.15, -21.94),
    "mla": ("Valletta", "MT", 35.90, 14.51), "nic": ("Nicosia", "CY", 35.17, 33.36), "tia": ("Tirana", "AL", 41.33, 19.82),
    "skp": ("Skopje", "MK", 42.00, 21.43), "sjj": ("Sarajevo", "BA", 43.86, 18.41), "tgd": ("Podgorica", "ME", 42.44, 19.26),
    "tor": ("Tórshavn", "FO", 62.01, -6.77), "goh": ("Nuuk", "GL", 64.18, -51.72),
    # Russia, Caucasus, Central Asia
    "mow": ("Moscow", "RU", 55.76, 37.62), "led": ("Saint Petersburg", "RU", 59.94, 30.31), "ekb": ("Yekaterinburg", "RU", 56.84, 60.61),
    "ovb": ("Novosibirsk", "RU", 55.03, 82.92), "irk": ("Irkutsk", "RU", 52.29, 104.28), "khv": ("Khabarovsk", "RU", 48.48, 135.07),
    "vvo": ("Vladivostok", "RU", 43.12, 131.89), "rov": ("Rostov-on-Don", "RU", 47.24, 39.71), "nqz": ("Astana", "KZ", 51.17, 71.43),
    "ala": ("Almaty", "KZ", 43.24, 76.89), "tas": ("Tashkent", "UZ", 41.30, 69.24), "fru": ("Bishkek", "KG", 42.87, 74.59),
    "dyu": ("Dushanbe", "TJ", 38.56, 68.79), "asb": ("Ashgabat", "TM", 37.95, 58.38), "tbs": ("Tbilisi", "GE", 41.72, 44.79),
    "evn": ("Yerevan", "AM", 40.18, 44.51), "gyd": ("Baku", "AZ", 40.41, 49.87), "uln": ("Ulaanbaatar", "MN", 47.92, 106.92),
    # Middle East
    "tlv": ("Tel Aviv", "IL", 32.08, 34.78), "amm": ("Amman", "JO", 31.95, 35.93), "bey": ("Beirut", "LB", 33.89, 35.50),
    "dam": ("Damascus", "SY", 33.51, 36.29), "bgw": ("Baghdad", "IQ", 33.31, 44.37), "thr": ("Tehran", "IR", 35.69, 51.39),
    "ruh": ("Riyadh", "SA", 24.71, 46.68), "jed": ("Jeddah", "SA", 21.49, 39.19), "dxb": ("Dubai", "AE", 25.20, 55.27),
    "fjr": ("Fujairah", "AE", 25.13, 56.33), "doh": ("Doha", "QA", 25.29, 51.53), "bah": ("Manama", "BH", 26.23, 50.59),
    "kwi": ("Kuwait City", "KW", 29.38, 47.99), "mct": ("Muscat", "OM", 23.59, 58.41), "sah": ("Sanaa", "YE", 15.37, 44.19),
    "ade": ("Aden", "YE", 12.79, 45.03),
    # Africa
    "cai": ("Cairo", "EG", 30.04, 31.24), "alx": ("Alexandria", "EG", 31.20, 29.92), "suz": ("Suez", "EG", 29.97, 32.55),
    "tun": ("Tunis", "TN", 36.81, 10.18), "alg": ("Algiers", "DZ", 36.75, 3.06), "cmn": ("Casablanca", "MA", 33.57, -7.59),
    "tip": ("Tripoli", "LY", 32.89, 13.19), "kht": ("Khartoum", "SD", 15.50, 32.56), "pzu": ("Port Sudan", "SD", 19.62, 37.22),
    "jib": ("Djibouti", "DJ", 11.59, 43.15), "add": ("Addis Ababa", "ET", 9.03, 38.74), "asm": ("Asmara", "ER", 15.32, 38.93),
    "nbo": ("Nairobi", "KE", -1.29, 36.82), "mba": ("Mombasa", "KE", -4.04, 39.67), "dar": ("Dar es Salaam", "TZ", -6.79, 39.21),
    "kla": ("Kampala", "UG", 0.35, 32.58), "kgl": ("Kigali", "RW", -1.95, 30.06), "bjm": ("Bujumbura", "BI", -3.38, 29.36),
    "mgq": ("Mogadishu", "SO", 2.05, 45.32), "jub": ("Juba", "SS", 4.85, 31.58), "dkr": ("Dakar", "SN", 14.72, -17.47),
    "abj": ("Abidjan", "CI", 5.36, -4.01), "acc": ("Accra", "GH", 5.60, -0.19), "los": ("Lagos", "NG", 6.52, 3.38),
    "abv": ("Abuja", "NG", 9.08, 7.40), "dla": ("Douala", "CM", 4.05, 9.70), "lbv": ("Libreville", "GA", 0.42, 9.47),
    "fih": ("Kinshasa", "CD", -4.44, 15.27), "pnr": ("Pointe-Noire", "CG", -4.78, 11.86), "lad": ("Luanda", "AO", -8.84, 13.23),
    "jnb": ("Johannesburg", "ZA", -26.20, 28.05), "cpt": ("Cape Town", "ZA", -33.92, 18.42), "dur": ("Durban", "ZA", -29.86, 31.03),
    "mpm": ("Maputo", "MZ", -25.97, 32.57), "tnr": ("Antananarivo", "MG", -18.88, 47.51), "mru": ("Port Louis", "MU", -20.16, 57.50),
    "sez": ("Victoria", "SC", -4.62, 55.45), "run": ("Saint-Denis", "RE", -20.88, 55.45), "hre": ("Harare", "ZW", -17.83, 31.05),
    "lun": ("Lusaka", "ZM", -15.39, 28.32), "llw": ("Lilongwe", "MW", -13.96, 33.79), "wdh": ("Windhoek", "NA", -22.56, 17.08),
    "gbe": ("Gaborone", "BW", -24.65, 25.91), "bko": ("Bamako", "ML", 12.64, -8.00), "nkc": ("Nouakchott", "MR", 18.08, -15.98),
    "oua": ("Ouagadougou", "BF", 12.37, -1.52), "nim": ("Niamey", "NE", 13.51, 2.11), "ndj": ("N’Djamena", "TD", 12.13, 15.06),
    "coo": ("Cotonou", "BJ", 6.37, 2.39), "lfw": ("Lomé", "TG", 6.13, 1.22), "fna": ("Freetown", "SL", 8.48, -13.23),
    "rob": ("Monrovia", "LR", 6.30, -10.80), "cky": ("Conakry", "GN", 9.64, -13.58), "bjl": ("Banjul", "GM", 13.45, -16.58),
    "oxb": ("Bissau", "GW", 11.86, -15.60), "ssg": ("Malabo", "GQ", 3.75, 8.78), "tms": ("São Tomé", "ST", 0.34, 6.73),
    "rai": ("Praia", "CV", 14.93, -23.51), "bgf": ("Bangui", "CF", 4.39, 18.56), "lpa": ("Las Palmas", "ES", 28.12, -15.43),
    # South Asia
    "bom": ("Mumbai", "IN", 19.08, 72.88), "del": ("Delhi", "IN", 28.61, 77.21), "maa": ("Chennai", "IN", 13.08, 80.27),
    "blr": ("Bengaluru", "IN", 12.97, 77.59), "ccu": ("Kolkata", "IN", 22.57, 88.36), "khi": ("Karachi", "PK", 24.86, 67.01),
    "lhe": ("Lahore", "PK", 31.55, 74.34), "isb": ("Islamabad", "PK", 33.68, 73.05), "dac": ("Dhaka", "BD", 23.81, 90.41),
    "cmb": ("Colombo", "LK", 6.93, 79.86), "ktm": ("Kathmandu", "NP", 27.72, 85.32), "pbh": ("Thimphu", "BT", 27.47, 89.64),
    "mle": ("Malé", "MV", 4.18, 73.51), "kbl": ("Kabul", "AF", 34.56, 69.21),
    # East and Southeast Asia
    "sin": ("Singapore", "SG", 1.29, 103.85), "kul": ("Kuala Lumpur", "MY", 3.14, 101.69), "bkk": ("Bangkok", "TH", 13.76, 100.50),
    "sgn": ("Ho Chi Minh City", "VN", 10.82, 106.63), "han": ("Hanoi", "VN", 21.03, 105.85), "pnh": ("Phnom Penh", "KH", 11.56, 104.92),
    "vte": ("Vientiane", "LA", 17.98, 102.63), "rgn": ("Yangon", "MM", 16.87, 96.20), "cgk": ("Jakarta", "ID", -6.21, 106.85),
    "sub": ("Surabaya", "ID", -7.25, 112.75), "mnl": ("Manila", "PH", 14.60, 120.98), "hkg": ("Hong Kong", "HK", 22.32, 114.17),
    "mfm": ("Macau", "MO", 22.20, 113.54), "can": ("Guangzhou", "CN", 23.13, 113.26), "sha": ("Shanghai", "CN", 31.23, 121.47),
    "pek": ("Beijing", "CN", 39.90, 116.40), "ctu": ("Chengdu", "CN", 30.66, 104.07), "kmg": ("Kunming", "CN", 25.04, 102.71),
    "urc": ("Urumqi", "CN", 43.83, 87.62), "lhw": ("Lanzhou", "CN", 36.06, 103.83), "tpe": ("Taipei", "TW", 25.03, 121.57),
    "sel": ("Seoul", "KR", 37.57, 126.98), "pus": ("Busan", "KR", 35.18, 129.08), "tyo": ("Tokyo", "JP", 35.68, 139.69),
    "osa": ("Osaka", "JP", 34.69, 135.50), "fuk": ("Fukuoka", "JP", 33.59, 130.40), "fyj": ("Pyongyang", "KP", 39.02, 125.75),
    "bwn": ("Bandar Seri Begawan", "BN", 4.90, 114.94), "dil": ("Dili", "TL", -8.56, 125.57), "pom": ("Port Moresby", "PG", -9.44, 147.18),
    "gum": ("Hagåtña", "GU", 13.44, 144.79),
    # Oceania
    "syd": ("Sydney", "AU", -33.87, 151.21), "mel": ("Melbourne", "AU", -37.81, 144.96), "bne": ("Brisbane", "AU", -27.47, 153.03),
    "per": ("Perth", "AU", -31.95, 115.86), "adl": ("Adelaide", "AU", -34.93, 138.60), "drw": ("Darwin", "AU", -12.46, 130.84),
    "akl": ("Auckland", "NZ", -36.85, 174.76), "wlg": ("Wellington", "NZ", -41.29, 174.78), "suv": ("Suva", "FJ", -18.14, 178.44),
    "nou": ("Nouméa", "NC", -22.27, 166.46), "ppt": ("Papeete", "PF", -17.54, -149.57), "hnl": ("Honolulu", "US", 21.31, -157.86),
    "apw": ("Apia", "WS", -13.83, -171.76), "tbu": ("Nukuʻalofa", "TO", -21.14, -175.20), "vli": ("Port Vila", "VU", -17.73, 168.32),
    "hir": ("Honiara", "SB", -9.43, 159.95),
    # North America
    "nyc": ("New York", "US", 40.71, -74.01), "iad": ("Ashburn", "US", 39.04, -77.49), "bos": ("Boston", "US", 42.36, -71.06),
    "atl": ("Atlanta", "US", 33.75, -84.39), "mia": ("Miami", "US", 25.76, -80.19), "chi": ("Chicago", "US", 41.88, -87.63),
    "dfw": ("Dallas", "US", 32.78, -96.80), "hou": ("Houston", "US", 29.76, -95.37), "den": ("Denver", "US", 39.74, -104.99),
    "mci": ("Kansas City", "US", 39.10, -94.58), "slc": ("Salt Lake City", "US", 40.76, -111.89), "phx": ("Phoenix", "US", 33.45, -112.07),
    "lax": ("Los Angeles", "US", 34.05, -118.24), "sjc": ("San José", "US", 37.34, -121.89), "sea": ("Seattle", "US", 47.61, -122.33),
    "msp": ("Minneapolis", "US", 44.98, -93.27), "jax": ("Jacksonville", "US", 30.33, -81.66), "anc": ("Anchorage", "US", 61.22, -149.90),
    "yyz": ("Toronto", "CA", 43.65, -79.38), "yul": ("Montreal", "CA", 45.50, -73.57), "yvr": ("Vancouver", "CA", 49.28, -123.12),
    "ycg": ("Calgary", "CA", 51.05, -114.07), "ywg": ("Winnipeg", "CA", 49.90, -97.14), "yhz": ("Halifax", "CA", 44.65, -63.57),
    "yyt": ("St. John’s", "CA", 47.56, -52.71), "mex": ("Mexico City", "MX", 19.43, -99.13), "mty": ("Monterrey", "MX", 25.69, -100.32),
    "gdl": ("Guadalajara", "MX", 20.67, -103.35), "cun": ("Cancún", "MX", 21.16, -86.85),
    # Central America, Caribbean
    "gua": ("Guatemala City", "GT", 14.63, -90.51), "sal": ("San Salvador", "SV", 13.69, -89.22), "tgu": ("Tegucigalpa", "HN", 14.07, -87.19),
    "mga": ("Managua", "NI", 12.11, -86.24), "sjo": ("San José", "CR", 9.93, -84.08), "pty": ("Panama City", "PA", 8.98, -79.52),
    "bze": ("Belize City", "BZ", 17.50, -88.20), "hav": ("Havanna", "CU", 23.11, -82.37), "kin": ("Montego Bay", "JM", 18.47, -77.92),
    "sdq": ("Santo Domingo", "DO", 18.49, -69.93), "pap": ("Port-au-Prince", "HT", 18.59, -72.31), "sju": ("San Juan", "PR", 18.47, -66.11),
    "pos": ("Port of Spain", "TT", 10.66, -61.52), "bgi": ("Bridgetown", "BB", 13.10, -59.62), "nas": ("Nassau", "BS", 25.05, -77.35),
    "cur": ("Willemstad", "CW", 12.11, -68.93), "ptp": ("Pointe-à-Pitre", "GP", 16.24, -61.53), "fdf": ("Fort-de-France", "MQ", 14.60, -61.07),
    # South America
    "bog": ("Bogotá", "CO", 4.71, -74.07), "baq": ("Barranquilla", "CO", 10.96, -74.80), "ccs": ("Caracas", "VE", 10.48, -66.90),
    "uio": ("Quito", "EC", -0.18, -78.47), "gye": ("Guayaquil", "EC", -2.17, -79.92), "lim": ("Lima", "PE", -12.05, -77.04),
    "scl": ("Santiago", "CL", -33.45, -70.67), "eze": ("Buenos Aires", "AR", -34.60, -58.38), "cor": ("Córdoba", "AR", -31.42, -64.18),
    "mvd": ("Montevideo", "UY", -34.90, -56.16), "asu": ("Asunción", "PY", -25.26, -57.58), "lpb": ("La Paz", "BO", -16.50, -68.15),
    "gru": ("São Paulo", "BR", -23.55, -46.63), "rio": ("Rio de Janeiro", "BR", -22.91, -43.17), "for": ("Fortaleza", "BR", -3.73, -38.53),
    "poa": ("Porto Alegre", "BR", -30.03, -51.23), "bsb": ("Brasília", "BR", -15.79, -47.88), "ssa": ("Salvador", "BR", -12.97, -38.50),
    "rec": ("Recife", "BR", -8.05, -34.88), "bel": ("Belém", "BR", -1.46, -48.49), "geo": ("Georgetown", "GY", 6.80, -58.16),
    "pbm": ("Paramaribo", "SR", 5.85, -55.20), "cay": ("Cayenne", "GF", 4.92, -52.31), "cok": ("Kochi", "IN", 9.93, 76.27),
}

# Land routes: (a, b[, factor[, [(lat, lon) via points]]]) - long-distance corridors that carry fibre today (motorways, railways,
# pipelines). No edge crosses a sea here (only the cables from the cable data do); on coasts, via points lead over land.
CORRIDORS = [
    # Central Europe
    ("fra", "ams"), ("fra", "bru"), ("fra", "lux"), ("fra", "par"), ("fra", "zrh"), ("fra", "muc"), ("fra", "dus"), ("fra", "ham"),
    ("fra", "ber"), ("fra", "prg"), ("dus", "ams"), ("dus", "bru"), ("dus", "ham"), ("ham", "ber"), ("ham", "cph", 1.0, [(54.45, 11.2)]),
    ("ham", "ams"), ("ham", "aar"), ("aar", "cph", 1.0, [(55.47, 9.47)]), ("ber", "waw"), ("ber", "prg"), ("muc", "vie"), ("muc", "zrh"),
    ("muc", "prg"), ("muc", "mil", 1.0, [(47.27, 11.39)]), ("ams", "bru"), ("bru", "par"), ("bru", "lux"), ("lux", "par"), ("par", "lyo"),
    ("lyo", "mrs"), ("lyo", "gva"), ("gva", "zrh"), ("par", "bod"), ("bod", "bio"), ("bod", "mrs", 1.0, [(43.60, 1.44)]),
    ("mrs", "bcn", 1.0, [(43.34, 3.22), (42.70, 2.90)]), ("bcn", "mad"), ("bcn", "vlc"), ("vlc", "mad"), ("mad", "lis"), ("mad", "bio"),
    ("mrs", "mil", 1.0, [(43.70, 7.26), (44.41, 8.93)]), ("mil", "zrh"), ("mil", "gva", 1.0, [(45.74, 7.32)]), ("mil", "rom", 1.0, [(44.49, 11.34), (43.77, 11.25)]),
    ("rom", "pmo", 1.0, [(40.85, 14.27), (39.30, 16.25), (38.11, 15.65)]), ("rom", "bri", 1.0, [(40.85, 14.27)]), ("mil", "lju", 1.0, [(45.44, 12.33), (45.65, 13.78)]),
    ("vie", "bts"), ("bts", "bud"), ("vie", "bud"), ("vie", "prg"), ("vie", "lju", 1.0, [(47.07, 15.44)]), ("lju", "zag"), ("zag", "bud"),
    ("zag", "beg", 1.8), ("beg", "bud", 1.8), ("beg", "sof", 1.8), ("beg", "skp", 1.8), ("skp", "sof", 1.8), ("skp", "skg", 1.8), ("skg", "ath", 1.8), ("skg", "sof", 1.8),
    ("skg", "ist", 1.8, [(41.15, 26.0)]), ("sof", "ist", 1.8), ("sof", "buh", 1.8), ("buh", "bud", 1.8, [(46.77, 23.6)]), ("buh", "kiv", 1.6), ("kiv", "kbp"),
    ("kiv", "ods"), ("ods", "kbp"), ("tia", "tgd", 1.8), ("tgd", "sjj", 1.8), ("sjj", "zag", 1.8), ("sjj", "beg", 1.8), ("tia", "skp", 1.8), ("tia", "skg", 1.8, [(40.6, 21.0)]),
    ("waw", "vno"), ("vno", "rix"), ("rix", "tll"), ("waw", "prg", 1.0, [(51.1, 17.03)]), ("waw", "kbp", 1.0, [(49.84, 24.03)]), ("waw", "msq"),
    ("waw", "bts", 1.0, [(50.06, 19.94)]), ("msq", "vno"), ("cph", "got"), ("got", "osl"), ("got", "sto"), ("osl", "sto"),
    ("hel", "led"), ("tll", "led"), ("lon", "man"), ("lon", "brs"), ("brs", "man"), ("man", "edi"), ("lon", "edi", 1.0, [(53.8, -1.55)]),
    ("dub", "ork"), ("ist", "ank", 1.4),
    # Russia, Caucasus, Central Asia (long routes cost more)
    ("led", "mow"), ("msq", "mow"), ("kbp", "mow", 3.0), ("mow", "rov"), ("rov", "tbs", 2.4), ("mow", "ekb", 2.0), ("ekb", "ovb", 2.6),
    ("ovb", "irk", 2.6), ("irk", "khv", 2.8, [(52.03, 113.5), (50.27, 127.53)]), ("khv", "vvo", 2.0), ("irk", "uln", 2.4), ("uln", "pek", 3.0),
    ("ekb", "nqz", 2.2), ("nqz", "ala", 1.6), ("ala", "fru"), ("fru", "tas", 1.2), ("tas", "dyu", 1.2), ("tas", "asb", 2.0), ("asb", "thr", 2.6),
    ("ala", "urc", 3.2), ("urc", "lhw", 3.0), ("ank", "tbs", 1.8, [(39.9, 41.27)]), ("tbs", "evn"), ("tbs", "gyd"), ("gyd", "thr", 2.6),
    ("evn", "thr", 2.6),
    # Middle East
    ("ank", "dam", 2.6, [(37.0, 35.32), (36.2, 37.16)]), ("dam", "bey", 1.6), ("dam", "amm", 1.6), ("amm", "tlv"), ("amm", "bgw", 2.6),
    ("ank", "bgw", 2.6, [(37.91, 40.23), (36.34, 43.13)]), ("bgw", "kwi", 2.4), ("bgw", "thr", 2.6), ("thr", "kbl", 2.6, [(36.3, 59.6), (34.35, 62.2)]),
    ("thr", "khi", 2.6, [(29.5, 60.86), (30.18, 66.98)]), ("ruh", "jed"), ("ruh", "doh", 1.0, [(25.38, 49.59)]), ("ruh", "bah", 1.0, [(26.3, 50.0)]),
    ("ruh", "kwi"), ("ruh", "dxb", 1.0, [(23.6, 51.6), (24.45, 54.37)]), ("dxb", "fjr"), ("dxb", "mct", 1.0, [(24.2, 55.76)]), ("fjr", "mct"),
    ("jed", "amm", 2.6, [(28.38, 36.57)]), ("jed", "sah", 1.4), ("sah", "ade"), ("ruh", "amm", 2.6),
    # Africa (land routes cost more: few, often interrupted long routes)
    ("tlv", "cai", 1.2, [(31.13, 33.8)]), ("cai", "alx"), ("cai", "suz"), ("alx", "tip", 2.2, [(32.08, 23.96), (32.12, 20.07), (31.2, 16.59)]),
    ("tip", "tun", 1.8), ("tun", "alg", 1.2), ("alg", "cmn", 1.2, [(34.68, -1.91)]), ("cai", "kht", 2.2, [(24.09, 32.9)]), ("kht", "pzu", 1.6),
    ("kht", "add", 2.0), ("kht", "jub", 2.2), ("pzu", "asm", 2.0), ("asm", "add", 2.0), ("add", "jib", 1.3), ("add", "nbo", 2.0), ("nbo", "mba"),
    ("nbo", "kla", 1.2), ("kla", "kgl", 1.2), ("kgl", "bjm", 1.3), ("nbo", "dar", 1.3), ("kla", "jub", 1.5), ("dar", "lun", 1.8), ("lun", "hre", 1.2),
    ("hre", "jnb", 1.2), ("llw", "lun", 1.4), ("jnb", "gbe"), ("jnb", "dur"), ("jnb", "cpt"), ("jnb", "mpm"), ("jnb", "wdh", 1.3, [(-28.45, 21.26)]),
    ("wdh", "lad", 2.2), ("lun", "fih", 2.4, [(-11.66, 27.48)]), ("fih", "pnr", 1.3), ("pnr", "lbv", 1.5), ("lbv", "dla", 1.5, [(1.86, 9.77)]),
    ("dla", "abv", 2.0), ("dla", "ndj", 2.2), ("dla", "bgf", 2.2), ("abv", "los"), ("los", "coo", 1.6), ("coo", "lfw", 1.6), ("lfw", "acc", 1.6), ("acc", "abj", 1.6),
    ("abj", "oua", 1.8), ("oua", "bko", 1.8), ("bko", "dkr", 1.8), ("dkr", "bjl", 1.8), ("bjl", "oxb", 1.8), ("oxb", "cky", 1.8), ("dkr", "nkc", 1.8),
    ("nkc", "cmn", 2.2, [(27.15, -13.2), (30.42, -9.6)]), ("abj", "rob", 1.8), ("rob", "fna", 1.8), ("fna", "cky", 1.8), ("cky", "bko", 1.5),
    ("oua", "nim", 1.4), ("nim", "abv", 1.5, [(12.0, 8.52)]), ("nim", "ndj", 2.4), ("lad", "fih", 2.0),
    # South Asia
    ("bom", "del"), ("del", "ccu"), ("bom", "blr"), ("blr", "maa"), ("bom", "maa", 1.0, [(17.39, 78.49)]), ("maa", "ccu", 1.0, [(17.69, 83.22), (20.3, 85.82)]),
    ("del", "lhe", 3.0), ("lhe", "isb"), ("lhe", "khi"), ("isb", "kbl", 2.2), ("ccu", "dac", 1.2), ("ccu", "ktm", 1.3, [(26.73, 88.4)]),
    ("del", "ktm", 1.3), ("ccu", "pbh", 1.4, [(26.73, 88.4)]),
    # East and Southeast Asia
    ("sin", "kul"), ("kul", "bkk", 1.0, [(7.01, 100.47)]), ("bkk", "pnh"), ("pnh", "sgn"), ("sgn", "han", 1.1, [(16.05, 108.2)]), ("han", "can", 1.2, [(22.82, 108.32)]),
    ("bkk", "vte"), ("vte", "han", 1.2), ("bkk", "rgn", 1.3), ("rgn", "kmg", 2.2), ("kmg", "ctu"), ("kmg", "vte", 1.4), ("kmg", "han", 1.2),
    ("can", "hkg"), ("hkg", "mfm"), ("can", "sha"), ("sha", "pek"), ("pek", "fyj", 2.0, [(40.13, 124.39)]), ("can", "ctu"), ("ctu", "lhw"),
    ("lhw", "pek"), ("ctu", "pek"), ("sel", "pus"), ("tyo", "osa"), ("osa", "fuk"), ("cgk", "sub"),
    # Oceania
    ("syd", "mel"), ("syd", "bne"), ("mel", "adl"), ("adl", "per", 1.0, [(-32.13, 133.68), (-31.68, 128.88), (-30.75, 121.47)]),
    ("adl", "drw", 1.1, [(-23.7, 133.88)]), ("akl", "wlg"),
    # North America
    ("nyc", "bos"), ("nyc", "iad"), ("nyc", "chi", 1.0, [(41.5, -81.69)]), ("iad", "atl"), ("atl", "mia", 1.0, [(30.33, -81.66)]), ("atl", "jax"),
    ("jax", "mia"), ("atl", "dfw"), ("atl", "chi", 1.0, [(36.16, -86.78)]), ("chi", "mci"), ("chi", "msp"), ("msp", "den"), ("chi", "yyz", 1.0, [(42.33, -83.05)]),
    ("mci", "dfw"), ("mci", "den"), ("dfw", "hou"), ("hou", "atl", 1.0, [(30.45, -91.19)]), ("dfw", "phx", 1.0, [(32.22, -110.97)]), ("den", "slc"),
    ("slc", "sjc"), ("slc", "sea", 1.0, [(43.62, -116.2)]), ("phx", "lax"), ("lax", "sjc"), ("sjc", "sea", 1.0, [(45.52, -122.68)]), ("sea", "yvr"),
    ("yvr", "ycg"), ("ycg", "ywg"), ("ywg", "yyz", 1.1, [(48.38, -89.25)]), ("yyz", "yul"), ("yul", "yhz", 1.0, [(46.81, -71.21), (45.27, -66.06)]),
    ("yul", "nyc"), ("bos", "yul"), ("dfw", "mty"), ("mty", "mex"), ("mex", "gdl"), ("gdl", "lax", 1.1, [(29.07, -110.96), (32.51, -117.04)]),
    ("mex", "cun", 1.2, [(20.97, -89.62)]), ("sea", "anc", 1.4, [(53.92, -122.75), (60.72, -135.06), (62.0, -145.0)]), ("ywg", "msp"),
    # Central and South America
    ("mex", "gua", 1.2, [(16.75, -93.12)]), ("gua", "sal"), ("sal", "tgu"), ("tgu", "mga"), ("mga", "sjo"), ("sjo", "pty"), ("gua", "bze"),
    ("bog", "baq"), ("bog", "uio", 1.2), ("uio", "gye"), ("gye", "lim", 1.2, [(-5.19, -80.63)]), ("lim", "lpb", 1.4, [(-13.53, -71.97)]),
    ("lpb", "scl", 1.4, [(-18.48, -70.31)]), ("scl", "eze", 1.2, [(-32.89, -68.83)]), ("scl", "cor", 1.2, [(-32.89, -68.83)]), ("cor", "eze"),
    ("eze", "mvd", 1.0, [(-33.12, -58.31)]), ("eze", "asu", 1.2), ("asu", "gru", 1.3), ("mvd", "poa", 1.2), ("poa", "gru"), ("gru", "rio"),
    ("gru", "bsb"), ("rio", "bsb"), ("bsb", "ssa"), ("ssa", "rec"), ("rec", "for"), ("bsb", "bel", 1.3), ("bel", "for", 1.3, [(-2.53, -44.3)]),
    ("bog", "ccs", 1.3, [(7.89, -72.5)]), ("geo", "pbm", 1.3), ("pbm", "cay", 1.3),
]

# Sea routes: known submarine cable corridors between coastal cities that the cable data lacks or holds only in pieces. Drawn like a cable
# but marked as an estimated course; dearer than a mapped cable so that mapped cables win where they exist. Via points keep the way at sea.
SEA_ROUTES = [
    # Australia, Oceania, Pacific
    ("per", "sin", [(-20.0, 108.0), (-7.6, 104.6)]), ("drw", "sin", [(-10.5, 123.0), (-8.6, 116.0), (-5.5, 109.0)]), ("syd", "gum", [(-15.0, 157.5)]),
    ("syd", "akl"), ("syd", "hnl", [(-20.0, 178.0)]), ("akl", "hnl"), ("syd", "suv"), ("suv", "hnl"), ("gum", "tyo"), ("gum", "hnl"), ("gum", "mnl"),
    ("hnl", "sjc"), ("hnl", "lax"), ("hnl", "tyo"), ("ppt", "hnl"), ("apw", "suv"), ("tbu", "suv"), ("vli", "suv"), ("hir", "syd", [(-15.0, 155.0)]),
    ("pom", "syd", [(-14.0, 154.0)]), ("dil", "drw"), ("nou", "syd"), ("tyo", "sea", [(47.0, 165.0), (52.0, -170.0)]), ("tyo", "sjc", [(36.0, -170.0)]),
    ("tpe", "sjc", [(30.0, 160.0), (34.0, -170.0)]), ("hkg", "lax", [(21.0, 125.0), (28.0, 165.0), (30.0, -160.0)]),
    # East Asia
    ("pus", "fuk"), ("pus", "sha", [(33.5, 126.0)]), ("pus", "hkg", [(31.0, 125.5), (24.5, 120.5)]), ("tyo", "hkg", [(31.0, 133.0), (24.0, 123.5)]),
    ("tpe", "hkg", [(23.5, 118.0)]), ("mnl", "hkg"), ("cgk", "sin", [(-3.0, 106.8)]), ("sub", "sin", [(-5.5, 111.0), (-2.0, 107.5)]),
    ("bwn", "sin", [(3.0, 110.0)]), ("bwn", "hkg", [(12.0, 114.5)]), ("sin", "hkg", [(5.0, 106.5), (15.0, 112.0)]), ("sin", "mnl", [(5.5, 113.0)]),
    # Indian Ocean
    ("cmb", "maa"), ("cmb", "sin", [(5.5, 90.0), (5.8, 96.0)]), ("cmb", "bom", [(10.0, 75.5)]), ("mle", "cmb"), ("mle", "cok"), ("mru", "dur"),
    ("mru", "cok", [(-5.0, 68.0)]), ("mru", "tnr"), ("run", "mru"), ("tnr", "mba"), ("sez", "dar"), ("sez", "mba"), ("sez", "jib", [(5.0, 52.0)]),
    ("bom", "fjr", [(22.5, 63.0)]), ("jib", "fjr", [(13.0, 50.0), (17.5, 57.0), (22.5, 60.0)]), ("jib", "khi", [(13.0, 50.0), (18.0, 60.0)]),
    ("mct", "khi"), ("fjr", "doh", [(26.4, 56.4), (26.5, 53.0)]), ("jed", "pzu"), ("jed", "suz", [(26.0, 35.5)]), ("jib", "jed", [(15.0, 41.5)]), ("bom", "jib", [(14.0, 58.0)]), ("jib", "suz", [(15.0, 41.5), (22.0, 37.5), (27.5, 34.0)]),
    ("mba", "jib", [(2.0, 48.0)]), ("mgq", "mba", [(-1.0, 43.0)]), ("mgq", "jib", [(6.0, 49.5), (11.8, 51.5)]), ("dar", "mpm", [(-12.0, 41.5), (-20.0, 37.0)]), ("mpm", "dur"), ("cpt", "lad", [(-23.0, 12.0)]),
    # Atlantic, Africa
    ("lad", "lbv", [(-4.5, 10.5)]), ("lbv", "los", [(2.0, 6.0)]), ("los", "acc", [(5.5, 2.0)]), ("acc", "dkr", [(4.0, -8.0), (8.0, -15.5)]),
    ("dkr", "lis", [(22.0, -19.5), (33.0, -12.0)]), ("lis", "los", [(30.0, -14.0), (20.0, -19.5), (8.0, -16.0), (3.5, -3.0)]),
    ("lis", "cpt", [(30.0, -14.0), (15.0, -21.0), (0.0, -8.0), (-20.0, 5.0)]), ("lon", "lis", [(48.0, -7.0), (43.0, -10.5)]), ("ssg", "dla"), ("tms", "lbv"), ("rai", "dkr"), ("lpa", "cmn"), ("lpa", "lis"),
    ("for", "lis", [(10.0, -30.0)]), ("for", "dkr"), ("for", "mia", [(10.0, -50.0), (18.0, -65.0)]), ("gru", "lad", [(-20.0, -10.0)]),
    ("rio", "cpt", [(-30.0, -10.0)]), ("yyt", "yhz"), ("goh", "yyt"), ("goh", "kef"), ("kef", "dub"), ("kef", "cph", [(62.0, -7.0), (58.0, 3.0)]),
    ("nyc", "lon", [(48.0, -40.0)]), ("nyc", "bod", [(45.0, -30.0)]), ("iad", "bio", [(42.0, -30.0)]), ("yhz", "dub", [(52.0, -30.0)]),
    # Central America, Caribbean
    ("mia", "nas"), ("mia", "hav"), ("hav", "kin", [(20.0, -78.5)]), ("kin", "pty"), ("pty", "baq"), ("baq", "cur"), ("cur", "ccs"), ("ccs", "pos"),
    ("pos", "bgi"), ("geo", "pos", [(8.5, -59.5)]), ("cay", "fdf", [(9.0, -55.0)]), ("bgi", "fdf"), ("fdf", "ptp"), ("ptp", "sju"), ("sju", "sdq"), ("sju", "mia", [(22.0, -70.0)]), ("pap", "kin"),
    ("cun", "mia"), ("pty", "gye", [(3.0, -81.0)]), ("lim", "scl", [(-20.0, -73.0)]), ("pty", "lim", [(-5.0, -82.0)]),
    # Mediterranean, Europe
    ("mrs", "tun"), ("mrs", "alg"), ("pmo", "tun"), ("pmo", "mla"), ("mla", "tip"), ("ath", "nic"), ("nic", "tlv"), ("nic", "bey"),
    ("mrs", "alx", [(37.5, 12.0), (34.5, 22.0)]), ("ath", "alx"), ("ist", "ods"), ("bcn", "pmo", [(40.5, 8.0)]), ("lon", "ams"), ("lon", "dub"),
    ("cph", "osl"), ("sto", "hel"), ("hel", "tll"), ("aar", "osl"), ("edi", "tor"), ("tor", "kef"), ("mad", "cmn", [(36.0, -5.6)]),
]

# Costs (km x factor)
F_CABLE = 1.0
F_CABLE_UNNAMED = 1.15
UNNAMED_KM = 50.0
F_LAND = 1.1
F_LANDING = 1.5
F_ACCESS = 1.2
F_SEA = 1.3
F_RADIO = 2.0
F_SAT = 6.0
LANDING_MAX_KM = 900.0             # landing point -> nearest hub
LANDING_SECOND = 1.25              # ... and the second nearest when it is hardly further
ACCESS_LANDING_KM = 450.0          # country without a hub: landing points this near the country point count as "in the country"
ACCESS_LAND_KM = 2500.0            # country without hub and cable (inland): land route to the nearest hubs
RADIO_MAX_KM = 120.0               # island without a mapped cable: up to here microwave (usual span over water) ...
SEA_ACCESS_MAX_KM = 3000.0         # ... above that a cable (course estimated - the data does not hold all), very far: satellite
HOP_HUB_KM = 250.0                 # place of a route -> hub
HOP_LANDING_KM = 150.0             # ... else a landing point
NODE_MERGE = 0.25                  # cable nodes this near (map units, ~10 km) are the same place (cables change at the station)
KM_PER_MS = 200.0                  # light in glass, out and back -> RTT/2
GEO_RTT_MS = 480.0                 # geostationary satellite: round trip at least ~480 ms
GEO_JUMP_MS = 350.0
SIMPLIFY = 0.06                    # map units (Douglas-Peucker) for the answer

# Islands and island states (access only over cable, radio or satellite; never a land route over the sea)
ISLANDS = frozenset((
    "GB IE IS JP TW PH ID NZ AU LK MT CY SG BH MV MU MG CU JM DO HT PR TT BS BB FJ PG NC PF RE GL FO CV KM SC BM KY "
    "KI TV NR TO WS VU SB FM MH PW CK NU TK WF AS GU MP PN SH FK GS TC VG VI AI MS KN AG DM LC VC GD AW CW SX BQ MF BL GP MQ PM YT IO "
    "CC CX NF HM BV TF AQ ST GQ BN TL JE GG IM AX SJ").split())

# Destinations that are (almost) only reached by satellite today (research stations, remote islands without a cable)
SAT_COUNTRIES = frozenset("AQ PN TK FK GS HM TF IO BV KI TV NR NF CC CX".split())

# Satellite networks: AS number -> orbit. Besides the table only the neutral words below are recognised (geostationary).
SAT_ASN = {14593: "leo", 27277: "leo", 7155: "geo", 6621: "geo", 22351: "geo", 31515: "geo"}
SAT_WORDS = re.compile(r"satellite|satcom", re.I)


def satellite(asn, name=None):
    """Operator -> "leo" (low orbit), "geo" (geostationary) or None."""
    try:
        a = int(asn or 0)
    except (TypeError, ValueError):
        a = 0
    if a in SAT_ASN:
        return SAT_ASN[a]
    n = str(name or "")
    if n and SAT_WORDS.search(n):
        return "geo"
    return None


# ---------------------------------------------------------------------------------------------------------- geometry
P = CR.project
distance_km = CR.distance_km


def _great_circle(a, b, step_km=180.0):
    """(lat, lon) -> (lat, lon): points on the great circle (without the start point)."""
    d = distance_km(a[0], a[1], b[0], b[1])
    n = max(1, int(math.ceil(d / step_km)))
    p1, l1, p2, l2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    w = d / 6371.0088
    if w < 1e-9:
        return [b]
    out = []
    for i in range(1, n + 1):
        f = i / n
        s1, s2 = math.sin((1 - f) * w) / math.sin(w), math.sin(f * w) / math.sin(w)
        x = s1 * math.cos(p1) * math.cos(l1) + s2 * math.cos(p2) * math.cos(l2)
        y = s1 * math.cos(p1) * math.sin(l1) + s2 * math.cos(p2) * math.sin(l2)
        z = s1 * math.sin(p1) + s2 * math.sin(p2)
        out.append((math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))))
    return out


def _line_ll(points_ll):
    """[(lat, lon), ...] -> (km, [(x, y), ...]) over great circles between the points."""
    km, pts = 0.0, [P(points_ll[0][1], points_ll[0][0])]
    for a, b in zip(points_ll, points_ll[1:]):
        km += distance_km(a[0], a[1], b[0], b[1])
        pts.extend(P(q[1], q[0]) for q in _great_circle(a, b))
    return km, pts


def _simplify(pts, tol):
    """Douglas-Peucker (map units); jumps at the date line are kept."""
    if len(pts) <= 2:
        return list(pts)
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    st = [(0, len(pts) - 1)]
    while st:
        a, b = st.pop()
        ax, ay = pts[a]
        bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        ll = math.hypot(dx, dy) or 1e-12
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            x, y = pts[i]
            if abs(x - pts[i - 1][0]) > 400 or abs(pts[i + 1][0] - x) > 400:
                d = 1e9                                     # date line: keep the point
            else:
                d = abs(dy * (x - ax) - dx * (y - ay)) / ll
            if d > best:
                best, bi = d, i
        if best > tol:
            keep[bi] = True
            st.append((a, bi))
            st.append((bi, b))
    return [p for p, k in zip(pts, keep) if k]


# ---------------------------------------------------------------------------------------------------------- country shapes
def _object_after(text, key):
    """JSON object behind "key:" in a generated JS file (the world map) -> dict or None."""
    i = text.find(key + ":")
    if i < 0:
        return None
    i = text.find("{", i)
    depth, j, in_str = 0, i, False
    while j < len(text):
        c = text[j]
        if in_str:
            if c == "\\":
                j += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[i:j + 1])
        j += 1
    return None


def load_world(path):
    """The world map (generated JS file, same projection) -> {"countries": {cc: {"d", "x", "y", "o"?}}, "points": {cc: [x, y]}} or None."""
    try:
        with open(path, encoding="utf-8") as f:
            t = f.read()
        return {"countries": _object_after(t, "countries") or {}, "points": _object_after(t, "points") or {}}
    except (OSError, ValueError):
        return None


def country_points(world):
    """World map -> {cc: {"lat", "lon", "x", "y"}}: the point of every country (label point, or the point of a territory without a
    shape); latitude and longitude come from the inverse projection. The ways start and end exactly at these map points."""
    out = {}
    w = world or {}
    pts = {}
    for cc, c in (w.get("countries") or {}).items():
        if isinstance(c, dict) and isinstance(c.get("x"), (int, float)) and isinstance(c.get("y"), (int, float)):
            pts[cc] = (float(c["x"]), float(c["y"]))
    for cc, p in (w.get("points") or {}).items():
        if isinstance(p, list) and len(p) == 2 and cc not in pts:
            pts[cc] = (float(p[0]), float(p[1]))
    for cc, (x, y) in pts.items():
        lon, lat = CR.unproject(x, y)
        out[cc] = {"lat": lat, "lon": lon, "x": x, "y": y}
    return out


class CountryShapes:
    """Point (map units) -> country (ISO-A2) over the shapes of the world map (even-odd rule); microstates without a shape over
    their point (at most 1 unit, ~40 km); coastal points just outside the coarse shape over the nearest edge (at most 1.2 units)."""

    def __init__(self, world):
        self.rings = {}                 # cc -> [[(x, y)...]]
        self.box = {}
        self.points = {}
        w = world or {}
        for cc, c in (w.get("countries") or {}).items():
            rings = []
            for part in ("d", "o"):
                try:
                    rings.extend(CR.read_path((c or {}).get(part) or ""))
                except (TypeError, ValueError, AttributeError):
                    pass
            if rings:
                self.rings[cc] = rings
                xs = [p[0] for r in rings for p in r]
                ys = [p[1] for r in rings for p in r]
                self.box[cc] = (min(xs), min(ys), max(xs), max(ys))
        for cc, p in (w.get("points") or {}).items():
            if isinstance(p, list) and len(p) == 2:
                self.points[cc] = (float(p[0]), float(p[1]))

    def __bool__(self):
        return bool(self.rings)

    @staticmethod
    def _inside(x, y, rings):
        inside = False
        for r in rings:
            n = len(r)
            for i in range(n):
                x1, y1 = r[i]
                x2, y2 = r[(i + 1) % n]
                if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-12) + x1:
                    inside = not inside
        return inside

    @staticmethod
    def _distance(x, y, rings):
        best = 1e9
        for r in rings:
            n = len(r)
            for i in range(n):
                x1, y1 = r[i]
                x2, y2 = r[(i + 1) % n]
                dx, dy = x2 - x1, y2 - y1
                t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / ((dx * dx + dy * dy) or 1e-12)))
                best = min(best, math.hypot(x - x1 - t * dx, y - y1 - t * dy))
        return best

    def country_at(self, x, y):
        near = min(((math.hypot(x - p[0], y - p[1]), cc) for cc, p in self.points.items()), default=(1e9, None))
        if near[0] <= 1.0:
            return near[1]
        cand = [cc for cc, (x0, y0, x1, y1) in self.box.items() if x0 - 1 <= x <= x1 + 1 and y0 - 1 <= y <= y1 + 1]
        for cc in cand:
            if self._inside(x, y, self.rings[cc]):
                return cc
        best = min(((self._distance(x, y, self.rings[cc]), cc) for cc in cand), default=(1e9, None))
        return best[1] if best[0] <= 1.2 else None


# ---------------------------------------------------------------------------------------------------------- graph
class InfraNet:
    """The infrastructure graph (built once per cable data set)."""

    def __init__(self, data=None, countries=None, shapes=None):
        self.shapes = shapes if isinstance(shapes, CountryShapes) else CountryShapes(shapes)
        self.lat, self.lon, self.x, self.y, self.kind, self.key, self.name, self.cc = [], [], [], [], [], [], [], []
        self.adj = []                 # i -> [(j, cost, edge, forward)]
        self.edges = []               # [{"a": kind, "p": [(x, y)...] (from u to v), "km", "n", "u", "v"}]
        self.index = {}               # key -> node
        self.countries = countries or {}
        self._grid = {}
        for k, (n, cc, la, lo) in HUBS.items():
            self._node("h:" + k, la, lo, "hub", n, cc)
        for k in CORRIDORS:
            a, b = k[0], k[1]
            f = k[2] if len(k) > 2 else 1.0
            via = k[3] if len(k) > 3 else []
            ua, ub = self.index["h:" + a], self.index["h:" + b]
            km, pts = _line_ll([(self.lat[ua], self.lon[ua])] + list(via) + [(self.lat[ub], self.lon[ub])])
            self._edge(ua, ub, "land", pts, km, km * F_LAND * f)
        for k in SEA_ROUTES:
            ua, ub = self.index["h:" + k[0]], self.index["h:" + k[1]]
            via = k[2] if len(k) > 2 else []
            km, pts = _line_ll([(self.lat[ua], self.lon[ua])] + list(via) + [(self.lat[ub], self.lon[ub])])
            self._edge(ua, ub, "sea", pts, km, km * F_SEA + 100)
        self.cables_stand = None
        self.landings = []
        if isinstance(data, dict) and isinstance(data.get("sea"), list):
            self.cables_stand = data.get("date")
            self._cables(data)
        self._attach()
        self._hubs_ll = [(i, self.lat[i], self.lon[i]) for i in range(len(self.kind)) if self.kind[i] == "hub"]

    # -------------------------------------------------------------- build
    def _node(self, key, la, lo, kind, name=None, cc=None, xy=None):
        x, y = xy if xy is not None else P(lo, la)
        i = len(self.kind)
        self.lat.append(la); self.lon.append(lo); self.x.append(x); self.y.append(y)
        self.kind.append(kind); self.key.append(key); self.name.append(name); self.cc.append(cc)
        self.adj.append([])
        self.index[key] = i
        return i

    def _edge(self, u, v, kind, pts, km, cost, name=None):
        e = len(self.edges)
        self.edges.append({"a": kind, "p": pts, "km": km, "n": name, "u": u, "v": v})
        self.adj[u].append((v, cost, e, True))
        self.adj[v].append((u, cost, e, False))
        return e

    def _cable_node(self, x, y):
        """Cable node at (x, y) - equal places of different cables (at most NODE_MERGE) are one node (change at the station)."""
        gx, gy = int(x / 0.5), int(y / 0.5)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in self._grid.get((gx + dx, gy + dy), ()):
                    if abs(self.x[i] - x) <= NODE_MERGE and abs(self.y[i] - y) <= NODE_MERGE:
                        return i
        lo, la = CR.unproject(x, y)
        i = self._node("k%d" % len(self.kind), la, lo, "cable", xy=(x, y))
        self._grid.setdefault((gx, gy), []).append(i)
        return i

    def _cables(self, data):
        for e, g in CR.graphs(data):
            if not g.edges:
                continue
            at = {i: self._cable_node(x, y) for i, (x, y) in enumerate(g.nodes)}
            seen = set()
            for i, lst in g.edges.items():
                for j, km, pts in lst:
                    k = (min(i, j), max(i, j), round(km, 3))
                    if k in seen or at[i] == at[j]:
                        continue
                    seen.add(k)
                    cost = km * F_CABLE if g.name else km * F_CABLE_UNNAMED + UNNAMED_KM
                    self._edge(at[i], at[j], "cable", [tuple(p) for p in pts], km, cost, g.name)
            for la, lo, k in g.landings:
                if k is not None and k in at:
                    self.landings.append(at[k])
        self.landings = sorted(set(self.landings))
        if self.shapes:
            for k in self.landings:
                self.cc[k] = self.shapes.country_at(self.x[k], self.y[k])

    def _nearest(self, la, lo, only=None, n=2, max_km=1e9):
        """Nearest nodes (kind in only) -> [(km, i)] ascending."""
        out = []
        for i in range(len(self.kind)):
            if only and self.kind[i] not in only:
                continue
            if abs(self.lat[i] - la) * 111 > max_km:
                continue
            d = distance_km(la, lo, self.lat[i], self.lon[i])
            if d <= max_km:
                out.append((d, i))
        out.sort()
        return out[:n]

    def _attach(self):
        """Landing points -> nearest hub in the same country (land route, course estimated). Without a hub in the country: the nearest
        hub of another country only if the country is no island and it is near (land route over the border); otherwise the landing
        point stays only an access of its country. Without a known country (no shapes): the nearest hub."""
        hubs = [i for i in range(len(self.kind)) if self.kind[i] == "hub"]
        for k in self.landings:
            la, lo, cc = self.lat[k], self.lon[k], self.cc[k]
            ds = sorted((distance_km(la, lo, self.lat[h], self.lon[h]), h) for h in hubs if abs(self.lat[h] - la) < 12)
            if not ds:
                continue
            same = [x for x in ds if cc and self.cc[x[1]] == cc]
            if same:
                targets = [same[0]] if same[0][0] <= LANDING_MAX_KM * 2 else []
                if len(same) > 1 and same[1][0] <= same[0][0] * LANDING_SECOND and same[1][0] <= 600:
                    targets.append(same[1])
            elif cc and cc not in ISLANDS and ds[0][0] <= 400:
                targets = [ds[0]]
            elif not cc and ds[0][0] <= 400:
                targets = [ds[0]]
            else:
                targets = []
            for d, h in targets:
                km, pts = _line_ll([(la, lo), (self.lat[h], self.lon[h])])
                cost = max(km, 5.0) * F_LANDING + max(0.0, km - 600) * 1.5      # long land legs to a landing point are rare
                self._edge(k, h, "landing", pts, km, cost)

    # -------------------------------------------------------------- access (home, countries)
    def access(self, key, la, lo, cc=None, island=None, xy=None, name=None, via=None):
        """Node for a point (home or country point) with access, radio or satellite edges. -> node index.
        Order: hubs in the same country (up to two) -> landing points near the point -> land route (no island state) -> radio -> sea
        route -> satellite."""
        if key in self.index:
            return self.index[key]
        i = self._node(key, la, lo, "country" if key.startswith("l:") else "home", name, cc, xy=xy)
        island = (cc in ISLANDS) if island is None else island

        def line(z):                                       # starts exactly at the world map point (not at the projection of lat/lon)
            km, pts = _line_ll([(la, lo), (self.lat[z], self.lon[z])])
            pts[0] = (self.x[i], self.y[i])
            return km, pts

        own = sorted((distance_km(la, lo, self.lat[h], self.lon[h]), h) for h, hla, hlo in self._hubs_ll if self.cc[h] == cc) if cc else []
        if via is not None:                                # backed: every measured way leaves the home over this hub
            own = [(distance_km(la, lo, self.lat[via], self.lon[via]), via)]
        connected = False
        if own:
            for d, h in own[:2]:
                if d > 2500:
                    continue
                km, pts = line(h)
                self._edge(i, h, "access", pts, km, max(km, 2.0) * F_ACCESS)
                connected = True
        if not connected:
            lset = set(self.landings)
            if self.shapes and cc:
                lands = sorted((distance_km(la, lo, self.lat[k], self.lon[k]), k) for k in self.landings if self.cc[k] == cc)[:2]
            else:
                lands = [x for x in self._nearest(la, lo, only=("cable",), n=40, max_km=ACCESS_LANDING_KM) if x[1] in lset][:2]
            for d, k in lands:
                km, pts = line(k)
                self._edge(i, k, "access", pts, km, max(km, 2.0) * F_ACCESS)
                connected = True
        if not connected and not island:
            for d, h in self._nearest(la, lo, only=("hub",), n=2, max_km=ACCESS_LAND_KM):
                km, pts = line(h)
                self._edge(i, h, "access", pts, km, max(km, 2.0) * F_ACCESS)
                connected = True
        if not connected and cc not in SAT_COUNTRIES:
            lset = set(self.landings)
            near = [x for x in self._nearest(la, lo, only=("hub", "cable"), n=30) if self.kind[x[1]] == "hub" or x[1] in lset][:1]
            if near and near[0][0] <= RADIO_MAX_KM:
                d, k = near[0]
                km, pts = line(k)
                self._edge(i, k, "radio", pts, km, max(km, 5.0) * F_RADIO)
                connected = True
            elif near and near[0][0] <= SEA_ACCESS_MAX_KM:
                d, k = near[0]
                km, pts = line(k)
                self._edge(i, k, "sea", pts, km, km * F_SEA + 100)
                connected = True
        if not connected:
            gw = self._nearest(la, lo, only=("hub",), n=1)
            if gw:
                d, h = gw[0]
                self._edge(i, h, "sat", [(self.x[i], self.y[i]), (self.x[h], self.y[h])], d, 36000.0 * 2 * F_SAT / 10 + d)
        return i

    def gateway(self, i):
        """Earth station for satellite traffic to node i: the nearest hub (never i itself)."""
        g = self._nearest(self.lat[i], self.lon[i], only=("hub",), n=2)
        g = [x for x in g if x[1] != i]
        return g[0][1] if g else None

    # -------------------------------------------------------------- search
    def tree(self, start):
        """Dijkstra from the start over the whole graph -> (dist, prev) with prev[j] = (i, edge, forward). Country and home nodes are
        end points, not transit nodes (no way leads "through" a country over its centre)."""
        dist, prev, heap = {start: 0.0}, {}, [(0.0, start)]
        while heap:
            d, i = heapq.heappop(heap)
            if d > dist.get(i, 1e18):
                continue
            if i != start and self.kind[i] in ("country", "home"):
                continue
            for j, c, e, fw in self.adj[i]:
                nd = d + c
                if nd < dist.get(j, 1e18):
                    dist[j] = nd
                    prev[j] = (i, e, fw)
                    heapq.heappush(heap, (nd, j))
        return dist, prev

    def search(self, a, b, preferred=None):
        """A* from a to b -> [(edge, forward)] or None. preferred: cable names that cost half (estimate of a crossing)."""
        if a == b:
            return []
        pref = set(preferred or ())
        fmin = 0.5 if pref else 1.0
        bla, blo = self.lat[b], self.lon[b]
        h = lambda i: distance_km(self.lat[i], self.lon[i], bla, blo) * fmin
        dist, prev, heap = {a: 0.0}, {}, [(h(a), 0.0, a)]
        closed = set()
        while heap:
            _, d, i = heapq.heappop(heap)
            if i == b:
                break
            if i in closed:
                continue
            closed.add(i)
            for j, c, e, fw in self.adj[i]:
                if j != b and self.kind[j] in ("country", "home"):
                    continue
                k = self.edges[e]
                if k["a"] == "sat":
                    continue
                if pref and k["a"] == "cable" and k["n"] in pref:
                    c *= 0.5
                nd = d + c
                if nd < dist.get(j, 1e18):
                    dist[j] = nd
                    prev[j] = (i, e, fw)
                    heapq.heappush(heap, (nd + h(j), nd, j))
        if b not in prev:
            return None
        way, i = [], b
        while i != a:
            i0, e, fw = prev[i]
            way.append((e, fw))
            i = i0
        return way[::-1]

    def snap(self, la, lo):
        """Place of a route -> nearest hub (at most HOP_HUB_KM) or landing point (at most HOP_LANDING_KM) or None."""
        best = None
        for h, hla, hlo in self._hubs_ll:
            if abs(hla - la) > 3:
                continue
            d = distance_km(la, lo, hla, hlo)
            if d <= HOP_HUB_KM and (best is None or d < best[0]):
                best = (d, h)
        if best:
            return best[1]
        lands = self._nearest(la, lo, only=("cable",), n=10, max_km=HOP_LANDING_KM)
        lset = set(self.landings)
        lands = [x for x in lands if x[1] in lset]
        return lands[0][1] if lands else None


# ---------------------------------------------------------------------------------------------------------- ways
def _tree_way(prev, start, j):
    way = []
    while j != start:
        if j not in prev:
            return None
        i, e, fw = prev[j]
        way.append((e, fw))
        j = i
    return way[::-1]


def _origin(net, way):
    kinds = {net.edges[e]["a"] for e, _ in way}
    if "sat" in kinds:
        return "sat"
    if "radio" in kinds:
        return "radio"
    if "cable" in kinds or "sea" in kinds:
        return "cable"
    return "land"


def _via(net, way, start):
    """Names of the hubs along a way (for the tooltip), without repetition."""
    out = []
    for e, fw in way:
        k = net.edges[e]
        j = k["v"] if fw else k["u"]
        if net.kind[j] == "hub" and (not out or out[-1] != net.name[j]):
            out.append(net.name[j])
    return out


def _cable_names(net, way):
    out = []
    for e, _ in way:
        n = net.edges[e]["n"] if net.edges[e]["a"] == "cable" else None
        if n and n not in out:
            out.append(n)
    return out


def _rtt_jump(sections):
    """Largest latency jump between two sections with a latency -> (jump_ms, index of the section before) or (0, None)."""
    prev, best = None, (0.0, None)
    for i, s in enumerate(sections if isinstance(sections, list) else []):
        r = s.get("rtt") if isinstance(s, dict) else None
        if not isinstance(r, (int, float)) or isinstance(r, bool):
            continue
        if prev is not None and r - prev[0] > best[0]:
            best = (r - prev[0], prev[1])
        prev = (r, i)
    return best


def _reached(sections):
    """True when the last section is the destination and answered (with a latency)."""
    last = next((s for s in reversed(sections if isinstance(sections, list) else []) if isinstance(s, dict)), None)
    return bool(last and last.get("kind") == "target" and last.get("reached") and isinstance(last.get("rtt"), (int, float)))


def _hop_places(sections):
    """Like ``cableroute.route_places`` (same order and counting, so that "step" of the cable estimate fits), plus per place: sure,
    index, home flag. -> [(lat, lon, name, rtt, cc, sure, index, is_home)]"""
    out = []
    for s in sections if isinstance(sections, list) else []:
        if not isinstance(s, dict):
            continue
        p = s.get("place")
        try:
            lat, lon = float(p["lat"]), float(p["lon"])
        except (TypeError, KeyError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        cc = p.get("cc") if isinstance(p.get("cc"), str) else (s.get("cc") if isinstance(s.get("cc"), str) else None)
        rtt = s.get("rtt")
        r = float(rtt) if isinstance(rtt, (int, float)) and not isinstance(rtt, bool) and math.isfinite(rtt) else None
        if out and abs(out[-1][0] - lat) < 0.01 and abs(out[-1][1] - lon) < 0.01:
            if out[-1][3] is None and r is not None:
                out[-1] = out[-1][:3] + (r,) + out[-1][4:]
            continue
        out.append((lat, lon, str(p.get("name") or "")[:60], r, cc, bool(p.get("sure")), len(out), p.get("source") == "start"))
    return out


def _smooth(places):
    """Places without outliers: an unsure place that forces a detour > 1.8 x + 600 km drops out (geo databases often err)."""
    out = list(places)
    i = 1
    while 0 < i < len(out) - 1:
        a, w, c = out[i - 1], out[i], out[i + 1]
        direct = distance_km(a[0], a[1], c[0], c[1])
        detour = distance_km(a[0], a[1], w[0], w[1]) + distance_km(w[0], w[1], c[0], c[1])
        if detour > 1.8 * direct + 600 and not w[5]:
            del out[i]
            i = max(1, i - 1)
        else:
            i += 1
    return out


def _id(e, fw):
    return str(e) if fw else "~" + str(e)


def _enter(net, way, cc):
    """Index in the way from which it is inside the destination country (first hub of the country) - the country sheet cuts there."""
    for n, (e, fw) in enumerate(way):
        k = net.edges[e]
        j = k["v"] if fw else k["u"]
        if net.kind[j] == "hub" and net.cc[j] == cc:
            return n + 1
    return None


def _exit(net, routes):
    """The hub over which the measured ways usually leave the home (first place at a hub) - or None without a measurement."""
    count = {}
    for r in routes or []:
        for o in _hop_places(r.get("sections") or []):
            if o[7]:
                continue
            k = net.snap(o[0], o[1])
            if k is not None and net.kind[k] == "hub":
                count[k] = count.get(k, 0) + 1
            break
    if not count:
        return None
    k, n = max(count.items(), key=lambda x: (x[1], -x[0]))
    return k if n >= 2 else None


class InfraRoutes:
    """Computes and keeps the infrastructure routes of a home (graph once per cable data set, answers per home and route state)."""

    def __init__(self, countries, shapes=None):
        self.countries = countries or {}
        self.shapes = CountryShapes(shapes) if shapes and not isinstance(shapes, CountryShapes) else shapes
        self._net = None
        self._net_key = None
        self._cache = {}

    def net(self, data):
        sea = data.get("sea") if isinstance(data, dict) else None
        k = (id(sea) if sea is not None else None, str(data.get("date")) if isinstance(data, dict) else None)
        if self._net is None or self._net_key != k:
            self._net = InfraNet(data, self.countries, self.shapes)
            self._net_key = k
            self._cache = {}
        return self._net

    @staticmethod
    def stand(data, home, routes, version=None):
        """Fingerprint of everything the answer depends on (cable data, home, stored routes); the view fetches again when it changes."""
        h = hashlib.sha1()
        h.update(VERSION.encode())
        h.update(str((data or {}).get("date") if isinstance(data, dict) else None).encode())
        h.update(str(version).encode())
        h.update(json.dumps(home, sort_keys=True, default=str).encode())
        for r in routes or []:
            h.update(("%s|%s;" % (r.get("key"), r.get("t"))).encode())
        return h.hexdigest()[:16]

    def _home(self, home):
        """home = {cc[, lat, lon]} -> (lat, lon, cc, xy) from the country point unless the coordinates are given."""
        home = home or {}
        cc = home.get("cc")
        if isinstance(home.get("lat"), (int, float)) and isinstance(home.get("lon"), (int, float)):
            return float(home["lat"]), float(home["lon"]), cc, None
        c = self.countries.get(cc) or {}
        if isinstance(c.get("lat"), (int, float)) and isinstance(c.get("lon"), (int, float)):
            xy = (c["x"], c["y"]) if isinstance(c.get("x"), (int, float)) and isinstance(c.get("y"), (int, float)) else None
            return float(c["lat"]), float(c["lon"]), cc, xy
        return None

    def compute(self, data, home, routes=None, countries=None, version=None):
        """data: the cable data set (or None); home: {cc[, lat, lon]} (the country point, never a real place);
        routes: [{key, cc, t, sections, rtt, cables (estimates)}] -> answer:
          {"version", "stand", "cables_stand", "home": [x, y], "sources": [...],
           "edges": {id: {"kind", "p", "km", "name"}}, "hubs": {key: {"name", "cc", "p"}},
           "countries": {cc: {"path": [edge ids, "~" = reversed], "origin", "km", "via", "cables", "enter"?, "gateway_name"?,
                              "sat"?: {"path", "gateway", "gateway_name"}}},
           "routes": {key: {"path", "origin": "route"|"sat", "places", "measured_edges", "via", "cables", "end"?, "conflict"?,
                            "orbit"?, "jump_ms"?, "gateway"?, "gateway_name"?, "target"?}}}
        None if the home has no point."""
        st = self.stand(data, home, routes, version)
        if st in self._cache:
            return self._cache[st]
        pt = self._home(home)
        if pt is None:
            return None
        net = self.net(data)
        sla, slo, scc, sxy = pt
        exit_ = _exit(net, routes)
        s = net.access("s:%.3f,%.3f:%s" % (sla, slo, exit_), sla, slo, scc, island=False, xy=sxy, name="Home", via=exit_)
        country_node = {}
        for cc in sorted(countries or self.countries):
            c = self.countries.get(cc) or {}
            if not isinstance(c.get("lat"), (int, float)) or not isinstance(c.get("lon"), (int, float)):
                continue
            xy = (c["x"], c["y"]) if isinstance(c.get("x"), (int, float)) and isinstance(c.get("y"), (int, float)) else None
            country_node[cc] = net.access("l:" + cc, c["lat"], c["lon"], cc, xy=xy)
        dist, prev = net.tree(s)                           # one tree for all countries (equal corridors = equal edges)
        used = {}
        targets = {}
        for cc, j in country_node.items():
            w = _tree_way(prev, s, j)
            if not w:
                continue
            for e, _ in w:
                used[e] = True
            z = {"path": [_id(e, fw) for e, fw in w], "origin": _origin(net, w), "km": int(round(sum(net.edges[e]["km"] for e, _ in w))),
                 "via": _via(net, w, s)[:8], "cables": _cable_names(net, w)[:4]}
            ent = _enter(net, w, cc)
            if ent is not None:
                z["enter"] = ent
            if z["origin"] == "sat":
                last = net.edges[w[-1][0]]
                gk = last["u"] if last["v"] == j else last["v"]
                z["gateway_name"] = net.name[gk] if net.kind[gk] == "hub" else None
            else:
                g = net.gateway(j)
                if g is not None:
                    gw = _tree_way(prev, s, g) or []
                    for e, _ in gw:
                        used[e] = True
                    z["sat"] = {"path": [_id(e, fw) for e, fw in gw], "gateway": [round(net.x[g], 2), round(net.y[g], 2)],
                                "gateway_name": net.name[g]}
            targets[cc] = z
        measured = {}
        for r in routes or []:
            try:
                x = self._route(net, s, r)
            except (KeyError, TypeError, ValueError, IndexError):
                x = None
            if not x:
                continue
            for e, _ in x.pop("_w"):
                used[e] = True
            measured[r["key"]] = x
        edges = {}
        for e in sorted(used):
            k = net.edges[e]
            edges[str(e)] = {"kind": k["a"], "p": [[round(px, 2), round(py, 2)] for px, py in _simplify(k["p"], SIMPLIFY)],
                             "km": int(round(k["km"])), "name": k["n"]}
        hubs = {}
        for e in used:
            for i in (net.edges[e]["u"], net.edges[e]["v"]):
                if net.kind[i] == "hub":
                    hubs[net.key[i][2:]] = {"name": net.name[i], "cc": net.cc[i], "p": [round(net.x[i], 2), round(net.y[i], 2)]}
        out = {"version": VERSION, "stand": st, "cables_stand": net.cables_stand, "home": [round(net.x[s], 2), round(net.y[s], 2)],
               "sources": SOURCES_ALWAYS + (["cables_osm"] if net.landings else []),
               "edges": edges, "hubs": hubs, "countries": targets, "routes": measured}
        if len(self._cache) > 6:
            self._cache.clear()
        self._cache[st] = out
        return out

    def _route(self, net, s, r):
        """A measured route -> {"path", "origin": "route"|"sat", "places", "measured_edges", "via", "cables", ...}."""
        sections = r.get("sections") or []
        places = [o for o in _hop_places(sections) if not o[7]]      # the home place is no hop
        places = _smooth(places)
        est = {}
        for x in r.get("cables") or []:
            if isinstance(x, dict) and not x.get("alternative") and isinstance(x.get("step"), int) and x.get("name"):
                est.setdefault(x["step"], set()).add(x["name"])
        points = []                                            # (node, place, index in the places)
        for o in places:
            k = net.snap(o[0], o[1])
            if k is None:
                continue
            if points and points[-1][0] == k:
                continue
            points.append((k, o, o[6]))
        cc = r.get("cc")
        c = self.countries.get(cc) or {}
        way, cur, prev_idx = [], s, None
        for k, o, idx in points:
            pref = set()
            if prev_idx is not None:
                for st in range(prev_idx, idx):
                    pref |= est.get(st, set())
            w = net.search(cur, k, pref)
            if w is None:
                continue
            way.extend(w)
            cur, prev_idx = k, idx
        if not points and not way:
            return None
        out = {"origin": "route", "places": len(points), "measured_edges": len(way), "via": _via(net, way, s)[:10],
               "cables": _cable_names(net, way)[:4]}
        # destination: the country point - unless the latency excludes the country (anycast, database country differs from the location)
        j = net.index.get("l:" + cc) if cc else None
        rtt = r.get("rtt")
        if j is not None and cc and isinstance(rtt, (int, float)) and _reached(sections):
            reach = rtt / 2.0 * KM_PER_MS + 300
            far = distance_km(net.lat[s], net.lon[s], c.get("lat", 0), c.get("lon", 0)) - 900
            if far > reach:
                out["conflict"] = {"rtt": round(rtt, 1), "km": int(round(far + 900)), "place": net.name[cur] if net.kind[cur] == "hub" else None}
                j = None
        jump, _before = _rtt_jump(sections)
        if j is not None and isinstance(rtt, (int, float)) and rtt >= GEO_RTT_MS and jump >= GEO_JUMP_MS and _reached(sections) \
                and not any(net.cc[h] == cc for h, _, _ in net._hubs_ll):
            out["origin"] = "sat"
            out["orbit"] = "geo"
            out["jump_ms"] = round(jump)
            out["gateway"] = [round(net.x[cur], 2), round(net.y[cur], 2)]
            out["gateway_name"] = net.name[cur] if net.kind[cur] == "hub" else None
            out["target"] = [round(net.x[j], 2), round(net.y[j], 2)]
            j = None
        if j is not None:
            w = net.search(cur, j)
            if w:
                way.extend(w)
                cur = j
        if not way:
            return None
        if j is None and "target" not in out:
            out["end"] = [round(net.x[cur], 2), round(net.y[cur], 2)]
        out["path"] = [_id(e, fw) for e, fw in way]
        out["_w"] = way
        return out
