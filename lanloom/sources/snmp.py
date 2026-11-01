"""Read-only SNMP source (router/switch): system group + interface counters every 10 s.

``SnmpClient`` is a pure standard-library reader:

* SNMP v2c (community) and SNMP v3 with the user-based security model (RFC 3414):
  noAuthNoPriv, authNoPriv, authPriv;
* authentication: HMAC-MD5-96 ("md5"), HMAC-SHA-96 ("sha", RFC 3414) and HMAC-SHA-256-192 ("sha256", RFC 7860);
  privacy: AES-128-CFB ("aes", RFC 3826). DES is not supported;
* key localisation per RFC 3414 A.2, cached per engine ID; engine discovery and one time-window resync;
* only GetRequest, GetNextRequest and GetBulkRequest PDUs can be built (``_READ_PDUS``); nothing can be written;
* replies are validated strictly (version, message ID, request ID, community / engine ID / user, HMAC, BER limits);
  foreign or broken datagrams are dropped until the deadline.

Secrets (community, passphrases, keys) never appear in exception texts, ``repr()`` or logs. Passphrases are not
kept after key derivation; the community has to be kept (it is sent with every request) but only privately.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import socket
import threading
import time

from . import _aes as aes
from . import _ber as ber
from ._common import Backoff, SourceError, StatusTracker, clip, emit_event, error_key, now, short_test

MODULE = "snmp"
SOURCE = "snmp"

MAX_MSG_SIZE = 65507
_MAX_WALK_ROUNDS = 10000
_LIMIT_31 = 0x7FFFFFFF

PDU_GET = 0xA0
PDU_GETNEXT = 0xA1
PDU_RESPONSE = 0xA2
PDU_GETBULK = 0xA5
PDU_REPORT = 0xA8
_READ_PDUS = frozenset({PDU_GET, PDU_GETNEXT, PDU_GETBULK})

_FLAG_AUTH = 0x01
_FLAG_PRIV = 0x02
_FLAG_REPORTABLE = 0x04
_USM = 3
_V2C = 1
_V3 = 3

# usmStats report OIDs (RFC 3414)
_OID_UNSUPPORTED_LEVEL = (1, 3, 6, 1, 6, 3, 15, 1, 1, 1, 0)
_OID_NOT_IN_TIME = (1, 3, 6, 1, 6, 3, 15, 1, 1, 2, 0)
_OID_UNKNOWN_USER = (1, 3, 6, 1, 6, 3, 15, 1, 1, 3, 0)
_OID_UNKNOWN_ENGINE = (1, 3, 6, 1, 6, 3, 15, 1, 1, 4, 0)
_OID_WRONG_DIGEST = (1, 3, 6, 1, 6, 3, 15, 1, 1, 5, 0)
_OID_DECRYPTION = (1, 3, 6, 1, 6, 3, 15, 1, 1, 6, 0)

_REPORTS = {
    _OID_UNSUPPORTED_LEVEL: ("source.snmp.auth_failed", "agent does not offer this security level"),
    _OID_NOT_IN_TIME: ("source.snmp.protocol", "time window does not match even after resync"),
    _OID_UNKNOWN_USER: ("source.snmp.auth_failed", "user unknown to the agent"),
    _OID_UNKNOWN_ENGINE: ("source.snmp.protocol", "engine ID unknown"),
    _OID_WRONG_DIGEST: ("source.snmp.auth_failed", "authentication rejected"),
    _OID_DECRYPTION: ("source.snmp.auth_failed", "decryption rejected"),
}

_ERROR_STATUS = ("noError", "tooBig", "noSuchName", "badValue", "readOnly", "genErr", "noAccess", "wrongType",
                 "wrongLength", "wrongEncoding", "wrongValue", "noCreation", "inconsistentValue",
                 "resourceUnavailable", "commitFailed", "undoFailed", "authorizationError", "notWritable",
                 "inconsistentName")

# auth protocol -> (hash constructor, truncated MAC length)
_AUTH = {"md5": (hashlib.md5, 12), "sha": (hashlib.sha1, 12), "sha256": (hashlib.sha256, 24)}
_AUTH_ALIASES = {"md5": "md5", "sha": "sha", "sha1": "sha", "sha-1": "sha", "sha256": "sha256", "sha-256": "sha256"}
_PRIV_ALIASES = {"aes": "aes", "aes128": "aes", "aes-128": "aes"}

# MIB-2 objects used by the source
OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
OID_SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_SPEED = "1.3.6.1.2.1.2.2.1.5"
OID_IF_OPER = "1.3.6.1.2.1.2.2.1.8"
OID_IF_IN = "1.3.6.1.2.1.2.2.1.10"
OID_IF_OUT = "1.3.6.1.2.1.2.2.1.16"
OID_IFX_NAME = "1.3.6.1.2.1.31.1.1.1.1"
OID_IFX_HC_IN = "1.3.6.1.2.1.31.1.1.1.6"
OID_IFX_HC_OUT = "1.3.6.1.2.1.31.1.1.1.10"
OID_IFX_HIGH_SPEED = "1.3.6.1.2.1.31.1.1.1.15"

OPER_STATUS = {1: "up", 2: "down", 3: "testing", 4: "unknown", 5: "dormant", 6: "notPresent", 7: "lowerLayerDown"}
MAX_IFACES = 1024


class SnmpError(SourceError):
    """SNMP failure. The text never contains the community, passphrases or keys."""

    def __init__(self, text: str, key: str = "source.snmp.protocol"):
        super().__init__(key, text)


class _TimeResync(Exception):
    """Internal: authenticated notInTimeWindows report, boots/time taken over."""


class _EngineChanged(Exception):
    """Internal: the agent no longer knows our engine ID."""


# ================================================================== message codec
def _usm_params(engine_id, boots, etime, user, auth, priv):
    return ber.tlv(ber.T_SEQUENCE, ber.enc_octets(engine_id) + ber.enc_int(boots) + ber.enc_int(etime)
                   + ber.enc_octets(user) + ber.enc_octets(auth) + ber.enc_octets(priv))


def encode_v3(msg_id, flags, engine_id, boots, etime, user, auth, priv, msg_data):
    """msg_data: ready TLV (scoped PDU as SEQUENCE, or encrypted as OCTET STRING)."""
    header = ber.tlv(ber.T_SEQUENCE, ber.enc_int(msg_id) + ber.enc_int(MAX_MSG_SIZE) + ber.enc_octets(bytes((flags,)))
                     + ber.enc_int(_USM))
    return ber.tlv(ber.T_SEQUENCE, ber.enc_int(_V3) + header
                   + ber.enc_octets(_usm_params(engine_id, boots, etime, user, auth, priv)) + msg_data)


def encode_v2c(community: bytes, pdu: bytes) -> bytes:
    return ber.tlv(ber.T_SEQUENCE, ber.enc_int(_V2C) + ber.enc_octets(community) + pdu)


def _int_field(node, minimum=0, maximum=_LIMIT_31):
    tag, w = node
    if tag != ber.T_INTEGER:
        raise ber.BerError("INTEGER expected")
    n = ber.int_from(w)
    if not minimum <= n <= maximum:
        raise ber.BerError("value out of range")
    return n


def decode_v3(data) -> dict:
    """Raw datagram -> dict, including the position of the auth parameters (for HMAC verification)."""
    data = bytes(data)
    tag, start, stop = ber.read_header(data, 0, len(data))
    if tag != ber.T_SEQUENCE or stop != len(data):
        raise ber.BerError("outer SEQUENCE invalid")
    version_n, p = ber.read_node(data, start, stop)
    header_n, p = ber.read_node(data, p, stop)
    if header_n[0] != ber.T_SEQUENCE or len(header_n[1]) != 4:
        raise ber.BerError("msgGlobalData invalid")
    msg_id_n, size_n, flags_n, model_n = header_n[1]
    if flags_n[0] != ber.T_OCTETS or len(flags_n[1]) != 1:
        raise ber.BerError("msgFlags invalid")
    tag, sv, sb = ber.read_header(data, p, stop)
    if tag != ber.T_OCTETS:
        raise ber.BerError("msgSecurityParameters invalid")
    p = sb
    tag, uv, ub = ber.read_header(data, sv, sb)
    if tag != ber.T_SEQUENCE or ub != sb:
        raise ber.BerError("USM parameters invalid")
    fields = []
    q = uv
    for expected in (ber.T_OCTETS, ber.T_INTEGER, ber.T_INTEGER, ber.T_OCTETS, ber.T_OCTETS, ber.T_OCTETS):
        tag, fv, fb = ber.read_header(data, q, ub)
        if tag != expected:
            raise ber.BerError("USM field invalid")
        fields.append((fv, fb))
        q = fb
    if q != ub:
        raise ber.BerError("USM trailing bytes")
    msg_data, p = ber.read_node(data, p, stop)
    if p != stop:
        raise ber.BerError("trailing bytes after msgData")
    if msg_data[0] not in (ber.T_SEQUENCE, ber.T_OCTETS):
        raise ber.BerError("msgData invalid")

    def raw(i):
        return data[fields[i][0]:fields[i][1]]

    if len(raw(0)) > 32 or len(raw(3)) > 32:
        raise ber.BerError("engine ID / user too long")
    return {
        "version": _int_field(version_n),
        "msg_id": _int_field(msg_id_n),
        "max_size": _int_field(size_n),
        "flags": flags_n[1][0],
        "model": _int_field(model_n, 1),
        "engine_id": raw(0),
        "boots": _int_field((ber.T_INTEGER, raw(1))),
        "time": _int_field((ber.T_INTEGER, raw(2))),
        "user": raw(3),
        "auth": raw(4),
        "auth_span": fields[4],
        "priv": raw(5),
        "data_tag": msg_data[0],
        "data": msg_data if msg_data[0] == ber.T_SEQUENCE else msg_data[1],
    }


def decode_v2c(data) -> tuple[int, bytes, tuple]:
    """Raw v2c datagram -> (version, community, pdu_node)."""
    node = ber.decode(bytes(data))
    tag, children = node
    if tag != ber.T_SEQUENCE or not isinstance(children, list) or len(children) != 3 \
            or children[1][0] != ber.T_OCTETS:
        raise ber.BerError("v2c message invalid")
    return _int_field(children[0]), children[1][1], children[2]


def build_pdu(tag, request_id, oids, non_repeaters=0, max_repetitions=0) -> bytes:
    """Read requests only. Every other PDU type is refused."""
    if tag not in _READ_PDUS:
        raise SnmpError("PDU type not allowed (read-only)")
    vbs = b"".join(ber.tlv(ber.T_SEQUENCE, ber.enc_oid(o) + ber.tlv(ber.T_NULL, b"")) for o in oids)
    if tag != PDU_GETBULK:
        non_repeaters = max_repetitions = 0
    return ber.tlv(tag, ber.enc_int(request_id) + ber.enc_int(non_repeaters) + ber.enc_int(max_repetitions)
                   + ber.tlv(ber.T_SEQUENCE, vbs))


def scoped_pdu(engine_id, pdu) -> bytes:
    return ber.tlv(ber.T_SEQUENCE, ber.enc_octets(engine_id) + ber.enc_octets(b"") + pdu)


def parse_pdu(pdu_node):
    """PDU node -> (pdu_type, request_id, error_status, error_index, [(oid_tuple, value_node)])."""
    ptag, pk = pdu_node
    if not ptag & 0x20 or not isinstance(pk, list) or len(pk) != 4 or pk[3][0] != ber.T_SEQUENCE \
            or not isinstance(pk[3][1], list):
        raise ber.BerError("PDU invalid")
    request_id = _int_field(pk[0], -_LIMIT_31 - 1)
    status = _int_field(pk[1])
    index = _int_field(pk[2])
    vbs = []
    for vb in pk[3][1]:
        if vb[0] != ber.T_SEQUENCE or not isinstance(vb[1], list) or len(vb[1]) != 2 or vb[1][0][0] != ber.T_OID:
            raise ber.BerError("varbind invalid")
        vbs.append((ber.oid_from(vb[1][0][1]), vb[1][1]))
    return ptag, request_id, status, index, vbs


def parse_scoped(scoped):
    tag, children = scoped
    if tag != ber.T_SEQUENCE or not isinstance(children, list) or len(children) != 3 \
            or children[0][0] != ber.T_OCTETS or children[1][0] != ber.T_OCTETS:
        raise ber.BerError("ScopedPDU invalid")
    return parse_pdu(children[2])


# ================================================================== USM keys and HMAC
def password_to_key(password, proto: str = "sha") -> bytes:
    """RFC 3414 A.2, first part: Ku = H(passphrase repeated to 1 MiB)."""
    pw = password.encode("utf-8") if isinstance(password, str) else bytes(password or b"")
    if not pw:
        raise SnmpError("passphrase missing", "source.snmp.config")
    repeated = pw * (1048576 // len(pw) + 1)
    return _AUTH[proto][0](repeated[:1048576]).digest()


def localize_key(ku: bytes, engine_id: bytes, proto: str = "sha") -> bytes:
    """RFC 3414 A.2, second part: Kul = H(Ku || engineID || Ku)."""
    return _AUTH[proto][0](ku + engine_id + ku).digest()


def mac(key: bytes, data: bytes, proto: str = "sha") -> bytes:
    fn, length = _AUTH[proto]
    return hmac.new(key, data, fn).digest()[:length]


def verify_mac(key: bytes, data: bytes, start: int, stop: int, proto: str = "sha") -> bool:
    length = _AUTH[proto][1]
    if stop - start != length:
        return False
    zeroed = data[:start] + bytes(length) + data[stop:]
    return hmac.compare_digest(mac(key, zeroed, proto), data[start:stop])


# ================================================================== client
class SnmpClient:
    """Blocking read-only SNMP client for one agent (v2c or v3). Only Get/GetNext/GetBulk."""

    def __init__(self, host, port=161, version="2c", community=None, user=None, auth_key=None, priv_key=None,
                 auth_proto="sha", priv_proto="aes", timeout=2.0, retries=2):
        self.host = str(host or "").strip()
        if not self.host or len(self.host) > 253 or any(c.isspace() for c in self.host):
            raise SnmpError("host missing or invalid", "source.snmp.config")
        self.port = int(port)
        if not 0 < self.port < 65536:
            raise SnmpError("port invalid", "source.snmp.config")
        self.version = str(version).lower().lstrip("v")
        if self.version not in ("2c", "3"):
            raise SnmpError("version must be 2c or 3", "source.snmp.config")
        self.timeout = max(0.1, float(timeout))
        self.retries = max(1, int(retries))
        self._sock = None
        self._lock = threading.RLock()
        self.dropped = 0
        self._wrong_macs = 0
        self._community = b""
        self.level = None
        if self.version == "2c":
            c = community.encode("utf-8") if isinstance(community, str) else bytes(community or b"")
            if not c or len(c) > 255:
                raise SnmpError("community missing or too long", "source.snmp.config")
            self._community = c
            return
        self.user = str(user or "")
        self._user_b = self.user.encode("utf-8")
        if not self._user_b or len(self._user_b) > 32:
            raise SnmpError("user missing or too long", "source.snmp.config")
        ap = _AUTH_ALIASES.get(str(auth_proto or "sha").lower())
        pp = _PRIV_ALIASES.get(str(priv_proto or "aes").lower())
        if ap is None:
            raise SnmpError("authentication protocol not supported", "source.snmp.config")
        if pp is None:
            raise SnmpError("privacy protocol not supported", "source.snmp.config")
        self.auth_proto, self.priv_proto = ap, pp
        if priv_key and not auth_key:
            raise SnmpError("privacy requires authentication", "source.snmp.config")
        self.level = "authPriv" if priv_key else ("authNoPriv" if auth_key else "noAuthNoPriv")
        # only derived keys are kept, never the passphrases
        self._ku_auth = password_to_key(auth_key, ap) if auth_key else None
        self._ku_priv = password_to_key(priv_key, ap) if priv_key else None
        self._keys = {}              # engine ID -> (auth key, AES round keys | None)
        self._engine_id = None
        self._boots = 0
        self._time = 0
        self._time_base = 0.0
        self._salt = int.from_bytes(os.urandom(8), "big")

    def __repr__(self):
        return "SnmpClient(host=%r, port=%d, version=%r)" % (self.host, self.port, self.version)

    __str__ = __repr__

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ------------------------------------------------------------ public
    def get(self, oids) -> dict:
        """{oid: value} for the requested OIDs (GetRequest)."""
        if isinstance(oids, str):
            oids = [oids]
        oid_list = [self._oid(o) for o in oids]
        if not oid_list:
            return {}
        with self._lock:
            return {ber.oid_text(o): self._value(w) for o, w in self._request(PDU_GET, oid_list)}

    def walk(self, base_oid, repetitions=25) -> list:
        """[(oid, value)] below base_oid via GetBulk. Stops when leaving the subtree, at endOfMibView or when the
        agent does not answer in increasing order."""
        base = self._oid(base_oid)
        n = len(base)
        repetitions = max(1, min(int(repetitions), 1000))
        out = []
        last = base
        for _ in range(_MAX_WALK_ROUNDS):
            with self._lock:
                vbs = self._request(PDU_GETBULK, [last], repetitions)
            if not vbs:
                return out
            for oid, node in vbs:
                if node[0] in (ber.T_END_OF_MIB, ber.T_NO_SUCH_OBJECT, ber.T_NO_SUCH_INSTANCE) \
                        or oid[:n] != base or oid <= last:
                    return out
                out.append((ber.oid_text(oid), self._value(node)))
                last = oid
        raise SnmpError("walk aborted after too many rounds")

    def reset(self):
        """Close the socket and forget the engine (next request rediscovers). Keys stay."""
        self.close()
        if self.version == "3":
            self._engine_id = None

    def close(self):
        s, self._sock = self._sock, None
        if s is not None:
            try:
                s.close()
            except OSError:
                pass

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _oid(o):
        try:
            return ber.oid_tuple(o)
        except ber.BerError:
            raise SnmpError("invalid OID", "source.snmp.config") from None

    @staticmethod
    def _value(node):
        try:
            return ber.value_of(node)
        except ber.BerError:
            raise SnmpError("invalid value in reply") from None

    def _socket(self):
        if self._sock is None:
            s = None
            try:
                fam, typ, proto, _, addr = socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_DGRAM)[0]
                s = socket.socket(fam, typ, proto)
                s.connect(addr)     # only replies from this peer reach us
            except OSError as exc:
                if s is not None:
                    s.close()
                raise SnmpError("cannot reach agent (%s)" % type(exc).__name__, "source.snmp.unreachable") from None
            self._sock = s
        return self._sock

    def _send(self, message):
        try:
            self._socket().send(message)
        except OSError as exc:
            raise SnmpError("send failed (%s)" % type(exc).__name__, "source.snmp.unreachable") from None

    def _receive(self, deadline):
        """One datagram or None at the deadline."""
        while True:
            rest = deadline - time.monotonic()
            if rest <= 0:
                return None
            s = self._sock
            if s is None:
                return None
            s.settimeout(rest)
            try:
                return s.recv(65535)
            except socket.timeout:
                return None
            except OSError:      # e.g. ICMP port unreachable - keep waiting
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    @staticmethod
    def _random_id():
        return int.from_bytes(os.urandom(4), "big") & _LIMIT_31

    def _no_reply(self):
        extra = ""
        if self._wrong_macs:
            extra = " (%d reply/replies with wrong MAC dropped)" % self._wrong_macs
            return SnmpError("no valid reply" + extra, "source.snmp.auth_failed")
        return SnmpError("no valid reply after %d attempt(s)" % self.retries, "source.snmp.timeout")

    def _request(self, tag, oids, max_repetitions=0):
        if self.version == "2c":
            return self._exchange_v2c(tag, oids, max_repetitions)
        if self._engine_id is None:
            self._discover()
        request_id = self._random_id()
        resynced = False
        while True:
            try:
                return self._exchange_v3(tag, request_id, oids, max_repetitions)
            except _TimeResync:
                if resynced:
                    raise self._report_error(_OID_NOT_IN_TIME) from None
                resynced = True
            except _EngineChanged:
                if resynced:
                    raise self._report_error(_OID_UNKNOWN_ENGINE) from None
                resynced = True
                self._engine_id = None
                self._discover()

    # ------------------------------------------------------------ v2c
    def _exchange_v2c(self, tag, oids, max_repetitions):
        request_id = self._random_id()
        message = encode_v2c(self._community, build_pdu(tag, request_id, oids, 0, max_repetitions))
        self._wrong_macs = 0
        for _ in range(self.retries):
            self._send(message)
            deadline = time.monotonic() + self.timeout
            while True:
                data = self._receive(deadline)
                if data is None:
                    break
                try:
                    version, community, pdu = decode_v2c(data)
                    ptag, rid, status, index, vbs = parse_pdu(pdu)
                except ber.BerError:
                    self.dropped += 1
                    continue
                if version != _V2C or not hmac.compare_digest(community, self._community) \
                        or ptag != PDU_RESPONSE or rid != request_id:
                    self.dropped += 1
                    continue
                if status:
                    raise self._status_error(status, index)
                return vbs
        raise self._no_reply()

    def _status_error(self, status, index):
        name = _ERROR_STATUS[status] if status < len(_ERROR_STATUS) else str(status)
        key = "source.snmp.auth_failed" if name in ("noAccess", "authorizationError") else "source.snmp.protocol"
        return SnmpError("agent reports error %s (index %d)" % (name, index), key)

    # ------------------------------------------------------------ v3
    def _salt_next(self):
        self._salt = (self._salt + 1) & 0xFFFFFFFFFFFFFFFF
        return self._salt.to_bytes(8, "big")

    def _engine_time(self):
        return min(_LIMIT_31, self._time + int(time.monotonic() - self._time_base))

    def _set_time(self, boots, etime, force):
        # RFC 3414 3.2 step 7b: only forward, except for the authenticated time-window report
        if force or boots > self._boots or (boots == self._boots and etime > self._engine_time()):
            self._boots = boots
            self._time = etime
            self._time_base = time.monotonic()

    def _keys_for(self, engine_id):
        if engine_id not in self._keys:
            k_auth = localize_key(self._ku_auth, engine_id, self.auth_proto) if self._ku_auth else None
            rk_priv = None
            if self._ku_priv:
                rk_priv = aes.expand_key(localize_key(self._ku_priv, engine_id, self.auth_proto)[:16])
            self._keys[engine_id] = (k_auth, rk_priv)
        return self._keys[engine_id]

    def _report_error(self, oid):
        if oid is None:
            return SnmpError("empty report")
        key, text = _REPORTS.get(oid, ("source.snmp.protocol", "report %s" % ber.oid_text(oid)))
        return SnmpError("agent report: " + text, key)

    def _flags(self):
        f = _FLAG_REPORTABLE
        if self._ku_auth:
            f |= _FLAG_AUTH
        if self._ku_priv:
            f |= _FLAG_PRIV
        return f

    def _discover(self):
        """Unauthenticated request -> report usmStatsUnknownEngineIDs -> engine ID / boots / time."""
        for _ in range(self.retries):
            msg_id = self._random_id()
            pdu = build_pdu(PDU_GET, self._random_id(), [])
            self._send(encode_v3(msg_id, _FLAG_REPORTABLE, b"", 0, 0, b"", b"", b"", scoped_pdu(b"", pdu)))
            deadline = time.monotonic() + self.timeout
            while True:
                data = self._receive(deadline)
                if data is None:
                    break
                try:
                    n = decode_v3(data)
                    if n["msg_id"] != msg_id or n["version"] != _V3 or n["model"] != _USM \
                            or n["data_tag"] != ber.T_SEQUENCE:
                        raise ber.BerError("foreign")
                    ptag, _, _, _, vbs = parse_scoped(n["data"])
                    if ptag != PDU_REPORT:
                        raise ber.BerError("not a report")
                except ber.BerError:
                    self.dropped += 1
                    continue
                oid = vbs[0][0] if vbs else None
                if oid != _OID_UNKNOWN_ENGINE:
                    raise self._report_error(oid)
                if not 1 <= len(n["engine_id"]) <= 32:
                    raise SnmpError("engine ID invalid")
                self._engine_id = n["engine_id"]
                self._set_time(n["boots"], n["time"], True)
                self._keys_for(self._engine_id)
                return
        raise SnmpError("no answer to engine discovery after %d attempt(s)" % self.retries, "source.snmp.timeout")

    def _exchange_v3(self, tag, request_id, oids, max_repetitions):
        engine = self._engine_id
        k_auth, rk_priv = self._keys_for(engine)
        plain = scoped_pdu(engine, build_pdu(tag, request_id, oids, 0, max_repetitions))
        flags = self._flags()
        mac_len = _AUTH[self.auth_proto][1]
        sent = set()
        self._wrong_macs = 0
        for _ in range(self.retries):
            msg_id = self._random_id()
            sent.add(msg_id)
            boots, etime = self._boots, self._engine_time()
            if rk_priv is not None:
                salt = self._salt_next()
                body = ber.enc_octets(aes.cfb(rk_priv, boots.to_bytes(4, "big") + etime.to_bytes(4, "big") + salt,
                                              plain, False))
            else:
                salt, body = b"", plain
            if k_auth is not None:
                blank = encode_v3(msg_id, flags, engine, boots, etime, self._user_b, bytes(mac_len), salt, body)
                sig = mac(k_auth, blank, self.auth_proto)
            else:
                sig = b""
            self._send(encode_v3(msg_id, flags, engine, boots, etime, self._user_b, sig, salt, body))
            deadline = time.monotonic() + self.timeout
            while True:
                data = self._receive(deadline)
                if data is None:
                    break
                vbs = self._check_v3(data, sent, request_id, k_auth, rk_priv)
                if vbs is not None:
                    return vbs
                self.dropped += 1
        raise self._no_reply()

    def _check_v3(self, data, sent, request_id, k_auth, rk_priv):
        """Varbinds for a valid reply, None for a dropped datagram; reports raise."""
        try:
            n = decode_v3(data)
        except ber.BerError:
            return None
        if n["version"] != _V3 or n["model"] != _USM or n["msg_id"] not in sent:
            return None
        flags = n["flags"]
        authentic = False
        if flags & _FLAG_AUTH:
            if k_auth is None or n["engine_id"] != self._engine_id or n["user"] != self._user_b:
                return None
            if not verify_mac(k_auth, bytes(data), *n["auth_span"], proto=self.auth_proto):
                self._wrong_macs += 1
                return None
            authentic = True
        try:
            if flags & _FLAG_PRIV:
                if not authentic or rk_priv is None or n["data_tag"] != ber.T_OCTETS or len(n["priv"]) != 8:
                    return None
                iv = n["boots"].to_bytes(4, "big") + n["time"].to_bytes(4, "big") + n["priv"]
                clear = aes.cfb(rk_priv, iv, n["data"], True)
                scoped, _ = ber.read_node(clear, 0, len(clear))
            else:
                if n["data_tag"] != ber.T_SEQUENCE:
                    return None
                scoped = n["data"]
            ptag, rid, status, index, vbs = parse_scoped(scoped)
        except (ber.BerError, aes.AesError):
            return None
        if authentic:
            self._set_time(n["boots"], n["time"], False)
        if ptag == PDU_RESPONSE:
            if rid != request_id:
                return None
            # the reply must be protected at least as well as our request
            if k_auth is not None and not authentic:
                return None
            if rk_priv is not None and not flags & _FLAG_PRIV:
                return None
            if k_auth is None and n["engine_id"] != self._engine_id:
                return None
            if status:
                raise self._status_error(status, index)
            return vbs
        if ptag == PDU_REPORT:
            if rid not in (request_id, 0):
                return None
            oid = vbs[0][0] if vbs else None
            if oid == _OID_NOT_IN_TIME:
                if k_auth is not None and not authentic:
                    return None     # only an authenticated report may move our clock
                self._set_time(n["boots"], n["time"], True)
                raise _TimeResync()
            if oid == _OID_UNKNOWN_ENGINE:
                raise _EngineChanged()
            raise self._report_error(oid)
        return None


# ================================================================== counters and polling
class CounterTracker:
    """Octet counter deltas per (interface, direction). 64-bit HC counters never wrap in practice, a decrease means
    a reset; 32-bit counters wrap once at 2^32 unless the implied rate is impossible for the link speed."""

    def __init__(self):
        self.last = {}           # (index, dir) -> (value, bits, mono)

    def reset(self):
        self.last = {}

    def delta(self, index, direction, value, bits, speed_bps=None, mono=None):
        mono = time.monotonic() if mono is None else mono
        if value is None:
            self.last.pop((index, direction), None)
            return None
        prev = self.last.get((index, direction))
        self.last[(index, direction)] = (value, bits, mono)
        if prev is None or prev[1] != bits:
            return None
        old = prev[0]
        if value >= old:
            return value - old
        if bits == 64:
            return None
        d = value + (1 << 32) - old
        elapsed = max(1.0, mono - prev[2])
        if speed_bps and d * 8 > speed_bps * elapsed * 1.5:
            return None          # counter reset, not a wrap
        return d

    def forget_missing(self, present):
        for k in [k for k in self.last if k[0] not in present]:
            del self.last[k]


def _index_map(rows, base):
    prefix = base + "."
    out = {}
    for oid, value in rows:
        if oid.startswith(prefix):
            rest = oid[len(prefix):]
            if rest.isdigit():
                out[int(rest)] = value
    return out


def read_system(client: SnmpClient) -> dict:
    v = client.get([OID_SYS_NAME, OID_SYS_DESCR, OID_SYS_UPTIME])
    up = v.get(OID_SYS_UPTIME)
    return {"name": clip(v.get(OID_SYS_NAME)), "descr": clip(v.get(OID_SYS_DESCR)),
            "uptime": round(up / 100.0, 2) if isinstance(up, int) else None}


def read_interfaces(client: SnmpClient) -> list[dict]:
    """Raw interface rows: index, name, oper, speed (bit/s), in/out counters with their width."""
    descr = _index_map(client.walk(OID_IF_DESCR), OID_IF_DESCR)
    names = _index_map(client.walk(OID_IFX_NAME), OID_IFX_NAME)
    oper = _index_map(client.walk(OID_IF_OPER), OID_IF_OPER)
    high = _index_map(client.walk(OID_IFX_HIGH_SPEED), OID_IFX_HIGH_SPEED)
    hc_in = _index_map(client.walk(OID_IFX_HC_IN), OID_IFX_HC_IN)
    hc_out = _index_map(client.walk(OID_IFX_HC_OUT), OID_IFX_HC_OUT)
    indices = sorted(set(descr) | set(names))[:MAX_IFACES]
    need_32 = any(i not in hc_in or i not in hc_out for i in indices)
    need_speed = any(not isinstance(high.get(i), int) or high.get(i) == 0 for i in indices)
    lo_in = _index_map(client.walk(OID_IF_IN), OID_IF_IN) if need_32 else {}
    lo_out = _index_map(client.walk(OID_IF_OUT), OID_IF_OUT) if need_32 else {}
    speed = _index_map(client.walk(OID_IF_SPEED), OID_IF_SPEED) if need_speed else {}
    rows = []
    for i in indices:
        hs = high.get(i)
        if isinstance(hs, int) and hs > 0:
            bps = hs * 1_000_000
        else:
            s = speed.get(i)
            bps = s if isinstance(s, int) else None
        if isinstance(hc_in.get(i), int) and isinstance(hc_out.get(i), int):
            cin, cout, bits = hc_in[i], hc_out[i], 64
        else:
            cin = lo_in.get(i) if isinstance(lo_in.get(i), int) else None
            cout = lo_out.get(i) if isinstance(lo_out.get(i), int) else None
            bits = 32
        o = oper.get(i)
        rows.append({"index": i, "name": clip(names.get(i) or descr.get(i) or str(i), 64),
                     "oper": OPER_STATUS.get(o, "unknown") if isinstance(o, int) else "unknown",
                     "speed": bps, "c_in": cin, "c_out": cout, "bits": bits})
    return rows


class Snmp:
    """Live source: polls one SNMP agent every ``interval`` seconds (default 10)."""

    name = SOURCE

    def __init__(self, host, version="2c", community=None, user=None, auth_key=None, priv_key=None,
                 auth_proto="sha", priv_proto="aes", port=161, interval=10.0, timeout=2.0, retries=2):
        # the client validates the configuration (SnmpError with a config key) and keeps only derived secrets
        self._client = SnmpClient(host, port=port, version=version, community=community, user=user,
                                  auth_key=auth_key, priv_key=priv_key, auth_proto=auth_proto,
                                  priv_proto=priv_proto, timeout=timeout, retries=retries)
        self.host = self._client.host
        self.interval = max(1.0, float(interval))
        self.counters = CounterTracker()
        self._last_uptime = None

    def __repr__(self):
        return "Snmp(host=%r, version=%r)" % (self.host, self._client.version)

    __str__ = __repr__

    def poll(self, client: SnmpClient) -> dict:
        """One blocking poll -> snmp frame (with octet deltas)."""
        sysinfo = read_system(client)
        up = sysinfo.get("uptime")
        if up is not None and self._last_uptime is not None and up < self._last_uptime:
            self.counters.reset()          # agent restarted: counters started over
        self._last_uptime = up
        rows = read_interfaces(client)
        mono = time.monotonic()
        ifaces = []
        for r in rows:
            d_in = self.counters.delta(r["index"], "in", r["c_in"], r["bits"], r["speed"], mono)
            d_out = self.counters.delta(r["index"], "out", r["c_out"], r["bits"], r["speed"], mono)
            ifaces.append({"index": r["index"], "name": r["name"], "oper": r["oper"], "speed": r["speed"],
                           "in": d_in, "out": d_out})
        self.counters.forget_missing({r["index"] for r in rows})
        return {"type": "snmp", "source": SOURCE, "host": self.host, "t": now(), "sys": sysinfo, "ifaces": ifaces}

    async def run(self, emit) -> None:
        status = StatusTracker(SOURCE)
        backoff = Backoff()
        while True:
            client = self._client
            try:
                good_since = None
                next_tick = time.monotonic()
                while True:
                    frame = await asyncio.to_thread(self.poll, client)
                    await emit_event(emit, frame)
                    await status.set(emit, True, "source.snmp.ok")
                    good_since = good_since or time.monotonic()
                    if time.monotonic() - good_since >= 30:
                        backoff.reset()
                    next_tick += self.interval
                    if next_tick < time.monotonic():
                        next_tick = time.monotonic() + self.interval
                    await asyncio.sleep(max(0.0, next_tick - time.monotonic()))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await status.set(emit, False, error_key(MODULE, exc))
            finally:
                client.reset()
            self.counters.reset()
            self._last_uptime = None
            await asyncio.sleep(backoff.next())

    async def test(self):
        def probe():
            c = self._client
            try:
                sysinfo = read_system(c)
                n = len(_index_map(c.walk(OID_IF_DESCR), OID_IF_DESCR))
            finally:
                c.reset()
            return {"name": sysinfo["name"], "descr": sysinfo["descr"], "ifaces": n}
        timeout = self._client.timeout * self._client.retries * 4 + 5
        return await short_test(MODULE, asyncio.to_thread(probe), timeout)
