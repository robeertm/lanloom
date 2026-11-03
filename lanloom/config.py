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
        # identifier: public release feed of this repository (the only outbound connection, can be switched off)
        release_feed=os.environ.get("LANLOOM_RELEASE_FEED", "https://api.github.com/repos/robeertm/lanloom/releases?per_page=30"),
        image_repo=os.environ.get("LANLOOM_IMAGE", "ghcr.io/robeertm/lanloom"),
        cookie_secure=os.environ.get("LANLOOM_COOKIE_SECURE", "auto").strip().lower(),
        trusted_origins=origins,
        trusted_proxies=proxies,
    )
