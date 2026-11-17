"""Roles and the one table that says who may call which route.

Three roles, each includes the rights of the one before:

* viewer   - reads everything that is not an administrative secret; changes only its own account.
* operator - additionally drafts, renames, hides devices, runs scans and ticks report items.
* admin    - additionally settings, credentials, users, executor approvals, AI and updates.

The table is enforced on the server for every request (main.py guard); the web page only hides what a role cannot
use. A route missing from the table is refused for everyone below admin (fail closed), and tests/test_access_matrix.py
checks every route of the app against this table, so a new route cannot slip through unnoticed.

Per route:
  role    - lowest role that may call it ("public" for the sign-in routes, "executor" for the executor token)
  stepup  - True: needs a recent second confirmation (passkey, one-time code, or password without second factor)
  audit   - action name written to the audit log for state-changing calls ("@" = the handler writes it itself)
  enroll  - reachable by a session that still has to set up its second factor
"""
from collections import namedtuple

ROLES = ("viewer", "operator", "admin")
RANK = {r: i for i, r in enumerate(ROLES)}
DEFAULT_ROLE = "viewer"

Rule = namedtuple("Rule", "role stepup audit enroll")


def R(role: str, audit=None, stepup: bool = False, enroll: bool = False) -> Rule:
    return Rule(role, stepup, audit, enroll)


V, O, A = "viewer", "operator", "admin"

