"""Counter reconciliation: an hourly self-check that the same traffic is counted once - not twice, not never.

Two independent meters that see the same bytes are compared once per finished hour:

    uplink_flows   reference: the router's internet uplink counter (SNMP of the gateway)
                   compared:  bytes booked from NetFlow/IPFIX of the allowed exporters
                   judged when NetFlow is switched on and the uplink moved at least 50 MB in that hour
    host_flows     reference: this host's interface counters (loopback, bridges and virtual pairs left out)
                   compared:  this host's TCP flows to LAN devices and to the internet
                   only "too much" is judged (TCP flows never contain all of an interface's traffic; more than the
                   interface carried means a flow was counted twice)

factor = compared / reference. Between 0.8 and 1.25 is plausible. Above: counted twice (for example the same traffic
on two interfaces, or a sampling rate applied twice); below: traffic not counted (an exporter missing, a VLAN not
exported). Findings show in the source view and in the nightly report; nothing is ever changed automatically.

The uplink pair is judged on the rolling sum of the last 24 hours, not on one hour: flow bytes are booked when the
record arrives, and many routers export a flow only when its connection ends (no active timeout). Hours of traffic
then land in the hour the connection ended - 0 % while it is open, several hundred % when it ends - although the day
adds up. The hourly ratios stay visible; only the day sum raises a finding. The byte sums of every hour of that window
are kept (hour, uplink bytes, flow bytes), also of quiet hours that are not judged on their own.

Only byte sums per hour are kept in memory, the last 64 results and the 24 hourly sums in the settings table; no
addresses.
"""
import time
from typing import Callable, Optional

LOW, HIGH = 0.8, 1.25
MIN_UPLINK_BYTES = 50_000_000
MIN_HOST_BYTES = 20_000_000
KEEP = 64                                   # a full day of both pairs plus a margin
METERS = ("wan", "netflow", "host_if", "host_flow")
WINDOW_H = 24                               # the uplink pair is judged on the sum of this many hours
MIN_WINDOW_BYTES = 3 * MIN_UPLINK_BYTES     # less uplink traffic in the window is too little to judge
DAY_PAIRS = ("uplink_flows",)               # pairs judged on the window instead of the hour
WINDOW_KEY = "counter_window"


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


def judge_window(entries, lower: bool = True) -> tuple:
    """(factor, state, hours, ref, val) over hourly sums [hour, ref, val]; too little traffic is ok, the factor is kept."""
    ref = sum(int(e[1]) for e in entries)
    val = sum(int(e[2]) for e in entries)
    hours = (max(int(e[0]) for e in entries) - min(int(e[0]) for e in entries) + 1) if entries else 0
    factor, state = judge(ref, val, lower)
    if ref < MIN_WINDOW_BYTES:
        state = "ok"
    return factor, state, hours, ref, val


def effective(result: dict) -> tuple:
    """(state, factor) that decides for one stored result: the window for DAY_PAIRS, the hour otherwise.

    Results stored before the window existed carry no wstate and are judged by their hour."""
    if result.get("pair") in DAY_PAIRS and "wstate" in result:
        return result.get("wstate") or "ok", result.get("wfactor")
    return result.get("state") or "ok", result.get("factor")


def judged(rows: list) -> list:
    """Findings among stored results: per hour, except for DAY_PAIRS where only the latest window decides."""
    latest = {}
    for r in rows:
        if r.get("pair") in DAY_PAIRS and "wstate" in r:
            if r["pair"] not in latest or int(r.get("hour") or 0) >= int(latest[r["pair"]].get("hour") or 0):
                latest[r["pair"]] = r
    out = []
    for r in rows:
        pair = r.get("pair")
        if pair in latest:
            if r is latest[pair] and effective(r)[0] != "ok":
                out.append(r)
            continue
        if (r.get("state") or "ok") != "ok":
            out.append(r)
    return sorted(out, key=lambda r: int(r.get("hour") or 0))


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
        window = self._note_window(hour, acc) if nf_on else []
        if nf_on and acc.get("wan", 0) >= MIN_UPLINK_BYTES:
            f, state = judge(acc["wan"], acc.get("netflow", 0))
            wf, wstate, whours, _, _ = judge_window(window)
            out.append({"hour": hour, "pair": "uplink_flows", "ref": acc["wan"], "val": acc.get("netflow", 0),
                        "factor": round(f, 3) if f is not None else None, "state": state,
                        "wfactor": round(wf, 3) if wf is not None else None, "wstate": wstate, "whours": whours})
        if acc.get("host_if", 0) >= MIN_HOST_BYTES:
            f, state = judge(acc["host_if"], acc.get("host_flow", 0), lower=False)
            out.append({"hour": hour, "pair": "host_flows", "ref": acc["host_if"], "val": acc.get("host_flow", 0),
                        "factor": round(f, 3) if f is not None else None, "state": state})
        if out:
            self.save(out)
        return out

    def window(self) -> list:
        """Hourly sums [hour, uplink bytes, flow bytes] of the rolling window, oldest first."""
        out = []
        for e in self.store.get_setting(WINDOW_KEY, []) or []:
            if isinstance(e, (list, tuple)) and len(e) == 3:
                try:
                    out.append([int(e[0]), max(0, int(e[1])), max(0, int(e[2]))])
                except (TypeError, ValueError):
                    continue
        return out

    def _note_window(self, hour: int, acc: dict) -> list:
        """Adds the sums of a finished hour, also a quiet one, and keeps only the last WINDOW_H hours up to it."""
        keep = [e for e in self.window() if hour - WINDOW_H < e[0] < hour]   # an hour from a clock jump back drops
        wan, nf = max(0, int(acc.get("wan") or 0)), max(0, int(acc.get("netflow") or 0))
        if wan or nf:
            keep.append([hour, wan, nf])
        self.store.set_setting(WINDOW_KEY, keep)
        return keep

    def save(self, results: list) -> None:
        old = [r for r in (self.store.get_setting("counter_check", []) or []) if isinstance(r, dict)]
        self.store.set_setting("counter_check", (old + results)[-KEEP:])

    def results(self) -> list:
        return [r for r in (self.store.get_setting("counter_check", []) or []) if isinstance(r, dict)]

    def findings(self, since: Optional[float] = None, until: Optional[float] = None) -> list:
        """Results that are not ok, optionally only for hours that end inside (since, until].

        The uplink pair is judged on its window: only its latest result in the range can be a finding."""
        rows = []
        for r in self.results():
            end = (int(r.get("hour") or 0) + 1) * 3600
            if since is not None and end <= since:
                continue
            if until is not None and end > until:
                continue
            rows.append(r)
        return judged(rows)

    def view(self) -> dict:
        res = self.results()
        nf_on = bool(self.store.get_setting("netflow_enabled", False))
        latest = {}
        for r in res:
            latest[r["pair"]] = r
        # findings of the 24 hours up to the newest result (as the table, it does not age while nothing is judged)
        last_end = (max(int(r.get("hour") or 0) for r in res) + 1) * 3600 if res else 0
        return {"low": LOW, "high": HIGH, "results": res[-24:], "latest": latest, "window_h": WINDOW_H,
                "findings": self.findings(since=last_end - 24 * 3600) if res else [], "netflow": nf_on,
                "current": {"hour": self.hour, **{k: int(v) for k, v in self.acc.items()}}}
