"""Built-in integrations (device-family adapters) and their registry.

Every adapter is built in: it lives in ``lanloom/integrations/<family>/`` and is listed in ``BUILTIN`` below.
There is no way to load anything else - no entry points, no plugin folder, no module path from the environment.
Ideas and adapter requests are welcome as issues (see CONTRIBUTING.md).

An adapter has two halves that share one translation module (``<family>/model.py``, vendor <-> neutral):

* the **read half** (``<family>/read.py``) runs in the web app with a read-only account. It never writes: it has
  exactly one send path with a static allow-list (see ``transport.py``), a static test checks every read half;
* the **write half** (``<family>/write.py``) runs only in the executor service, only when real writes are armed,
  with its own credentials the app never sees (see ``credentials.py``). It is a ``ControllerAdapter``.

The web app may import manifests and read halves; it never imports a write half (``load_write`` refuses unless the
process is the executor, and a static test checks the app's imports).
"""
import importlib
import re
from dataclasses import dataclass, field

from . import capabilities as caps_mod

INTEGRATION_API = 1           # contract level of the adapter halves; app and executor show a mismatch
FAMILY_RE = re.compile(r"\A[a-z][a-z0-9_]{1,31}\Z")
_EXECUTOR = {"on": False}     # set by lanloom.executor.main() only


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class IntegrationManifest:
    """What the registry knows about one family. ``read``/``write`` are ``module:Class`` inside the family package."""
    family: str
    label: str                                   # shown in the family selection list
    capabilities: dict
    api_level: int = INTEGRATION_API
    read: str = ""
    write: str = ""
    docs: str = ""                               # docs/integrations/<family>.md, empty for internal families
    selectable: bool = True                      # offered in the integration dialog
    config_fields: tuple = ()                    # plain settings of the read half
    secret_fields: tuple = ()                    # read-only credentials (stored encrypted in the app)
    write_secret_fields: tuple = field(default=())   # credentials of the write half (executor options file only)

    def validate(self) -> "IntegrationManifest":
        if not FAMILY_RE.match(self.family or ""):
            raise ManifestError("family id must match %s" % FAMILY_RE.pattern)
        if not isinstance(self.label, str) or not self.label.strip() or len(self.label) > 64:
            raise ManifestError("label")
        if self.api_level != INTEGRATION_API:
            raise ManifestError("api level %r, this release speaks %d" % (self.api_level, INTEGRATION_API))
        prefix = "lanloom.integrations.%s." % self.family
        for half, spec in (("read", self.read), ("write", self.write)):
            if not spec:
                continue
            mod, _, cls = spec.partition(":")
            if not mod.startswith(prefix) or not cls.isidentifier() or mod.rsplit(".", 1)[-1] != half:
                raise ManifestError("%s half must be %s%s:<Class>" % (half, prefix, half))
        if self.docs and self.docs != "docs/integrations/%s.md" % self.family:
            raise ManifestError("docs")
        if set(self.secret_fields) & set(self.write_secret_fields):
            raise ManifestError("read and write credentials must be different fields")
        caps_mod.validate(self.capabilities, self.family)
        return self

    def public(self) -> dict:
        return {"family": self.family, "label": self.label, "api_level": self.api_level, "docs": self.docs or None,
                "read_half": bool(self.read), "write_half": bool(self.write),
                "config_fields": list(self.config_fields), "secret_fields": list(self.secret_fields),
                "capabilities": caps_mod.public(caps_mod.validate(self.capabilities, self.family))}


def _builtin() -> tuple:
    from .simulation import MANIFEST as simulation
    return (simulation,)


BUILTIN = tuple(m.validate() for m in _builtin())
_BY_FAMILY = {m.family: m for m in BUILTIN}
if len(_BY_FAMILY) != len(BUILTIN):
    raise ManifestError("duplicate family id")


def families(selectable_only: bool = False) -> list:
    return [m for m in BUILTIN if m.selectable or not selectable_only]


def get(family: str) -> IntegrationManifest:
    m = _BY_FAMILY.get(family)
    if m is None:
        raise KeyError(family)
    return m


def _load(spec: str):
    mod, _, cls = spec.partition(":")
    return getattr(importlib.import_module(mod), cls)      # spec comes from a validated built-in manifest only


def load_read(family: str, **options):
    """Instance of the family's read half (web app)."""
    m = get(family)
    if not m.read:
        raise KeyError("%s has no read half" % family)
    return _load(m.read)(**options)


def mark_executor_process() -> None:
    """Called once by ``python -m lanloom.executor``; the web app never calls it."""
    _EXECUTOR["on"] = True


def load_write(family: str, **options):
    """Instance of the family's write half. Only inside the executor process."""
    if not _EXECUTOR["on"]:
        raise PermissionError("write halves load only in the executor service")
    m = get(family)
    if not m.write:
        raise KeyError("%s has no write half" % family)
    from ..firewall.adapter import ControllerAdapter
    inst = _load(m.write)(**options)
    if not isinstance(inst, ControllerAdapter):
        raise TypeError("write half must subclass ControllerAdapter")
    return inst


def public_list() -> list:
    """The family selection list for the integration dialog (selectable families only)."""
    return [m.public() for m in families(selectable_only=True)]
