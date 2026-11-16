"""Nightly report section "Access": lock-outs, failed sign-ins, sign-ins from a new address or device, new users,
changed roles, second factors switched off, and whether the audit log is intact. Written from the audit log and the
security notices; never names what was typed into a sign-in form."""
from .nightly.contrib import Contribution, item, register, section

FAILED_MANY = 20


def _available(ctx) -> bool:
    return getattr(ctx.core, "audit", None) is not None


def _build(ctx) -> dict:
    audit = ctx.core.audit
    rows = audit.between(ctx.since, ctx.until, ("auth.", "users.", "account.totp_off", "account.passkey_remove", "security."))
    items = []
    count = lambda action, outcome=None: sum(1 for r in rows if r["action"] == action and (outcome is None or r["outcome"] == outcome))
    locks = [r for r in rows if r["action"] == "auth.lockout"]
    if locks:
        who = sorted({r["target"] for r in locks if r["target"]})
        items.append(item("acc-lock:%d" % locks[-1]["id"], "access.report.lockout", "warn", point=True, n=len(locks),
                          users=", ".join(who[:3]) or "-"))
    failed = count("auth.login_failed") + count("auth.stepup", "failed")
    if failed >= FAILED_MANY:
        items.append(item("acc-failed-many", "access.report.failed_many", "warn", point=True, n=failed))
    elif failed:
        items.append(item("acc-failed", "access.report.failed", "info", n=failed))
    new_dev = ctx.core.store.query("SELECT n.t, u.username FROM notices n JOIN users u ON u.id=n.user_id"
                                   " WHERE n.kind IN ('new_device', 'new_address') AND n.t > ? AND n.t <= ? ORDER BY n.t",
                                   (ctx.since, ctx.until))
    if new_dev:
        who = sorted({r["username"] for r in new_dev})
        items.append(item("acc-new-device", "access.report.new_device", "info", n=len(new_dev), users=", ".join(who[:3])))
    joined = [r for r in rows if r["action"] == "users.joined" and r["outcome"] == "ok"]
    if joined:
        items.append(item("acc-joined", "access.report.joined", "info", n=len(joined),
                          users=", ".join(sorted({r["target"] for r in joined})[:3])))
    changed = [r for r in rows if r["action"] in ("users.change", "users.delete", "users.reset_done") and r["outcome"] == "ok"]
    if changed:
        items.append(item("acc-changed", "access.report.changed", "info", n=len(changed)))
    off = [r for r in rows if r["action"] in ("account.totp_off", "account.passkey_remove") and r["outcome"] == "ok"]
    if off:
        items.append(item("acc-2fa-off:%d" % off[-1]["id"], "access.report.factor_removed", "warn", point=True, n=len(off),
                          users=", ".join(sorted({r["actor"] for r in off})[:3])))
    policy = [r for r in rows if r["action"] == "security.policy" and r["outcome"] == "ok"]
    if policy:
        items.append(item("acc-policy", "access.report.policy", "info", n=len(policy)))
    chain = audit.verify()
    if not chain["ok"]:
        items.append(item("acc-chain:%s" % chain["broken_at"], "access.report.chain_broken", "danger", point=True, at=chain["broken_at"]))
    signins = count("auth.login", "ok")
    key = "access.report.summary" if any(i["point"] for i in items) else "access.report.calm"
    return section("access", "access.report.title", key, items, n=signins, points=sum(1 for i in items if i["point"]))


register(Contribution("access", 36, "access.report.title", _available, _build))
