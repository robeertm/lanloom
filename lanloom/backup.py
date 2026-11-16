"""Backup and restore of the Lanloom configuration: one encrypted file, a schedule, and a move to another host.

File format (``*.llbackup``)
----------------------------
``MAGIC`` (15 bytes) · file version (1 byte) · header length (4 bytes, big endian) · header (JSON) · ciphertext

* The header is readable without the passphrase. It only says what the file is: format numbers, the version that
  wrote it, when, whether history is inside, the key-derivation parameters and the wrapped data key. It never names a
  host, a user or a secret.
* Key: Argon2id(passphrase, random salt) -> 256-bit key-encryption key. A fresh random data key per file encrypts the
  content with AES-256-GCM; the data key itself is stored wrapped (AES-256-GCM) with the key-encryption key. Scheduled
  backups therefore never need the passphrase itself: the app keeps only the derived key (in its own vault).
* The whole prefix (magic, version, length, header) is the additional authenticated data of the content, so any
  change to the header is noticed. A wrong passphrase fails at the data key ("wrong passphrase"); a damaged file
  fails at the content ("damaged").
* Content: gzip-compressed JSON with the tables, the secrets (plaintext only inside the encrypted content) and two
  instance keys that must travel with the accounts (recovery-code pepper).

What is inside: settings, users and roles with their second factors, passkeys, recovery codes, credentials of every
connection, the inventory with every manual entry (names, kinds, hidden devices, "connected via"), firewall drafts,
the audit log and - optionally - history (scans, journal, nightly reports, data paths). Never inside: the instance key
(``secret.key``), sessions, open invitations, one-time setup codes, the updater's state or runtime caches.

Restore replaces the configuration in one transaction after a safety copy of the database, keeps the instance key of
the target (a moved instance has its own new key: every secret is encrypted again with it), ends every session and
writes one audit entry. An older backup is migrated; a backup from a newer Lanloom is refused with a clear message.
"""
import base64
import gzip
import hashlib
import io
import json
import logging
import os
import platform
import re
import secrets as _secrets
import sqlite3
import struct
import threading
import time
import zlib
from pathlib import Path
from typing import Callable, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import __version__

log = logging.getLogger("lanloom.backup")

MAGIC = b"LANLOOM-BACKUP\x00"
FILE_VERSION = 1             # layout of the container (magic, header, ciphertext)
CONTENT_FORMAT = 1           # layout of the content; older contents are migrated (MIGRATIONS), newer ones refused
SUFFIX = ".llbackup"
FOLDER = "config-backups"    # inside the data volume (the updater's own copies live in "backups")
UPLOADS = ".uploads"
AUTO_PREFIX = "lanloom-auto-"
PASSPHRASE_MIN = 12
PASSPHRASE_MAX = 1024
KEY_BYTES = 32
WRAP_AAD = b"lanloom-backup-wrap-v1"

# Argon2id: 64 MiB, 3 passes, 1 lane - about half a second on a small single-board computer. A file may carry other
# parameters (a later version may raise them) but only within these limits, so a crafted header cannot exhaust memory.
KDF_DEFAULT = {"m": 65536, "t": 3, "p": 1}
KDF_LIMITS = {"m": (8192, 1048576), "t": (1, 12), "p": (1, 8)}

UPLOAD_MAX = int(os.environ.get("LANLOOM_BACKUP_MAX_MB", "512") or 512) * 1024 * 1024
CONTENT_MAX = 2 * 1024 * 1024 * 1024        # decompressed content, protection against a compression bomb
UPLOAD_TTL = 3600
EVERY_CHOICES = (6, 12, 24, 72, 168)        # hours between scheduled backups
KEEP_MIN, KEEP_MAX = 1, 60
DEFAULT_SCHEDULE = {"enabled": False, "every_hours": 24, "keep": 7, "history": False}
SAFETY_KEEP = 2                             # safety copies of the database taken before a restore

# Tables of the configuration (always) and of the history (optional). Sessions, invitations, notices, known sign-in
# places and the meta table are never part of a backup: they belong to the running instance.
CONFIG_TABLES = ("users", "passkeys", "recovery_codes", "settings", "integrations", "devices", "phys_manual")
HISTORY_TABLES = ("scans", "events", "reports", "report_marks", "paths_hour", "paths_day", "paths_seen")
INSTANCE_TABLES = ("sessions", "invites", "notices", "known_logins")       # emptied on restore (they name old user ids)
# settings that describe this instance at runtime, not the configuration
LOCAL_SETTINGS = frozenset({"fw_sim_state", "fw_jobs", "update_cache", "report_heartbeat", "report_epoch",
                            "report_last_version", "ai_pairing", "ai_usage", "backup_state"})
# vault entries that are never exported (half-finished set-ups, the imported pepper travels separately)
LOCAL_SECRET_PREFIXES = ("totp_pending:",)
PEPPER_SECRET = "auth.recovery_pepper"
KEK_SECRET = "backup.kek"
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


