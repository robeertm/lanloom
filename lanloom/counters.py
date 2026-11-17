"""Counter reconciliation: an hourly self-check that the same traffic is counted once - not twice, not never.

Two independent meters that see the same bytes are compared once per finished hour:

    uplink_flows   reference: the router's internet uplink counter (SNMP of the gateway)
                   compared:  bytes booked from NetFlow/IPFIX of the allowed exporters
                   judged when NetFlow is switched on and the uplink moved at least 50 MB in that hour
    host_flows     reference: this host's interface counters (loopback, bridges and virtual pairs left out)
                   compared:  this host's TCP flows to LAN devices and to the internet
                   only "too much" is judged (TCP flows never contain all of an interface's traffic; more than the
                   interface carried means a flow was counted twice)

factor = compared / reference. Between 0.8 and 1.25 is plausible. Above: counted twice (for example two exporters
reporting the same flows, or a sampling rate applied twice); below: traffic not counted (an exporter missing, a VLAN
not exported). Findings show in the source view and in the nightly report; nothing is ever changed automatically.

Only byte sums per hour are kept in memory and the last 48 results in the settings table; no addresses.
"""
import time
from typing import Callable, Optional

LOW, HIGH = 0.8, 1.25
MIN_UPLINK_BYTES = 50_000_000
MIN_HOST_BYTES = 20_000_000
KEEP = 48
METERS = ("wan", "netflow", "host_if", "host_flow")


def judge(ref: int, val: int, lower: bool = True) -> tuple:
    """(factor, state) for one pair; state ok | double | missing."""
    factor = val / ref if ref > 0 else None
    if factor is None:
        return None, "ok"
    if factor > HIGH:
        return factor, "double"
    if lower and factor < LOW:
        return factor, "missing"
    return factor, "ok"


class CounterCheck:
    def __init__(self, core, clock: Callable[[], float] = time.time):
        self.core = core
        self.store = core.store
        self.clock = clock
        self.hour: Optional[int] = None
        self.acc = dict.fromkeys(METERS, 0)
        self._nf_last: Optional[int] = None

    def feed(self, meter: str, nbytes, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        self._roll(now)
        self.acc[meter] = self.acc.get(meter, 0) + max(0, int(nbytes or 0))

    def tick(self, now: float) -> None:
        """Once a second from the live loop: NetFlow bytes come from the collector's running total."""
        try:
            total = int(self.core.netflow.stats().get("bytes") or 0)
        except Exception:
            total = None
        if total is not None:
            if self._nf_last is not None and total >= self._nf_last:
                self.feed("netflow", total - self._nf_last, now)
            self._nf_last = total
        self._roll(now)

    def _roll(self, now: float) -> None:
        hour = int(now // 3600)
        if self.hour is None:
            self.hour = hour
            return
        if hour == self.hour:
            return
        if hour == self.hour + 1:
            self.evaluate(self.hour, dict(self.acc))
        # a gap of more than an hour (sleep, clock jump) is not judged: the sums do not belong to one hour
        self.hour = hour
        self.acc = dict.fromkeys(METERS, 0)

    def evaluate(self, hour: int, acc: dict) -> list:
        out = []
        nf_on = bool(self.store.get_setting("netflow_enabled", False))
        if nf_on and acc.get("wan", 0) >= MIN_UPLINK_BYTES:
            f, state = judge(acc["wan"], acc.get("netflow", 0))
            out.append({"hour": hour, "pair": "uplink_flows", "ref": acc["wan"], "val": acc.get("netflow", 0),
                        "factor": round(f, 3) if f is not None else None, "state": state})
        if acc.get("host_if", 0) >= MIN_HOST_BYTES:
            f, state = judge(acc["host_if"], acc.get("host_flow", 0), lower=False)
            out.append({"hour": hour, "pair": "host_flows", "ref": acc["host_if"], "val": acc.get("host_flow", 0),
                        "factor": round(f, 3) if f is not None else None, "state": state})
        if out:
            self.save(out)
        return out

    def save(self, results: list) -> None:
        old = [r for r in (self.store.get_setting("counter_check", []) or []) if isinstance(r, dict)]
        self.store.set_setting("counter_check", (old + results)[-KEEP:])

    def results(self) -> list:
        return [r for r in (self.store.get_setting("counter_check", []) or []) if isinstance(r, dict)]

    def findings(self, since: Optional[float] = None, until: Optional[float] = None) -> list:
        """Results that are not ok, optionally only for hours that end inside (since, until]."""
        out = []
        for r in self.results():
            if r.get("state") == "ok":
                continue
            end = (int(r.get("hour") or 0) + 1) * 3600
            if since is not None and end <= since:
                continue
            if until is not None and end > until:
                continue
            out.append(r)
        return out

    def view(self) -> dict:
        res = self.results()
        nf_on = bool(self.store.get_setting("netflow_enabled", False))
        latest = {}
        for r in res:
            latest[r["pair"]] = r
        return {"low": LOW, "high": HIGH, "results": res[-24:], "latest": latest,
                "findings": [r for r in res[-24:] if r.get("state") != "ok"], "netflow": nf_on,
                "current": {"hour": self.hour, **{k: int(v) for k, v in self.acc.items()}}}
