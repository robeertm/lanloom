#!/usr/bin/env python3
"""Telemetry-free as a checked rule: every place in the shipped code that can open a network connection is listed
in security/egress.json with what it is for. A new place, or a fixed public host name, without an entry fails.

What counts as a place (Python, read with the ast module, nothing is imported or run):
  * a call of a function that opens a connection or a socket: urllib.request.urlopen / build_opener /
    OpenerDirector.open, http.client.HTTP(S)Connection, socket.socket / create_connection / socketpair is not,
    asyncio.open_connection / open_unix_connection, loop.create_connection / create_datagram_endpoint /
    sock_connect, smtplib.SMTP / SMTP_SSL, websockets.connect, ssl.get_server_certificate, subprocess calls of
    network tools (curl, wget, ping, nc);
  * the key is "<file>::<enclosing class.function>::<kind>", so moving code or a second call in the same
    function does not need a new entry, a new function with a connection does.
What counts as a host: a public host name in an http(s):// or ws(s):// literal of the shipped code (Python and the
web app's JavaScript). Documentation-only names (example.com/.net/.org, *.example, *.invalid, *.test, *.local,
localhost) are ignored. Each host entry says whether the code connects to it or only shows it as a link.

The browser side is closed by the Content Security Policy (connect-src 'self'), which tests/test_egress.py pins.

  egress_check.py [--root DIR] [--list] [--json]
Exit 0 = every place and host has an entry and every entry still exists; 1 = not; 2 = bad call.
"""
import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST = "security/egress.json"
PY_DIRS = ("lanloom", "updater")
JS_DIRS = ("lanloom/web",)
CATEGORIES = {
    "update-check": "release feed of the project, optional, can be switched off",
    "update-signature": "image registry of the project: signature of an update candidate, asked by the updater only "
                        "during an update or rollback an administrator started",
    "container-runtime": "local socket of the container runtime (one-click updater, local AI setup, host agent - read only)",
    "agent-link": "the optional host agent's reports to the one Lanloom it was paired with (no other destination)",
    "lan-scan": "probes of the network scanner, only private address ranges",
    "configured-source": "a device or service whose address an administrator entered (read-only integrations)",
    "configured-channel": "a delivery channel an administrator or user set up (report, notifications, heartbeat)",
    "configured-ai": "an AI service an administrator set up and switched on",
    "executor-link": "between the app and its own executor container",
    "own-connection-check": "TLS handshake to the addresses of the connections set up (certificate expiry)",
    "listener": "a socket that only receives (no outbound connection)",
    "addon-system": "the API of the add-on system inside the add-on network (add-on mode only, after an administrator "
                    "picks the broker it offers)",
    "local-host": "the host itself (loopback, own interfaces, kernel tables)",
}

CALLS = {
    # dotted callee (after import aliases are resolved) -> kind
    "urllib.request.urlopen": "http",
    "urllib.request.build_opener": "http",
    "urllib.request.OpenerDirector": "http",
    "http.client.HTTPConnection": "http",
    "http.client.HTTPSConnection": "http",
    "socket.socket": "socket",
    "socket.create_connection": "socket",
    "socket.getaddrinfo": "dns",
    "socket.gethostbyname": "dns",
    "socket.gethostbyname_ex": "dns",
    "socket.gethostbyaddr": "dns",
    "asyncio.open_connection": "socket",
    "asyncio.open_unix_connection": "unix-socket",
    "smtplib.SMTP": "smtp",
    "smtplib.SMTP_SSL": "smtp",
    "websockets.connect": "websocket",
    "websockets.client.connect": "websocket",
    "websockets.asyncio.client.connect": "websocket",
    "ssl.get_server_certificate": "tls",
}
# method names that open connections whatever object they are called on (event loop, opener)
METHODS = {
    "create_connection": "socket", "create_datagram_endpoint": "socket", "sock_connect": "socket",
    "create_unix_connection": "unix-socket", "open_connection": "socket", "getaddrinfo": "dns",
}
NET_TOOLS = {"curl", "wget", "ping", "ping6", "nc", "ncat", "socat", "nslookup", "dig", "arping"}
SUBPROCESS = {"subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call",
              "subprocess.check_output", "asyncio.create_subprocess_exec", "os.system", "os.popen"}
URL = re.compile(r"\b(?:https?|wss?)://([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?=[:/?#\"'\s]|$)")
DOC_HOSTS = re.compile(r"(?:^|\.)(?:example\.(?:com|net|org)|[a-z0-9-]+\.(?:example|invalid|test|local|localhost)|localhost)$", re.I)