RULES = {
    # ---------------------------------------------------------------- public (no session)
    ("GET", "/api/health"): R("public"),
    ("GET", "/api/state"): R("public", enroll=True),
    ("GET", "/api/i18n/{lang}"): R("public", enroll=True),
    ("POST", "/api/setup/account"): R("public", "@"),
    ("POST", "/api/login"): R("public", "@"),
    ("POST", "/api/login/second"): R("public", "@"),
    ("POST", "/api/passkey/login/options"): R("public"),
    ("POST", "/api/passkey/login/verify"): R("public", "@"),
    ("POST", "/api/invite/check"): R("public"),
    ("POST", "/api/invite/accept"): R("public", "@"),
    ("GET", "/api/ai/installer/{os_name}"): R("public"),
    ("POST", "/api/ai/pair/hello"): R("public"),
    ("POST", "/api/ai/pair"): R("public", "ai.paired"),
    ("POST", "/api/ai/pair/status"): R("public"),
    ("GET", "/"): R("public"),
    ("GET", "/{path:path}"): R("public"),

    # ---------------------------------------------------------------- own account (every role)
    ("POST", "/api/logout"): R(V, "auth.logout", enroll=True),
    ("GET", "/api/account"): R(V, enroll=True),
    ("PUT", "/api/account/prefs"): R(V, "account.prefs"),
    ("POST", "/api/account/password"): R(V, "account.password"),
    ("POST", "/api/account/stepup/options"): R(V),
    ("POST", "/api/account/stepup"): R(V, "@"),
    ("POST", "/api/account/totp/setup"): R(V, enroll=True),
    ("POST", "/api/account/totp/enable"): R(V, "account.totp_on", stepup=True, enroll=True),
    ("DELETE", "/api/account/totp"): R(V, "account.totp_off", stepup=True),
    ("POST", "/api/account/recovery"): R(V, "account.recovery_new", stepup=True),
    ("GET", "/api/passkeys"): R(V, enroll=True),
    ("POST", "/api/passkey/register/options"): R(V, stepup=True, enroll=True),
    ("POST", "/api/passkey/register/verify"): R(V, "account.passkey_add", stepup=True, enroll=True),
    ("DELETE", "/api/passkeys/{key_id}"): R(V, "account.passkey_remove", stepup=True),
    ("GET", "/api/account/sessions"): R(V),
    ("DELETE", "/api/account/sessions"): R(V, "account.signout_all"),
    ("DELETE", "/api/account/sessions/{sid}"): R(V, "account.session_end"),
    ("GET", "/api/account/notices"): R(V),
    ("POST", "/api/account/notices/seen"): R(V, "account.notices_seen"),

    # ---------------------------------------------------------------- users, security policy, audit log (admin)
    ("GET", "/api/users"): R(A),
    ("POST", "/api/users/invite"): R(A, "users.invite", stepup=True),
    ("DELETE", "/api/users/invites/{invite_id}"): R(A, "users.invite_revoke"),
    ("PATCH", "/api/users/{user_id}"): R(A, "users.change", stepup=True),
    ("DELETE", "/api/users/{user_id}"): R(A, "users.delete", stepup=True),
    ("POST", "/api/users/{user_id}/reset"): R(A, "users.reset_link", stepup=True),
    ("GET", "/api/users/{user_id}/sessions"): R(A),
    ("DELETE", "/api/users/{user_id}/sessions"): R(A, "users.signout_all", stepup=True),
    ("GET", "/api/security"): R(A),
    ("PUT", "/api/security"): R(A, "security.policy", stepup=True),
    ("GET", "/api/audit"): R(A),
    ("GET", "/api/audit/export"): R(A),

    # ---------------------------------------------------------------- backup and restore (admin; set-up page: set-up code)
    ("GET", "/api/backup"): R(A),
    ("PUT", "/api/backup/settings"): R(A, "backup.settings", stepup=True),
    ("POST", "/api/backup/export"): R(A, "backup.export", stepup=True),
    ("POST", "/api/backup/run"): R(A, "backup.run"),
    ("GET", "/api/backup/files/{name}"): R(A, stepup=True),             # the handler writes backup.download
    ("DELETE", "/api/backup/files/{name}"): R(A, "backup.delete"),
    ("POST", "/api/backup/upload"): R(A, "backup.upload"),
    ("POST", "/api/backup/check"): R(A, "backup.check"),
    ("POST", "/api/backup/restore"): R(A, "@", stepup=True),
    ("POST", "/api/setup/restore/upload"): R("public", "@"),
    ("POST", "/api/setup/restore/check"): R("public", "@"),
    ("POST", "/api/setup/restore"): R("public", "@"),

    # ---------------------------------------------------------------- settings, networks, scans
    ("GET", "/api/settings"): R(V),
    ("GET", "/api/storage"): R(A),
    ("PUT", "/api/storage/retention"): R(A, "storage.retention"),
    ("POST", "/api/storage/cleanup"): R(A, "storage.cleanup"),
    ("GET", "/api/selfcheck/counters"): R(V),
    ("PUT", "/api/settings"): R(A, "settings.change"),
    ("GET", "/api/paths"): R(V),
    ("GET", "/api/netflow"): R(A),
    ("GET", "/api/networks"): R(V),
    ("GET", "/api/setup/discover"): R(V),
    ("GET", "/api/setup/suggestions"): R(A),
    ("GET", "/api/setup/timing"): R(V),
    ("POST", "/api/setup/demo"): R(V, "setup.demo"),
    ("GET", "/api/scan"): R(V),
    ("POST", "/api/scan"): R(O, "scan.start"),
    ("DELETE", "/api/scan"): R(O, "scan.cancel"),

    # ---------------------------------------------------------------- inventory and map
    ("GET", "/api/devices"): R(V),
    ("GET", "/api/search"): R(V),
    ("GET", "/api/overview"): R(V),
    ("GET", "/api/overview/now"): R(V),
    ("PATCH", "/api/devices/{device_id}"): R(O, "device.change"),
    ("DELETE", "/api/devices/{device_id}"): R(O, "device.forget"),
    ("POST", "/api/devices/{device_id}/identity"): R(O, "device.identity"),
    ("POST", "/api/devices/{device_id}/protect"): R(O, "device.protect"),
    ("GET", "/api/devices/{device_id}/integrate"): R(V),
    ("GET", "/api/integrate"): R(V),
    ("GET", "/api/protect"): R(V),
    ("GET", "/api/zones"): R(V),
    ("POST", "/api/zones/consent"): R(A, "zone.consent"),
    ("GET", "/api/inventory"): R(V),
    ("GET", "/api/devices/{device_id}/inventory"): R(V),
    ("PUT", "/api/devices/{device_id}/inventory"): R(O, "inventory.change"),
    ("POST", "/api/devices/{device_id}/lifecycle"): R(O, "inventory.lifecycle"),
    ("POST", "/api/inventory/bulk"): R(O, "inventory.bulk"),
    ("GET", "/api/inventory/settings"): R(V),
    ("PUT", "/api/inventory/settings"): R(A, "inventory.settings"),
    ("GET", "/api/topology"): R(V),
    ("GET", "/api/map"): R(V),
    ("GET", "/api/component/{comp_id:path}"): R(V),

    # ---------------------------------------------------------------- integrations (credentials: admin)
    ("GET", "/api/integrations"): R(O),
    ("POST", "/api/integrations/test"): R(A, "integration.test"),
    ("POST", "/api/integrations"): R(A, "integration.save", stepup=True),
    ("DELETE", "/api/integrations/{integ_id}"): R(A, "integration.delete", stepup=True),

    # ---------------------------------------------------------------- hub bridge (read only; traces cost the hub work: operator)
    ("GET", "/api/hub/{integ_id}/bridge"): R(V),
    ("GET", "/api/hub/{integ_id}/automations"): R(V),
    ("GET", "/api/hub/{integ_id}/traces/{entity_id}"): R(V),
    ("POST", "/api/hub/{integ_id}/traces/{entity_id}"): R(O, "hub.traces"),
    ("GET", "/api/hub/{integ_id}/sensors"): R(V),
    ("GET", "/api/hub/{integ_id}/entity/{entity_id}"): R(V),       # history: within the bridge's own budget
    ("POST", "/api/hub/{integ_id}/match"): R(O, "hub.match"),

    # ---------------------------------------------------------------- notifications (own rules for everyone)
    ("GET", "/api/notify"): R(V),
    ("PUT", "/api/notify/prefs"): R(V, "notify.prefs"),
    ("GET", "/api/notify/history"): R(V),
    ("POST", "/api/notify/push"): R(V, "notify.push_add"),
    ("POST", "/api/notify/push/forget"): R(V, "notify.push_remove"),
    ("DELETE", "/api/notify/push/{sub_id}"): R(V, "notify.push_remove"),
    ("POST", "/api/notify/test"): R(V, "notify.test"),
    ("POST", "/api/notify/maintenance"): R(O, "notify.maintenance_add"),
    ("DELETE", "/api/notify/maintenance/{window_id}"): R(O, "notify.maintenance_remove"),
    ("PUT", "/api/notify/settings"): R(A, "notify.settings"),
    ("PUT", "/api/notify/heartbeat"): R(A, "notify.heartbeat"),
    ("POST", "/api/notify/heartbeat/test"): R(A, "notify.heartbeat_test"),

    # ---------------------------------------------------------------- nightly reports
    ("GET", "/api/reports"): R(V),
    ("GET", "/api/reports/preview"): R(V),
    ("GET", "/api/reports/settings"): R(A),
    ("PUT", "/api/reports/settings"): R(A, "reports.settings"),
    ("POST", "/api/reports/tz"): R(O, "reports.timezone"),
    ("POST", "/api/reports/test"): R(A, "reports.test"),
    ("GET", "/api/reports/{day}"): R(V),
    ("POST", "/api/reports/{day}/read"): R(O, "reports.read"),
    ("PUT", "/api/reports/{day}/items"): R(O, "reports.tick"),

    # ---------------------------------------------------------------- AI (explain: operator, everything else: admin)
    ("GET", "/api/ai"): R(A),
    ("PUT", "/api/ai"): R(A, "ai.settings"),
    ("POST", "/api/ai/models"): R(A, "ai.models"),
    ("POST", "/api/ai/test"): R(A, "ai.test"),
    ("GET", "/api/ai/jobs/{job_id}"): R(O),
    ("POST", "/api/ai/explain"): R(O, "ai.explain"),
    ("POST", "/api/ai/ask"): R(O, "ai.ask"),                     # read-only facts; the question is never logged
    ("POST", "/api/ai/report"): R(O, "ai.report_plain"),
    ("POST", "/api/ai/identify"): R(O, "ai.identify"),
    ("GET", "/api/ai/hints"): R(O),
    ("POST", "/api/ai/hints/{device_id}"): R(O, "ai.hint"),
    ("POST", "/api/ai/firewall"): R(O, "ai.firewall_suggest"),   # a proposal only; the draft is the user's click
    ("GET", "/api/ai/local"): R(A),
    ("POST", "/api/ai/local/discover"): R(A, "ai.discover"),
    ("POST", "/api/ai/local/install"): R(A, "ai.install"),
    ("POST", "/api/ai/local/use"): R(A, "ai.use"),
    ("POST", "/api/ai/local/remove"): R(A, "ai.remove"),
    ("POST", "/api/ai/pairing"): R(A, "ai.pairing", stepup=True),

    # ---------------------------------------------------------------- updates
    ("GET", "/api/update"): R(V),
    ("POST", "/api/update/check"): R(A, "update.check"),
    ("POST", "/api/update/apply"): R(A, "update.apply"),

    # ---------------------------------------------------------------- firewall editor
    ("GET", "/api/firewall"): R(V),
    ("PUT", "/api/firewall/draft"): R(O, "firewall.draft"),
    ("DELETE", "/api/firewall/draft"): R(O, "firewall.draft_clear"),
    ("POST", "/api/firewall/preview"): R(O, "firewall.preview"),
    ("POST", "/api/firewall/plan"): R(A, "firewall.plan"),
    ("POST", "/api/firewall/approve"): R(A, "firewall.approve"),      # the approval itself is a passkey signature
    ("POST", "/api/firewall/undo"): R(O, "firewall.undo"),
    ("POST", "/api/firewall/reset"): R(A, "firewall.reset"),
    ("GET", "/api/firewall/keys"): R(A),

    # ---------------------------------------------------------------- executor service (bearer token, no session)
    ("GET", "/api/executor/plans"): R("executor"),
    ("POST", "/api/executor/state"): R("executor"),
    ("POST", "/api/executor/report"): R("executor"),

    # ---------------------------------------------------------------- live feed (WebSocket)
    ("WS", "/api/live"): R(V),
}

