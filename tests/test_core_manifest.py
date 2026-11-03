"""Core manifest: every listed engine file exists, declares its namespace and API version, and stays neutral
(English only, no host- or person-specific vocabulary), so host applications can vendor it unchanged.

The vocabulary lists are stored as truncated SHA-256 hashes of lower-case words: the public repository names none of
the words it rejects."""
import hashlib
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# German words (function words and the vocabulary of the original engine)
GERMAN = {
    "075416261f4fae0f", "09890dd421f069bd", "0a7cb27e52927eac", "0e70f1c4acfadf67", "117682bdc9c802f5",
    "129a925cc58eb037", "130472dc07a5b960", "1932e644d5abd332", "19411365c2364410", "1c1c65e8f2de96f1",
    "1da8321dcc9a00ab", "1ebc8306b4fe2bfb", "200b00229fe68c4c", "247cdfbd926a8804", "2933954e9b42ed53",
    "2941689fe5e921ea", "2b172b91dba3b2f8", "338bcf5b2e53f193", "33f97911d11f1149", "3436db9d27d75b4c",
    "35487809a6ee83cc", "356d7d6c69a67f28", "37760ceac873107a", "3aa49f16d2a42286", "3ecd58f481fc9bf7",
    "424c8eda8e3a16a7", "42d7b01d7ed944a8", "46e6aa2c7a13b1d7", "4da271bb37e5a8c6", "5050d80d22ecd471",
    "515ea9b46d4f8f4a", "528d6a0b3d93aaec", "59d76fe0e3262d1f", "5a5e92771f65c7a5", "5b4d57b7a7ad56f1",
    "5bd1269b11fc4c7c", "5dc83ff5d5cdd02a", "65694373f868620f", "66692d74199c7799", "679e5df28b7d6a0c",
    "67c0d7ed00d48580", "6a8cd96a4b778934", "6d196499e0beb9b9", "6d9393e2a2f3f621", "75b17ec9692d92e4",
    "77c2d0f078e25fce", "78198e7ab262449a", "79ae28eeedf07e84", "7b24c1ad239d4a6b", "7ee69f810ab82668",
    "8155136610606747", "856cc99d87343400", "857e192cac5d5533", "8a8a1a9fe8565d64", "8bca6dada231e0fd",
    "8ff1f02b8f5a0370", "912864949c8adf2d", "9227a87d17bb6de0", "96489f509c4d989f", "9672fb3e59c6d2e3",
    "98970d1a1b801acf", "a303a7d1c0ffdb14", "a42517945927c258", "a665c12561dff12c", "a839ad3ed1f2bd38",
    "a8d79f40ddb79de5", "a9fb9d9b6ead015d", "ac276f4e32d209d2", "acabd0abc0fafd87", "b0dc4766642d9585",
    "b184048d4893820a", "b1957384abf4d8cb", "b2253f5de165a720", "b51c854170449c38", "b5f9036d932291cb",
    "b81d9593a3f6c4f7", "bb3b599c68537216", "c4c05a977d82536a", "c6816dc95102dc48", "cc9ed6bb85542035",
    "cd23c4c603b344f6", "cd635901d0a0cb49", "d058932161269d62", "d08200ba3ef70e5c", "d3cec99112255db9",
    "d5ec8a57ea0902c4", "d717f800853afcf7", "d941a36dbe67051b", "dd61d5c7fc0c86fd", "dd6b079f455bea52",
    "dd8cd5b13c674ae5", "e14348f31712a2b9", "e377e6a044b07539", "e91d032d46b8db0d", "e9b5b78ab02082e0",
    "ece863fe3a2d6522", "eeccef1ac21b1189", "f1b7384743db3616", "f3372afe904de693", "f76f9bc3920fd702",
    "fdc275ebe7bd74e1", "ffc40415928704ec"
}
# vocabulary of private host applications
PRIVATE = {
    "00e176eb5781c9a5", "011674d3d77facc7", "06dbfe29d4f9f2c3", "16188709fb9e8068", "1f4f4954cb64b172",
    "22b993144e0b1ff1", "26015552d60161dc", "2d69120f4a37384f", "4007d46292298e83", "406721eee872ec99",
    "58a22b4b9e1e7bf7", "6299c25d6fff4f45", "6429af1fb206f397", "64b4d0f47c93ce23", "6792adea40894066",
    "6d022eb93cfb20c4", "71772791ff32c319", "728e14e4f3e41ee6", "795b104abe3e4134", "7a40905683d375a9",
    "7b561231e8acddcc", "8802289820a2ae77", "8be0b5445fca6eb1", "8e6aa6b5008006ff", "98ba56e2bb7e0f7f",
    "9b89025ce7a6d932", "9f2a08fde416cef2", "a179e645bd839f7f", "a67c545a4952020e", "a9feb31c7b7094ce",
    "add39c7d76d33bb9", "b13610793f0b7fae", "bc874a49e470a75a", "bd558de5f7945767", "c1bcb6a60493cf2c",
    "ce29c26322abc211", "d7611927b900a7b4", "dda820158d142a0b", "e72ba210d9d2fac8", "ec6f049478046f72"
}


def manifest():
    with open(os.path.join(ROOT, "core-manifest.json"), encoding="utf-8") as f:
        return json.load(f)


def words(src):
    """Lower-case words of a source file; camelCase and snake_case identifiers are split into their parts."""
    out = set()
    for tok in re.findall(r"[A-Za-z\u00c0-\u017f]+", src):
        out.add(tok.lower())
        for part in re.findall(r"[A-Z\u00c0-\u00de]?[a-z\u00df-\u00ff]+|[A-Z]+(?![a-z])", tok):
            out.add(part.lower())
    return out


def digest(word):
    return hashlib.sha256(word.encode("utf-8")).hexdigest()[:16]


def test_manifest_format():
    m = manifest()
    assert m["core"] == 1 and m["files"], m
    for e in m["files"]:
        assert set(e) == {"path", "namespace", "api", "description"}, e
        assert isinstance(e["api"], int) and e["namespace"].startswith("Lanloom."), e


def test_manifest_files_exist_and_declare_api():
    for e in manifest()["files"]:
        p = os.path.join(ROOT, e["path"])
        assert os.path.isfile(p), e["path"]
        src = open(p, encoding="utf-8").read()
        name = e["namespace"].split(".", 1)[1]
        assert "root.Lanloom." + name + " = " in src, e["path"]
        assert re.search(r"const API = %d;" % e["api"], src), e["path"]
        assert "API = %d" % e["api"] in src.split("*/", 1)[0], e["path"]   # listed in the header as stable API


def test_manifest_files_are_neutral():
    for e in manifest()["files"]:
        src = open(os.path.join(ROOT, e["path"]), encoding="utf-8").read()
        assert not re.search(r"[\u00c4\u00d6\u00dc\u00e4\u00f6\u00fc\u00df]", src), e["path"]   # no umlauts
        found = {w for w in words(src) if digest(w) in GERMAN}
        assert not found, (e["path"], "German words", sorted(found))
        found = {w for w in words(src) if digest(w) in PRIVATE}
        assert not found, (e["path"], "private vocabulary", len(found))
        assert not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", src), e["path"]   # no addresses


def test_vocabulary_check_catches_words():
    # self-test of the hashing: German parts of mixed identifiers are recognised
    assert {w for w in words("routeBahn netz_knoten") if digest(w) in GERMAN} == {"bahn", "netz", "knoten"}
    assert digest("kachel") in GERMAN and "kachel" in words("routeKachelNow")
