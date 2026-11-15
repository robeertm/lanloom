"""Write half of the sample family (executor only): the in-memory sample network."""
from ...firewall.adapter import SimulatedController


class SampleWriter(SimulatedController):
    family = "simulation"
