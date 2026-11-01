"""Read-only live data sources.

* ``HostNet()``  - TCP flow bytes (netlink socket diagnostics) and interface counters of the host, every second;
* ``Hub(url, token)`` - home-automation hub over its WebSocket API (strict message allow-list);
* ``Mqtt(host, ...)`` - MQTT broker, subscribe-only, message/byte counts per topic root (never payloads);
* ``Snmp(host, ...)`` - router/switch over SNMP v2c/v3 (Get/GetNext/GetBulk only), every 10 seconds.

Each source: ``async run(emit)`` (loops until cancelled, reconnects with backoff 1 s -> 60 s, emits status events)
and ``async test() -> (ok, reason_key, details)``. See ``_common`` for the shared contract.
"""
from ._common import Backoff, SourceError
from .hostnet import HostNet
from .hub import Hub
from .mqtt import Mqtt
from .snmp import Snmp

SOURCES = {"host": HostNet, "hub": Hub, "mqtt": Mqtt, "snmp": Snmp}

# Reason keys a source can emit (for the i18n files).
REASON_KEYS = (
    "source.hostnet.ok", "source.hostnet.no_bytes", "source.hostnet.unsupported", "source.hostnet.unavailable",
    "source.hostnet.error",
    "source.hub.ok", "source.hub.config", "source.hub.auth_failed", "source.hub.forbidden",
    "source.hub.request_failed", "source.hub.protocol", "source.hub.closed", "source.hub.timeout",
    "source.hub.refused", "source.hub.unreachable", "source.hub.tls", "source.hub.error",
    "source.mqtt.ok", "source.mqtt.config", "source.mqtt.auth_failed", "source.mqtt.refused",
    "source.mqtt.subscribe_denied", "source.mqtt.protocol", "source.mqtt.closed", "source.mqtt.timeout",
    "source.mqtt.unreachable", "source.mqtt.tls", "source.mqtt.forbidden", "source.mqtt.error",
    "source.snmp.ok", "source.snmp.config", "source.snmp.auth_failed", "source.snmp.protocol",
    "source.snmp.timeout", "source.snmp.unreachable", "source.snmp.refused", "source.snmp.error",
)

__all__ = ["Backoff", "SourceError", "HostNet", "Hub", "Mqtt", "Snmp", "SOURCES", "REASON_KEYS"]