class BackupError(Exception):
    """A refusal with an i18n key (never contains a secret, a path or a passphrase)."""

    def __init__(self, key: str, **vars_):
        super().__init__(key)
        self.key = key
        self.vars = vars_


# ---------------------------------------------------------------------- small helpers
def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text) -> bytes:
    if not isinstance(text, str):
        raise BackupError("backup.err.damaged")
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        raise BackupError("backup.err.damaged")


def version_tuple(text) -> tuple:
    out = []
    for part in str(text or "0").split(".")[:3]:
        m = re.match(r"\d+", part)
        out.append(int(m.group(0)) if m else 0)
    return tuple(out + [0] * (3 - len(out)))


def check_passphrase(passphrase) -> str:
    if not isinstance(passphrase, str) or len(passphrase) < PASSPHRASE_MIN:
        raise BackupError("backup.err.passphrase_short", n=PASSPHRASE_MIN)
    if len(passphrase) > PASSPHRASE_MAX:
        raise BackupError("backup.err.passphrase_long")
    return passphrase


def derive(passphrase: str, salt: bytes, m: int, t: int, p: int) -> bytes:
    from argon2.low_level import Type, hash_secret_raw
    return hash_secret_raw(passphrase.encode("utf-8"), salt, time_cost=t, memory_cost=m, parallelism=p,
                           hash_len=KEY_BYTES, type=Type.ID)


def new_kek(passphrase: str, kdf: Optional[dict] = None) -> dict:
    """{"salt", "m", "t", "p", "key"} - the key-encryption key of one passphrase (kept for scheduled backups)."""
    kdf = dict(kdf or KDF_DEFAULT)
    salt = _secrets.token_bytes(16)
    return {"salt": _b64(salt), "m": kdf["m"], "t": kdf["t"], "p": kdf["p"],
            "key": _b64(derive(check_passphrase(passphrase), salt, kdf["m"], kdf["t"], kdf["p"]))}


def _encode_value(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"$b": _b64(bytes(v))}
    return v


def _decode_value(v):
    if isinstance(v, dict):
        if set(v) == {"$b"}:
            return _unb64(v["$b"])
        raise BackupError("backup.err.damaged")
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    raise BackupError("backup.err.damaged")


