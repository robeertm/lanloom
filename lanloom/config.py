"""Runtime configuration from environment variables (all optional)."""
import os
from dataclasses import dataclass
from pathlib import Path


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    data_dir: Path
    host: str
    port: int
    update_check: bool
    demo: bool
    probe: bool
    release_feed: str
    image_repo: str
    cookie_secure: str          # "auto" | "always" | "never"
    trusted_origins: tuple
    trusted_proxies: tuple      # addresses/networks whose X-Forwarded-* headers are believed (empty = none)
    auth_header: str = ""       # sign-in through a trusted auth proxy: header with the user name (empty = off)
    auth_groups_header: str = ""    # header with the user's groups (comma or space separated)
    auth_group_roles: tuple = ()    # ((group, role), ...) - the highest matching role wins
    auth_default_role: str = ""     # role for proxy users without a matching group ("" = refuse)
    base_path: str = ""         # served below a sub-path, e.g. "/lanloom" (optional; a proxy may also strip it)


def _group_roles(raw: str) -> tuple:
    out = []
    for part in (raw or "").split(","):
        if "=" in part:
            group, role = part.split("=", 1)
            group, role = group.strip(), role.strip().lower()
            if group and role in ("viewer", "operator", "admin"):
                out.append((group, role))
    return tuple(out)


def _base_path(raw: str) -> str:
    raw = (raw or "").strip().rstrip("/")
    if not raw:
        return ""
    if not raw.startswith("/"):
        raw = "/" + raw
    import re
    return raw if re.fullmatch(r"(/[A-Za-z0-9._~-]+)+", raw) else ""


def load() -> Config:
    proxies = tuple(p.strip() for p in os.environ.get("LANLOOM_TRUSTED_PROXIES", "").split(",") if p.strip())
    origins = tuple(o.strip().rstrip("/") for o in os.environ.get("LANLOOM_TRUSTED_ORIGINS", "").split(",") if o.strip())
    try:
        port = int(os.environ.get("LANLOOM_PORT", "7380"))
    except ValueError:
        port = 7380
    return Config(
        data_dir=Path(os.environ.get("LANLOOM_DATA", "/data")),
        host=os.environ.get("LANLOOM_HOST", "0.0.0.0"),
        port=port,
        update_check=_flag("LANLOOM_UPDATE_CHECK", True),
        demo=_flag("LANLOOM_DEMO", False),
        probe=_flag("LANLOOM_PROBE", False),
        # identifier: public release feed of this repository (Lanloom's own outbound connection, can be switched off; others exist only when switched on: report delivery, cloud AI)
        release_feed=os.environ.get("LANLOOM_RELEASE_FEED", "https://api.github.com/repos/robeertm/lanloom/releases?per_page=30"),
        image_repo=os.environ.get("LANLOOM_IMAGE", "ghcr.io/robeertm/lanloom"),
        cookie_secure=os.environ.get("LANLOOM_COOKIE_SECURE", "auto").strip().lower(),
        trusted_origins=origins,
        trusted_proxies=proxies,
        auth_header=os.environ.get("LANLOOM_AUTH_HEADER", "").strip(),
        auth_groups_header=os.environ.get("LANLOOM_AUTH_GROUPS_HEADER", "").strip(),
        auth_group_roles=_group_roles(os.environ.get("LANLOOM_AUTH_GROUP_ROLES", "")),
        auth_default_role=(os.environ.get("LANLOOM_AUTH_DEFAULT_ROLE", "").strip().lower()
                           if os.environ.get("LANLOOM_AUTH_DEFAULT_ROLE", "").strip().lower() in ("viewer", "operator", "admin") else ""),
        base_path=_base_path(os.environ.get("LANLOOM_BASE_PATH", "")),
    )
