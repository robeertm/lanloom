"""Nightly report section "Backup": age of the newest backup, the last scheduled run, and a point when the newest
backup is too old (schedule on, nothing newer than twice the interval) or the last scheduled run failed. A restore in
the report window is mentioned. Never names a passphrase or a path outside the data volume."""
from .nightly.contrib import Contribution, item, register, section


def _available(ctx) -> bool:
    return getattr(ctx.core, "backups", None) is not None


def _build(ctx) -> dict:
    b = ctx.core.backups
    s = b.schedule()
    st = b._state()
    newest = b.newest()
    items = []
    if s["enabled"]:
        if b.stale(ctx.until):
            if newest:
                items.append(item("bk-stale:%d" % int(newest), "backup.report.stale", "warn", point=True,
                                  at_at=newest, age_dur=max(0, int(ctx.until - newest))))
            else:
                items.append(item("bk-none", "backup.report.none_yet", "warn", point=True))
        if st.get("last_error") and isinstance(st.get("last_run"), (int, float)) and st["last_run"] > ctx.since - 86400:
            items.append(item("bk-failed:%d" % int(st["last_run"]), "backup.report.failed", "warn", point=True,
                              at_at=st["last_run"], reason_key=st["last_error"]))
        ran = [f for f in b.files() if f.get("auto") and (f.get("created") or f["mtime"]) > ctx.since
               and (f.get("created") or f["mtime"]) <= ctx.until]
        if ran:
            items.append(item("bk-ran", "backup.report.ran", "ok", n=len(ran), bytes_bytes=sum(f["size"] for f in ran)))
    else:
        items.append(item("bk-off", "backup.report.off", "info"))
    restored = isinstance(st.get("last_restore"), (int, float)) and ctx.since < st["last_restore"] <= ctx.until
    if restored:
        items.append(item("bk-restored:%d" % int(st["last_restore"]), "backup.report.restored", "info", at_at=st["last_restore"]))
    if newest:
        key = "backup.report.summary"
        vars_ = {"at_at": newest, "age_dur": max(0, int(ctx.until - newest))}
    else:
        key = "backup.report.summary_never"
        vars_ = {}
    return section("backup", "backup.report.title", key, items, **vars_)


register(Contribution("backup", 38, "backup.report.title", _available, _build))
