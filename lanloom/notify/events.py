"""The register of notification events: what can be announced, how serious it is by default, and whether it waits.

Every event has a translation key for its name and its message, a default severity and a few flags:

* ``hold``     - the condition must still be true after the hold time (several watch rounds) before an immediate
                 message goes out; a condition that clears itself earlier is only a flap and never sent.
* ``recovery`` - the event has an all-clear (the device is back, the line is up again). The all-clear is only sent to
                 people who were told about the outage.
* ``personal`` - only the person concerned may receive it (security notices of an account).
* ``available``- is the source there at all (a later feature brings it)? Unavailable events are not offered as rules.

Later features add their own events with ``register(Event(...))`` and announce them with ``core.notify.emit(...)``.
"""
from collections import namedtuple
from typing import Callable, Optional

SEVERITIES = ("info", "warning", "critical")
RANK = {s: i for i, s in enumerate(SEVERITIES)}
MODES = ("off", "nightly", "hourly", "immediate")
DEFAULT_MODE = "nightly"          # calm by default: everything goes into the nightly report unless somebody asks for more

Event = namedtuple("Event", "id group order title severity hold recovery personal available")


def make(eid: str, group: str, order: int, severity: str = "warning", hold: bool = False, recovery: bool = False,
         personal: bool = False, available: Optional[Callable] = None) -> Event:
    if severity not in SEVERITIES:
        raise ValueError("unknown severity %r" % severity)
    return Event(eid, group, order, "notify.ev.%s" % eid, severity, hold, recovery, personal, available or (lambda core: True))


REGISTRY: dict = {}


def register(ev: Event) -> Event:
    REGISTRY[ev.id] = ev
    return ev


def get(eid: str) -> Optional[Event]:
    return REGISTRY.get(eid)


def ordered() -> list:
    return sorted(REGISTRY.values(), key=lambda e: (e.order, e.id))


def _has_integrations(core) -> bool:
    try:
        return bool(core.integrations())
    except Exception:
        return False


def _has_backups(core) -> bool:
    return getattr(core, "backups", None) is not None


def _later(core) -> bool:          # the source comes with a later feature; the event is registered so rules can wait for it
    return bool(getattr(core, "incidents", None))


# ---------------------------------------------------------------- built-in events
register(make("dev_new_protected", "devices", 10, "critical"))           # new device in a protected network
register(make("dev_new", "devices", 11, "info"))                         # new device anywhere else
register(make("dev_offline", "devices", 12, "warning", hold=True, recovery=True))
register(make("wan_down", "internet", 20, "critical", hold=True, recovery=True))
register(make("src_fail", "connections", 30, "warning", hold=True, recovery=True, available=_has_integrations))
register(make("integ_auth", "connections", 31, "warning", hold=True, recovery=True, available=_has_integrations))
register(make("cert_expiring", "connections", 32, "warning", available=_has_integrations))
register(make("backup_old", "lanloom", 40, "warning", available=_has_backups))
register(make("update_available", "lanloom", 41, "info"))
register(make("update_failed", "lanloom", 43, "warning"))                # update or rollback did not go through
register(make("security_notice", "lanloom", 42, "warning", personal=True))
register(make("incident", "incidents", 50, "warning", available=_later))
register(make("vulnerability", "incidents", 51, "warning", available=lambda core: bool(getattr(core, "vulns", None))))
