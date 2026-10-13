"""Emulated controllers: applies approved plans to traffic lights inside the SUMO simulation."""
import traci
from traci import _trafficlight as tl

from .adapter import AdapterError, ControllerAdapter


class SumoAdapter(ControllerAdapter):
    def __init__(self, tables: dict[str, dict[int, dict[int, float]]]):
        """tables: tls id -> plan id -> {phase index: green seconds}; what the engineer loaded."""
        self.tables = tables
        self._base = {}  # tls id -> the program as it was before the first change

    def plans(self, tls):
        return set(self.tables.get(tls, {}))

    def select(self, tls, plan):
        splits = self.tables.get(tls, {}).get(plan)
        if splits is None or tls not in traci.trafficlight.getIDList():
            raise AdapterError("plan or controller not found")
        logic = self._base.setdefault(tls, traci.trafficlight.getAllProgramLogics(tls)[0])
        self._program(tls, logic, splits)

    def base(self, tls):
        logic = self._base.pop(tls, None)
        if logic is not None:
            self._program(tls, logic, {})

    @staticmethod
    def _program(tls, logic, splits):
        current = traci.trafficlight.getPhase(tls)
        phases = [tl.Phase(splits.get(i, p.duration), p.state, p.minDur, p.maxDur, p.next, p.name)
                  for i, p in enumerate(logic.phases)]
        traci.trafficlight.setProgramLogic(
            tls, tl.Logic(logic.programID, logic.type, current, phases=phases, subParameter=logic.subParameter))
        traci.trafficlight.setPhase(tls, current)