class Finder(ast.NodeVisitor):
    def __init__(self, rel: str):
        self.rel = rel
        self.aliases = {}       # local name -> dotted module path
        self.stack = []
        self.sites = {}         # key -> first line
        self.hosts = {}         # host -> [file:line]

    # imports ---------------------------------------------------------------
    def visit_Import(self, node):
        for a in node.names:
            self.aliases[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module and not node.level:
            for a in node.names:
                self.aliases[a.asname or a.name] = "%s.%s" % (node.module, a.name)
        self.generic_visit(node)

    # scopes ----------------------------------------------------------------
    def _scope(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scope

    def dotted(self, node):
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        base = self.aliases.get(node.id, node.id)
        return ".".join([base] + list(reversed(parts)))

    def add(self, kind, node):
        key = "%s::%s::%s" % (self.rel, ".".join(self.stack) or "<module>", kind)
        self.sites.setdefault(key, node.lineno)

    def visit_Call(self, node):
        name = self.dotted(node.func)
        kind = CALLS.get(name or "")
        if kind is None and isinstance(node.func, ast.Attribute) and node.func.attr in METHODS:
            kind = METHODS[node.func.attr]
        if kind is None and name in SUBPROCESS:
            first = node.args[0] if node.args else None
            words = []
            if isinstance(first, (ast.List, ast.Tuple)) and first.elts and isinstance(first.elts[0], ast.Constant):
                words = [str(first.elts[0].value)]
            elif isinstance(first, ast.Constant) and isinstance(first.value, str):
                words = first.value.split()[:1]
            if words and Path(words[0]).name in NET_TOOLS:
                kind = "net-tool"
        if kind:
            self.add(kind, node)
        self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            for host in URL.findall(node.value):
                if not DOC_HOSTS.search(host):
                    self.hosts.setdefault(host.lower(), []).append("%s:%d" % (self.rel, node.lineno))


def scan(root: Path) -> tuple:
    sites, hosts = {}, {}
    for d in PY_DIRS:
        for path in sorted((root / d).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            f = Finder(rel)
            f.visit(ast.parse(path.read_text(encoding="utf-8"), filename=rel))
            sites.update({k: "%s:%d" % (rel, v) for k, v in f.sites.items()})
            for h, where in f.hosts.items():
                hosts.setdefault(h, []).extend(where)
    for d in JS_DIRS:
        for path in sorted((root / d).rglob("*.js")):
            rel = path.relative_to(root).as_posix()
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for host in URL.findall(line):
                    if not DOC_HOSTS.search(host):
                        hosts.setdefault(host.lower(), []).append("%s:%d" % (rel, n))
    return sites, hosts


def load_allowlist(root: Path) -> dict:
    return json.loads((root / ALLOWLIST).read_text(encoding="utf-8"))


def check(root: Path = ROOT) -> dict:
    """{"missing_sites", "stale_sites", "missing_hosts", "stale_hosts", "bad_entries", "sites", "hosts"}"""
    sites, hosts = scan(root)
    allow = load_allowlist(root)
    listed_sites = {e["site"]: e for e in allow.get("sites", [])}
    listed_hosts = {e["host"].lower(): e for e in allow.get("hosts", [])}
    bad = []
    for e in allow.get("sites", []):
        if e.get("category") not in CATEGORIES or not str(e.get("purpose", "")).strip():
            bad.append("site %s: category must be one of %s and purpose must be set" % (e.get("site"), ", ".join(CATEGORIES)))
    for e in allow.get("hosts", []):
        if e.get("use") not in ("connect", "link") or e.get("category") not in set(CATEGORIES) | {"link"} \
                or not str(e.get("purpose", "")).strip():
            bad.append("host %s: use connect|link, a known category and a purpose are required" % e.get("host"))
    return {
        "missing_sites": sorted((k, v) for k, v in sites.items() if k not in listed_sites),
        "stale_sites": sorted(k for k in listed_sites if k not in sites),
        "missing_hosts": sorted((h, w) for h, w in hosts.items() if h not in listed_hosts),
        "stale_hosts": sorted(h for h in listed_hosts if h not in hosts),
        "bad_entries": bad,
        "sites": sites,
        "hosts": hosts,
    }


def report(res: dict) -> str:
    out = []
    for key, where in res["missing_sites"]:
        out.append("NEW OUTBOUND PLACE without an entry: %s (%s)" % (key, where))
    for host, where in res["missing_hosts"]:
        out.append("NEW HOST without an entry: %s (%s)" % (host, ", ".join(where[:3])))
    for key in res["stale_sites"]:
        out.append("STALE entry, place no longer in the code: %s" % key)
    for host in res["stale_hosts"]:
        out.append("STALE entry, host no longer in the code: %s" % host)
    out.extend("BAD ENTRY: %s" % b for b in res["bad_entries"])
    if out:
        out.append("-> add or remove the entry in %s (category + purpose); a connection the user did not set up "
                   "breaks the no-telemetry rule (SECURITY.md)." % ALLOWLIST)
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--list", action="store_true", help="print every place and host found")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    res = check(a.root)
    if a.json:
        print(json.dumps({k: v for k, v in res.items()}, indent=1, default=list))
    elif a.list:
        for k, v in sorted(res["sites"].items()):
            print("site %-90s %s" % (k, v))
        for h, w in sorted(res["hosts"].items()):
            print("host %-40s %s" % (h, ", ".join(w[:3])))
    text = report(res)
    if text:
        print(text, file=sys.stderr)
        return 1
    if not a.json and not a.list:
        print("egress: %d places, %d hosts, all listed" % (len(res["sites"]), len(res["hosts"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
