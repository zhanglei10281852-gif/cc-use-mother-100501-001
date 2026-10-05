"""流域联合调度领域包。"""

from .contracts import DispatchScenario, canonical_fingerprint, canonical_json, unique_by_identity
from .engine import Plan, ValidationReport, Violation, validate_plan
from .events import Event, EventStore
from .scenario import Scenario, ScenarioState, build_scenario
from .service import DispatchService, DomainError
from .topology import Facility, FacilityType, Topology, build_topology

__all__ = [
    "DispatchScenario",
    "canonical_fingerprint",
    "canonical_json",
    "unique_by_identity",
    "Plan",
    "ValidationReport",
    "Violation",
    "validate_plan",
    "Event",
    "EventStore",
    "Scenario",
    "ScenarioState",
    "build_scenario",
    "DispatchService",
    "DomainError",
    "Facility",
    "FacilityType",
    "Topology",
    "build_topology",
]