# Every action name the audit log can carry: the ones of the table plus those the handlers write themselves.
# The page shows them through the texts audit.action.<name>; tests/test_access_matrix.py checks that each has one.
HANDLER_ACTIONS = ("auth.login", "auth.login_failed", "auth.lockout", "auth.setup_failed", "auth.stepup", "users.joined",
                   "users.reset_done", "invite.failed", "audit.export", "access.denied", "backup.download", "backup.restored",
                   "backup.restore_failed")
AUDIT_ACTIONS = frozenset({r.audit for r in RULES.values() if r.audit and r.audit != "@"} | set(HANDLER_ACTIONS))

# Fields that only admins see, whatever route carries them (location of the site, e.g. the cell of a mobile router).
ADMIN_ONLY_FIELDS = frozenset({"location", "cell_id", "cell", "lac", "tac", "latitude", "longitude"})

# Routes a viewer reaches although they change something: harmless per-user state, listed so the matrix test knows.
VIEWER_WRITES = {k for k, r in RULES.items() if r.role == V and k[0] not in ("GET", "WS")}


def rank(role) -> int:
    return RANK.get(role, -1)


def allows(role, need: str) -> bool:
    if need == "public":
        return True
    if need not in RANK:
        return False
    return rank(role) >= RANK[need]


def rule_for(method: str, path: str):
    return RULES.get((method, path))


def redact_admin_fields(data, role):
    """Copy of `data` without ADMIN_ONLY_FIELDS (recursively) unless `role` is admin."""
    if allows(role, A):
        return data
    if isinstance(data, dict):
        return {k: redact_admin_fields(v, role) for k, v in data.items() if k not in ADMIN_ONLY_FIELDS}
    if isinstance(data, list):
        return [redact_admin_fields(v, role) for v in data]
    return data


def capabilities(role) -> dict:
    """What the web page may offer to this role (only for showing or hiding controls; the server decides)."""
    return {
        "role": role,
        "operate": allows(role, O),
        "admin": allows(role, A),
    }
