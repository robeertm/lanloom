"""Translation module of the sample family (vendor <-> neutral). The sample network already is neutral, so the
mapping is the identity; real families map their vendor objects here, shared by the read and the write half."""
import copy

from ...firewall.adapter import sample_state


def to_neutral(raw: dict) -> dict:
    return copy.deepcopy(raw)


def initial() -> dict:
    return sample_state()
