"""Credentials, kept apart: read-only accounts in the web app, write accounts only in the executor.

* **Web app** (read halves): settings and secrets are entered in the integration dialog; secrets are stored
  encrypted with ``secret.key`` (see ``lanloom/crypto.py``), never shown again, never logged. Before an account is
  used, the read half reports its scope; an account that can write is refused when that is checkable
  (``refuse_write_capable``) - a read-only account is the rule, not a recommendation.
* **Executor** (write halves): its own options file, mode 0600, owned by the executor's user, a regular file and
  never reached through a symbolic link (``load_write_options``). Tokens are preferred over passwords. The app never
  sees this file. Superuser principals are refused and owner-level roles produce a warning (``vet_write_account``).

Secrets travel inside ``Secret``: its ``repr()``/``str()`` never show the value, it cannot be serialised by
accident (``json.dumps`` raises), and only ``reveal()`` hands the text to the one send path that needs it.
"""
import json
import os
import re
import stat

MAX_OPTIONS_BYTES = 64 * 1024
SECRET_NAME = re.compile(r"(pass(word)?|secret|token|key)$", re.I)
SUPERUSER_PRINCIPALS = re.compile(r"\Aroot(@[a-z0-9._-]+)?\Z", re.I)     # the system superuser of an appliance
OWNER_ROLES = ("owner", "superadmin", "super_admin", "administrator@all")


class CredentialError(ValueError):
    pass


class Secret:
    __slots__ = ("_value",)

    def __init__(self, value):
        object.__setattr__(self, "_value", "" if value is None else str(value))

    def reveal(self) -> str:
        return self._value

    def __bool__(self):
        return bool(self._value)

    def __repr__(self):
        return "Secret(***)"

    __str__ = __repr__

    def __format__(self, spec):
        return "Secret(***)"

    def __setattr__(self, name, value):
        raise AttributeError("Secret is read-only")

    def __reduce__(self):
        raise TypeError("a Secret is never serialised")

    def __eq__(self, other):
        return isinstance(other, Secret) and other._value == self._value

    __hash__ = None


def load_write_options(path, *, secret_fields=(), uid: int = None) -> dict:
    """Read the executor's options file safely. Secret-looking and declared fields come back as ``Secret``."""
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise CredentialError("options file cannot be opened (missing, or a symbolic link): %s" % type(exc).__name__)
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise CredentialError("options file is not a regular file")
        if st.st_mode & 0o077:
            raise CredentialError("options file must be mode 0600 (it is %o)" % (st.st_mode & 0o777))
        if st.st_uid != (os.getuid() if uid is None else uid):
            raise CredentialError("options file must belong to the executor's user")
        if st.st_size > MAX_OPTIONS_BYTES:
            raise CredentialError("options file is too large")
        raw = fh.read(MAX_OPTIONS_BYTES + 1)
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise CredentialError("options file is not valid JSON") from None
    if not isinstance(data, dict) or not all(isinstance(k, str) for k in data):
        raise CredentialError("options file must hold one JSON object")
    declared = set(secret_fields)
    return {k: (Secret(v) if (k in declared or SECRET_NAME.search(k)) and isinstance(v, str) else v) for k, v in data.items()}


def vet_write_account(info: dict) -> tuple:
    """(ok, warnings) for the account a write half logged in with. Superuser principals are refused outright."""
    info = info or {}
    principal = str(info.get("principal") or "")
    if SUPERUSER_PRINCIPALS.match(principal):
        return False, ["superuser"]
    warnings = []
    if str(info.get("role") or "").lower() in OWNER_ROLES:
        warnings.append("owner_role")
    if info.get("expires") in (None, 0) and info.get("token"):
        warnings.append("no_expiry")
    return True, warnings


def refuse_write_capable(scope: str) -> bool:
    """True when the read half found that its account can write - the app then refuses to use it."""
    return scope == "can_write"
