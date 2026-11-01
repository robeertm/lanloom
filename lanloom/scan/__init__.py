"""Network discovery (read-only).

Public API:
  detect_networks() -> list[dict]
  make_plan(cidrs, deep=False, consent=False, **overrides) -> dict   (raises ValueError(reason_key))
  async run(plan, progress=None) -> dict                              (ScanResult)
"""
from .netinfo import detect_networks
from .runner import make_plan, run, validate_plan
from .safety import check_cidr

__all__ = ["detect_networks", "make_plan", "run", "validate_plan", "check_cidr"]
