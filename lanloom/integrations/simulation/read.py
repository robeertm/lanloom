"""Read half of the sample family: returns the neutral sample network. No send path at all."""
from . import CAPABILITIES, model
from .. import capabilities


class SampleReader:
    family = "simulation"

    def __init__(self, state: dict = None):
        self._raw = state if state is not None else model.initial()

    def __repr__(self):
        return "SampleReader()"

    def capabilities(self) -> dict:
        return capabilities.validate(CAPABILITIES, self.family)

    def read(self) -> dict:
        return model.to_neutral(self._raw)

    async def account_scope(self) -> str:
        """read_only | can_write | unknown - the app refuses an account that can write, when that is checkable."""
        return "read_only"