# ---------------------------------------------------------------------- container
def seal(content: dict, kek: dict, meta: dict) -> bytes:
    """Encrypt `content` into the file format with the key-encryption key `kek` (see new_kek)."""
    data_key = AESGCM.generate_key(bit_length=256)
    wrap_nonce = _secrets.token_bytes(12)
    wrapped = AESGCM(_unb64(kek["key"])).encrypt(wrap_nonce, data_key, WRAP_AAD)
    nonce = _secrets.token_bytes(12)
    header = {"v": FILE_VERSION, "kdf": {"alg": "argon2id", "m": kek["m"], "t": kek["t"], "p": kek["p"], "salt": kek["salt"]},
              "wrap": {"nonce": _b64(wrap_nonce), "key": _b64(wrapped)}, "nonce": _b64(nonce),
              "meta": dict(meta, zip="gzip")}
    hbytes = json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
    prefix = MAGIC + struct.pack(">BI", FILE_VERSION, len(hbytes)) + hbytes
    raw = json.dumps(content, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    packed = gzip.compress(raw, compresslevel=6, mtime=0)
    return prefix + AESGCM(data_key).encrypt(nonce, packed, prefix)


def read_header(blob: bytes) -> tuple:
    """(header dict, prefix bytes). Refuses files that are not Lanloom backups or come from a newer container."""
    if len(blob) < len(MAGIC) + 5 or not blob.startswith(MAGIC):
        raise BackupError("backup.err.not_backup")
    version, hlen = struct.unpack(">BI", blob[len(MAGIC):len(MAGIC) + 5])
    if version > FILE_VERSION:
        raise BackupError("backup.err.newer_file")
    start = len(MAGIC) + 5
    if hlen <= 0 or hlen > 65536 or start + hlen > len(blob):
        raise BackupError("backup.err.damaged")
    try:
        header = json.loads(blob[start:start + hlen].decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise BackupError("backup.err.damaged")
    if not isinstance(header, dict) or not isinstance(header.get("kdf"), dict) or not isinstance(header.get("wrap"), dict) \
            or not isinstance(header.get("meta"), dict):
        raise BackupError("backup.err.damaged")
    return header, blob[:start + hlen]


def public_meta(header: dict) -> dict:
    """What may be shown before the passphrase is entered (unverified until the content is decrypted)."""
    m = header.get("meta") or {}
    app = str(m.get("app") or "")[:20]
    fmt = m.get("format") if isinstance(m.get("format"), int) else 0
    out = {"app": app, "format": fmt, "created": m.get("created") if isinstance(m.get("created"), (int, float)) else None,
           "history": bool(m.get("history")), "auto": bool(m.get("auto"))}
    out["compat"] = compat(app, fmt)
    return out


def compat(app: str, fmt: int) -> str:
    """"same" | "older" (migrated) | "newer" (refused)."""
    if fmt > CONTENT_FORMAT or version_tuple(app) > version_tuple(__version__):
        return "newer"
    if fmt < CONTENT_FORMAT or version_tuple(app) < version_tuple(__version__):
        return "older"
    return "same"


def open_sealed(blob: bytes, passphrase: str) -> tuple:
    """(header, content) of a backup file. Raises BackupError with not_backup / newer / wrong_passphrase / damaged."""
    header, prefix = read_header(blob)
    meta = public_meta(header)
    if meta["compat"] == "newer":
        raise BackupError("backup.err.newer", version=meta["app"] or "?", current=__version__)
    kdf = header["kdf"]
    params = {}
    for name, (lo, hi) in KDF_LIMITS.items():
        v = kdf.get(name)
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise BackupError("backup.err.damaged")
        params[name] = v
    if kdf.get("alg") != "argon2id":
        raise BackupError("backup.err.damaged")
    salt = _unb64(kdf.get("salt"))
    if not 8 <= len(salt) <= 64:
        raise BackupError("backup.err.damaged")
    if not isinstance(passphrase, str) or not passphrase or len(passphrase) > PASSPHRASE_MAX:
        raise BackupError("backup.err.wrong_passphrase")
    kek = derive(passphrase, salt, params["m"], params["t"], params["p"])
    try:
        data_key = AESGCM(kek).decrypt(_unb64(header["wrap"].get("nonce")), _unb64(header["wrap"].get("key")), WRAP_AAD)
    except (InvalidTag, ValueError):
        raise BackupError("backup.err.wrong_passphrase")
    try:
        packed = AESGCM(data_key).decrypt(_unb64(header.get("nonce")), blob[len(prefix):], prefix)
    except (InvalidTag, ValueError):
        raise BackupError("backup.err.damaged")
    raw = _gunzip(packed)
    try:
        content = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise BackupError("backup.err.damaged")
    if not isinstance(content, dict):
        raise BackupError("backup.err.damaged")
    fmt = content.get("format")
    if not isinstance(fmt, int) or fmt > CONTENT_FORMAT or version_tuple(content.get("app")) > version_tuple(__version__):
        raise BackupError("backup.err.newer", version=str(content.get("app") or "?")[:20], current=__version__)
    return header, migrate(content)


def _gunzip(packed: bytes) -> bytes:
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = io.BytesIO()
    data = packed
    try:
        while data:
            chunk = d.decompress(data, 8 * 1024 * 1024)
            out.write(chunk)
            if out.tell() > CONTENT_MAX:
                raise BackupError("backup.err.too_large")
            data = d.unconsumed_tail
        out.write(d.flush())
    except zlib.error:
        raise BackupError("backup.err.damaged")
    if not d.eof:
        raise BackupError("backup.err.damaged")
    return out.getvalue()


# ---------------------------------------------------------------------- migrations of older contents
def _migrate_rows(content: dict) -> dict:
    """Format-independent repairs of contents written by older versions (columns they did not have yet)."""
    users = (content.get("tables") or {}).get("users") or []
    if users and all(isinstance(u, dict) and "role" not in u for u in users):
        # written before roles existed (before 1.12): the one account there was becomes an administrator with the
        # usual time to set up a second factor - the same rule the database migration applies after an update
        for u in users:
            u["role"] = "admin"
            u.setdefault("mfa_due", time.time() + 48 * 3600)
    return content


# CONTENT_FORMAT n -> function(content) -> content in format n + 1
MIGRATIONS: dict = {}


def migrate(content: dict) -> dict:
    fmt = content.get("format")
    while fmt < CONTENT_FORMAT:
        step = MIGRATIONS.get(fmt)
        if step is None:
            raise BackupError("backup.err.too_old", version=str(content.get("app") or "?")[:20])
        content = step(content)
        fmt = content["format"] = fmt + 1
    return _migrate_rows(content)


# ---------------------------------------------------------------------- the service
def recovery_pepper(vault, derived: bytes) -> bytes:
    """The pepper of the recovery codes: the one carried over by a restore, else the one derived from the instance key."""
    try:
        raw = vault.get(PEPPER_SECRET) if vault is not None else None
    except Exception:            # unreadable entry: fall back, never stop the app
        raw = None
    if raw:
        try:
            value = bytes.fromhex(raw)
            if len(value) == 32:
                return value
        except ValueError:
            pass
    return derived


class Backups:
    """Export, schedule, check and restore. `app` is main.App (store, vault, accounts, factors, audit, core ...)."""

    def __init__(self, app, clock: Callable[[], float] = time.time):
        self.app = app
        self.store = app.store
        self.vault = app.vault
        self.cfg = app.cfg
        self.clock = clock
        self._lock = threading.Lock()           # one export / restore / scheduled run at a time
        self._task = None
        self._loop = None
        self.demo = bool(getattr(app.cfg, "demo", False))

    # ------------------------------------------------------------------ places
    @property
    def folder(self) -> Path:
        return Path(self.cfg.data_dir) / FOLDER

    def _ensure_folder(self) -> Path:
        d = self.folder
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        return d

    @staticmethod
    def _write_private(path: Path, data: bytes) -> None:
        tmp = path.with_name(path.name + ".part")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        os.replace(tmp, path)

    def file_path(self, name: str) -> Path:
        """Path of a stored backup by its file name (only names this module writes; no directories)."""
        if not isinstance(name, str) or not _NAME_RE.match(name) or not name.endswith(SUFFIX) or name.startswith("."):
            raise BackupError("backup.err.missing")
        p = self.folder / name
        if not p.is_file():
            raise BackupError("backup.err.missing")
        return p

    # ------------------------------------------------------------------ schedule settings
    def schedule(self) -> dict:
        raw = self.store.get_setting("backup_cfg", {}) or {}
        s = dict(DEFAULT_SCHEDULE)
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k in s and isinstance(v, type(s[k])) and not (isinstance(v, bool) and not isinstance(s[k], bool)):
                    s[k] = v
        if s["every_hours"] not in EVERY_CHOICES:
            s["every_hours"] = DEFAULT_SCHEDULE["every_hours"]
        s["keep"] = max(KEEP_MIN, min(KEEP_MAX, int(s["keep"])))
        return s

    def key_set(self) -> bool:
        try:
            return bool(self.vault.get(KEK_SECRET))
        except Exception:
            return False

    def update_schedule(self, patch: dict, passphrase: Optional[str] = None) -> dict:
        s = self.schedule()
        if "enabled" in patch:
            s["enabled"] = patch["enabled"] is True
        if "history" in patch:
            s["history"] = patch["history"] is True
        if "every_hours" in patch:
            if patch["every_hours"] not in EVERY_CHOICES:
                raise BackupError("backup.err.invalid")
            s["every_hours"] = patch["every_hours"]
        if "keep" in patch:
            if not isinstance(patch["keep"], int) or isinstance(patch["keep"], bool) or not KEEP_MIN <= patch["keep"] <= KEEP_MAX:
                raise BackupError("backup.err.invalid")
            s["keep"] = patch["keep"]
        if passphrase:
            self.vault.put(KEK_SECRET, json.dumps(new_kek(passphrase)))
        if s["enabled"] and not self.key_set():
            raise BackupError("backup.err.no_key")
        was = self.schedule()["enabled"]
        self.store.set_setting("backup_cfg", s)
        if s["enabled"] and not was:
            self._set_state(enabled_at=self.clock())
        return self.public_state()

    def _state(self) -> dict:
        st = self.store.get_setting("backup_state", {}) or {}
        return st if isinstance(st, dict) else {}

    def _set_state(self, **patch) -> None:
        st = self._state()
        st.update(patch)
        self.store.set_setting("backup_state", st)

    # ------------------------------------------------------------------ listing and sizes
    def files(self) -> list:
        out = []
        try:
            entries = list(self.folder.iterdir())
        except OSError:
            return out
        for p in entries:
            if not p.is_file() or not p.name.endswith(SUFFIX) or p.name.startswith("."):
                continue
            item = {"name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime, "auto": p.name.startswith(AUTO_PREFIX)}
            try:
                with open(p, "rb") as fh:
                    head = fh.read(70000)
                item.update(public_meta(read_header(head)[0]))
            except (OSError, BackupError):
                item["damaged"] = True
            out.append(item)
        out.sort(key=lambda i: i.get("created") or i["mtime"], reverse=True)
        return out

    def _table_bytes(self, table: str) -> tuple:
        cols = self._columns(table)
        if not cols:
            return 0, 0
        expr = "+".join("COALESCE(length(\"%s\"),0)" % c for c in cols)
        row = self.store.one("SELECT COUNT(*) AS n, COALESCE(SUM(%s),0) AS b FROM \"%s\"" % (expr, table))
        return int(row["n"]), int(row["b"]) + 8 * len(cols) * int(row["n"])

    def estimate(self) -> dict:
        conf = sum(self._table_bytes(t)[1] for t in CONFIG_TABLES) + self._table_bytes("secrets")[1] + self._table_bytes("audit")[1]
        hist = sum(self._table_bytes(t)[1] for t in HISTORY_TABLES)
        return {"config_bytes": conf, "history_bytes": hist}

    def public_state(self) -> dict:
        st = self._state()
        last = self.newest()
        return {"schedule": self.schedule(), "key_set": self.key_set(), "files": self.files(), "estimate": self.estimate(),
                "last_run": st.get("last_run"), "last_ok": st.get("last_ok"), "last_error": st.get("last_error"),
                "last_export": st.get("last_export"), "newest": last, "stale": self.stale(), "folder": "/data/" + FOLDER,
                "passphrase_min": PASSPHRASE_MIN, "every_choices": list(EVERY_CHOICES), "keep_range": [KEEP_MIN, KEEP_MAX],
                "next_run": self.next_run()}

    def newest(self) -> Optional[float]:
        times = [f.get("created") or f["mtime"] for f in self.files() if not f.get("damaged")]
        st = self._state()
        if isinstance(st.get("last_export"), (int, float)):
            times.append(st["last_export"])
        return max(times) if times else None

    def stale(self, now: Optional[float] = None) -> bool:
        """The schedule is on and the newest backup is older than twice the interval (at least a day and an hour)."""
        s = self.schedule()
        if not s["enabled"]:
            return False
        now = self.clock() if now is None else now
        newest = self.newest()
        limit = max(2 * s["every_hours"] * 3600, 25 * 3600)
        if newest is None:
            since = self._state().get("enabled_at") or now
            return now - since > limit
        return now - newest > limit

    def next_run(self) -> Optional[float]:
        s = self.schedule()
        if not s["enabled"]:
            return None
        last = self._state().get("last_run")
        return (last + s["every_hours"] * 3600) if isinstance(last, (int, float)) else self.clock()

    # ------------------------------------------------------------------ export
    def _columns(self, table: str) -> list:
        return [r["name"] for r in self.store.query("PRAGMA table_info(\"%s\")" % table)]

    def collect(self, history: bool) -> dict:
        tables = {}
        for table in CONFIG_TABLES + (HISTORY_TABLES if history else ()):
            if not self._columns(table):
                continue
            rows = self.store.query("SELECT * FROM \"%s\"" % table)
            if table == "settings":
                rows = [r for r in rows if r["key"] not in LOCAL_SETTINGS]
            tables[table] = [{k: _encode_value(v) for k, v in r.items()} for r in rows]
        secrets = {}
        for row in self.store.query("SELECT name FROM secrets ORDER BY name"):
            name = row["name"]
            if name == PEPPER_SECRET or name.startswith(LOCAL_SECRET_PREFIXES):
                continue
            try:
                value = self.vault.get(name)
            except Exception:            # an entry this instance cannot read (other key): leave it out, say so
                log.warning("backup: a stored secret could not be read and is left out")
                continue
            if value is not None:
                secrets[name] = value
        audit_rows = self.store.query("SELECT t, actor, actor_id, role, action, target, outcome, ip, agent, detail FROM audit ORDER BY id")
        chain = self.app.audit.verify() if getattr(self.app, "audit", None) else {"ok": True, "count": 0}
        return {"format": CONTENT_FORMAT, "app": __version__, "created": self.clock(), "history": bool(history),
                "source": {"arch": platform.machine(), "schema": self._schema()},
                "tables": tables, "secrets": secrets, "audit": audit_rows, "audit_chain": {"ok": bool(chain["ok"]), "count": chain["count"]},
                "keys": {"recovery_pepper": self.app.factors.pepper.hex()}}

    def _schema(self) -> int:
        row = self.store.one("SELECT value FROM meta WHERE key='schema'")
        return int(row["value"]) if row else 0

    def _meta(self, history: bool, auto: bool = False) -> dict:
        return {"app": __version__, "format": CONTENT_FORMAT, "created": round(self.clock(), 3), "history": bool(history),
                "auto": bool(auto)}

    def export(self, passphrase: str, history: bool = False) -> tuple:
        """(file name, bytes) of a new backup encrypted with `passphrase`."""
        check_passphrase(passphrase)
        with self._lock:
            content = self.collect(history)
            blob = seal(content, new_kek(passphrase), self._meta(history))
        self._set_state(last_export=self.clock())
        return self.file_name(), blob

    def file_name(self, auto: bool = False) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(self.clock()))
        return "%s%s-%s%s" % (AUTO_PREFIX if auto else "lanloom-", stamp, __version__, SUFFIX)

    # ------------------------------------------------------------------ scheduled backups
    def run_now(self, reason: str = "schedule") -> dict:
        """Write one backup into the folder with the stored key; prune to `keep`. Never raises for the loop."""
        now = self.clock()
        s = self.schedule()
        raw = None
        try:
            raw = self.vault.get(KEK_SECRET)
        except Exception:
            raw = None
        if not raw:
            self._set_state(last_run=now, last_error="backup.err.no_key")
            raise BackupError("backup.err.no_key")
        try:
            kek = json.loads(raw)
            with self._lock:
                content = self.collect(s["history"])
                blob = seal(content, kek, self._meta(s["history"], auto=True))
                d = self._ensure_folder()
                name = self.file_name(auto=True)
                path = d / name
                n = 1
                while path.exists():
                    n += 1
                    path = d / name.replace(SUFFIX, "-%d%s" % (n, SUFFIX))
                self._write_private(path, blob)
                removed = self.prune(s["keep"])
        except BackupError as exc:
            self._set_state(last_run=now, last_error=exc.key)
            raise
        except OSError as exc:
            key = "backup.err.disk_full" if getattr(exc, "errno", 0) == 28 else "backup.err.write"
            self._set_state(last_run=now, last_error=key)
            log.warning("scheduled backup failed: %s", exc.strerror or type(exc).__name__)
            raise BackupError(key)
        self._set_state(last_run=now, last_ok=now, last_error=None)
        log.info("backup written (%s, %d bytes, %d old removed)", reason, len(blob), removed)
        return {"name": path.name, "size": len(blob), "removed": removed}

    def prune(self, keep: int) -> int:
        autos = sorted((p for p in self.folder.iterdir() if p.is_file() and p.name.startswith(AUTO_PREFIX) and p.name.endswith(SUFFIX)),
                       key=lambda p: (p.name[:len(AUTO_PREFIX) + 15], p.stat().st_mtime), reverse=True)   # time stamp in the name first
        n = 0
        for p in autos[keep:]:
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
        return n

    def delete_file(self, name: str) -> bool:
        p = self.file_path(name)
        p.unlink()
        return True

    def due(self, now: Optional[float] = None) -> bool:
        nxt = self.next_run()
        return nxt is not None and (self.clock() if now is None else now) >= nxt

    async def loop(self, every: float = 300.0) -> None:
        import asyncio
        while True:
            try:
                if self.due():
                    await asyncio.to_thread(self.run_now)
            except BackupError:
                pass                      # recorded in backup_state; the nightly report shows it
            except Exception:             # the loop must survive anything
                log.exception("backup loop")
            await asyncio.sleep(every)

    def start(self) -> None:
        import asyncio
        self._loop = asyncio.get_running_loop()
        if getattr(self.cfg, "probe", False) or self._task is not None:
            return
        self._task = asyncio.create_task(self.loop())

    def _on_loop(self, fn) -> None:
        """Run `fn` on the app's event loop (sources are asyncio tasks); restores run in a worker thread."""
        import asyncio
        loop = getattr(self, "_loop", None)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and loop.is_running() and running is not loop:
            async def call():
                fn()
            asyncio.run_coroutine_threadsafe(call(), loop).result(30)
        else:
            fn()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except BaseException:
                pass
            self._task = None

    # ------------------------------------------------------------------ uploads (a file to check and restore)
    def _uploads(self) -> Path:
        d = self._ensure_folder() / UPLOADS
        d.mkdir(exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        now = self.clock()
        for p in d.iterdir():
            try:
                if now - p.stat().st_mtime > UPLOAD_TTL:
                    p.unlink()
            except OSError:
                pass
        return d

    async def receive(self, chunks, slot: Optional[str] = None) -> dict:
        """Store an uploaded file (async iterator of bytes). Returns {"upload", "size", "meta"}."""
        d = self._uploads()
        upload = slot or _secrets.token_hex(12)
        path = d / (upload + ".part")
        size = 0
        head = b""
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > UPLOAD_MAX:
                        raise BackupError("backup.err.too_large")
                    if len(head) < 70000:
                        head += chunk[:70000 - len(head)]
                    fh.write(chunk)
            meta = public_meta(read_header(head)[0])
        except BaseException:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        os.replace(path, d / (upload + ".bin"))
        return {"upload": upload, "size": size, "meta": meta}

    def _upload_path(self, upload: str) -> Path:
        if not isinstance(upload, str) or not re.fullmatch(r"[a-z0-9]{6,40}", upload):
            raise BackupError("backup.err.upload_gone")
        p = self._uploads() / (upload + ".bin")
        if not p.is_file():
            raise BackupError("backup.err.upload_gone")
        return p

    def drop_upload(self, upload: str) -> None:
        try:
            self._upload_path(upload).unlink()
        except (BackupError, OSError):
            pass

    def blob_of(self, ref: dict) -> bytes:
        """The bytes of an uploaded file ({"upload": id}) or of a stored backup ({"file": name})."""
        if ref.get("upload"):
            return self._upload_path(str(ref["upload"])).read_bytes()
        if ref.get("file"):
            return self.file_path(str(ref["file"])).read_bytes()
        raise BackupError("backup.err.upload_gone")

    # ------------------------------------------------------------------ check and preview
    def _validated(self, content: dict) -> dict:
        tables = content.get("tables")
        if not isinstance(tables, dict) or not isinstance(content.get("secrets", {}), dict):
            raise BackupError("backup.err.damaged")
        clean = {}
        for table, rows in tables.items():
            if table not in CONFIG_TABLES + HISTORY_TABLES:
                continue                    # unknown table of the same format: ignored
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise BackupError("backup.err.damaged")
            clean[table] = [{str(k): _decode_value(v) for k, v in r.items()} for r in rows]
        if not clean.get("users"):
            raise BackupError("backup.err.no_users")
        secrets = {}
        for name, value in (content.get("secrets") or {}).items():
            if not isinstance(name, str) or not isinstance(value, str) or len(name) > 200:
                raise BackupError("backup.err.damaged")
            secrets[name] = value
        audit_rows = content.get("audit") or []
        if not isinstance(audit_rows, list) or any(not isinstance(r, dict) for r in audit_rows):
            raise BackupError("backup.err.damaged")
        pepper = (content.get("keys") or {}).get("recovery_pepper")
        if pepper is not None:
            try:
                if len(bytes.fromhex(pepper)) != 32:
                    raise ValueError
            except (TypeError, ValueError):
                raise BackupError("backup.err.damaged")
        return {"tables": clean, "secrets": secrets, "audit": audit_rows, "pepper": pepper,
                "audit_chain": content.get("audit_chain") or {}, "history": bool(content.get("history")),
                "app": str(content.get("app") or "")[:20], "created": content.get("created"),
                "arch": str((content.get("source") or {}).get("arch") or "")[:20], "format": content.get("format")}

    def check(self, blob: bytes, passphrase: str) -> tuple:
        """(header, validated content). Raises BackupError."""
        header, content = open_sealed(blob, passphrase)
        return header, self._validated(content)

    def _own_audit_fresh(self) -> bool:
        """The instance has no audit history of its own yet (only failed set-up attempts before the first account)."""
        return self.store.user_count() == 0

    def preview(self, data: dict) -> dict:
        """What a restore of `data` (from check) would replace, keep and end."""
        t = data["tables"]
        users_now = self.store.query("SELECT username, role FROM users WHERE source != 'sample' ORDER BY username COLLATE NOCASE")
        users_new = sorted(({"username": u.get("username"), "role": u.get("role")} for u in t.get("users", [])
                            if u.get("source") != "sample"), key=lambda u: str(u["username"]).lower())
        integ_now = [r["name"] for r in self.store.query("SELECT name FROM integrations ORDER BY id")]
        integ_new = [str(r.get("name")) for r in t.get("integrations", [])]
        manual = lambda rows: sum(1 for r in rows if r.get("user_title") or r.get("user_kind") or r.get("hidden"))
        drafts = lambda rows: sum(1 for r in rows if str(r.get("key", "")).startswith("fw_draft:"))
        settings_now = [r for r in self.store.query("SELECT key FROM settings") if r["key"] not in LOCAL_SETTINGS]
        dev_now = self.store.query("SELECT user_title, user_kind, hidden FROM devices")
        sessions_now = self.store.one("SELECT COUNT(*) AS n FROM sessions")["n"]
        audit_now = self.store.one("SELECT COUNT(*) AS n FROM audit")["n"]
        fresh = self._own_audit_fresh()
        hist_now = sum(self._table_bytes(x)[0] for x in HISTORY_TABLES)
        hist_new = sum(len(t.get(x, [])) for x in HISTORY_TABLES)
        return {
            "app": data["app"], "created": data["created"], "arch": data["arch"], "this_arch": platform.machine(),
            "compat": compat(data["app"], data["format"] or 0), "current": __version__,
            "users": {"now": users_now, "new": users_new},
            "integrations": {"now": integ_now, "new": integ_new},
            "devices": {"now": len(dev_now), "new": len(t.get("devices", [])), "manual_now": manual(dev_now),
                        "manual_new": manual(t.get("devices", []))},
            "connections": {"now": self.store.one("SELECT COUNT(*) AS n FROM phys_manual")["n"], "new": len(t.get("phys_manual", []))},
            "drafts": {"now": drafts(settings_now), "new": drafts(t.get("settings", []))},
            "settings": {"now": len(settings_now), "new": len(t.get("settings", []))},
            "secrets": {"now": self.store.one("SELECT COUNT(*) AS n FROM secrets")["n"], "new": len(data["secrets"])},
            "history": {"included": data["history"], "now": hist_now, "new": hist_new},
            "audit": {"now": audit_now, "new": len(data["audit"]), "imported": fresh, "source_ok": bool(data["audit_chain"].get("ok", True))},
            "sessions_end": sessions_now,
            "passkeys": len(t.get("passkeys", [])),
        }

    # ------------------------------------------------------------------ restore
    def safety_copy(self) -> Optional[str]:
        """Copy of the current database before a restore (data volume, 0600); the newest SAFETY_KEEP are kept."""
        if self.store.path == ":memory:":
            return None
        d = self._ensure_folder()
        name = "before-restore-%s.db" % time.strftime("%Y%m%d-%H%M%S", time.gmtime(self.clock()))
        target = d / name
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        dst = sqlite3.connect(str(target))
        try:
            with self.store._lock:
                self.store._db.backup(dst)
        finally:
            dst.close()
        olds = sorted((p for p in d.iterdir() if p.name.startswith("before-restore-") and p.name.endswith(".db")),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for p in olds[SAFETY_KEEP:]:
            try:
                p.unlink()
            except OSError:
                pass
        return name

    def _insert(self, tx, table: str, rows: list, cols_now: list) -> int:
        n = 0
        for r in rows:
            cols = [c for c in r if c in cols_now]
            if not cols:
                continue
            tx.execute("INSERT OR REPLACE INTO \"%s\"(%s) VALUES(%s)" % (table, ",".join('"%s"' % c for c in cols),
                                                                      ",".join("?" * len(cols))),
                       tuple(r[c] for c in cols))
            n += 1
        return n

    def restore(self, data: dict, actor: Optional[dict] = None, ip: str = "", agent: str = "") -> dict:
        """Replace the configuration with `data` (from check). Ends every session. Returns a summary."""
        app = self.app
        if not self._lock.acquire(timeout=30):
            raise BackupError("backup.err.busy")
        try:
            fresh = self._own_audit_fresh()
            safety = self.safety_copy()
            core = getattr(app, "core", None)
            if core is not None:
                try:
                    core.cancel_scan()
                except Exception:
                    pass
            t = data["tables"]
            own_audit = self.store.query("SELECT t, actor, actor_id, role, action, target, outcome, ip, agent, detail FROM audit ORDER BY id") if fresh else []
            counts = {}
            with self.store.transaction() as tx:
                for table in INSTANCE_TABLES:
                    tx.execute("DELETE FROM \"%s\"" % table)
                for table in CONFIG_TABLES + (HISTORY_TABLES if data["history"] else ()):
                    cols_now = self._columns(table)
                    if not cols_now:
                        continue
                    if table == "settings":
                        keep = ",".join("'%s'" % k for k in sorted(LOCAL_SETTINGS))
                        tx.execute("DELETE FROM settings WHERE key NOT IN (%s)" % keep)
                        rows = [r for r in t.get("settings", []) if r.get("key") not in LOCAL_SETTINGS]
                    else:
                        tx.execute("DELETE FROM \"%s\"" % table)
                        rows = t.get(table, [])
                    counts[table] = self._insert(tx, table, rows, cols_now)
                tx.execute("DELETE FROM secrets")
                for name, value in data["secrets"].items():
                    if name == PEPPER_SECRET or name.startswith(LOCAL_SECRET_PREFIXES):
                        continue
                    self.vault.put(name, value)
                if data["pepper"]:
                    self.vault.put(PEPPER_SECRET, data["pepper"])
                tx.execute("DELETE FROM sessions")
            if data["pepper"]:
                app.factors.pepper = bytes.fromhex(data["pepper"])
            imported = 0
            if fresh and getattr(app, "audit", None) is not None:
                imported = app.audit.rewrite(list(data["audit"]) + own_audit)
            self._reload()
            summary = {"users": counts.get("users", 0), "integrations": counts.get("integrations", 0),
                       "devices": counts.get("devices", 0), "connections": counts.get("phys_manual", 0),
                       "secrets": len(data["secrets"]), "history": data["history"], "audit_imported": imported,
                       "safety_copy": bool(safety), "from_version": data["app"], "from_arch": data["arch"]}
            if getattr(app, "audit", None) is not None:
                try:
                    app.audit.record("backup.restored", actor, target=data["app"], ip=ip, agent=agent,
                                     actor_name=None if actor else "setup",
                                     detail=dict(summary, source_chain_ok=bool(data["audit_chain"].get("ok", True))))
                except Exception:
                    log.exception("audit entry for the restore failed")
            for admin in self.store.query("SELECT id FROM users WHERE role='admin' AND disabled=0"):
                try:
                    app.notices.add(admin["id"], "restored", {"version": data["app"], "ip": ip})
                except Exception:
                    pass
            self._set_state(last_restore=self.clock())
            log.info("configuration restored from a backup of version %s (%d users, %d connections, %d devices)",
                     data["app"], summary["users"], summary["integrations"], summary["devices"])
            return summary
        finally:
            self._lock.release()

    def _reload(self) -> None:
        """Make the running app use the restored data: sources, caches, pending sign-ins, set-up code."""
        app = self.app
        from . import auth
        app.tickets = auth.Tickets()
        with app.passkeys._lock:
            app.passkeys._pending.clear()
        try:
            app.setup_code.ensure()
        except Exception:
            pass
        core = getattr(app, "core", None)
        if core is not None and hasattr(core, "reload_after_restore"):
            self._on_loop(core.reload_after_restore)
        fw = getattr(app, "firewall", None)
        if fw is not None:
            fw._sim = None if getattr(fw, "_runner", None) is None else fw._sim


def sha256_hex(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()
