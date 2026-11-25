"""测试共用构造工具。"""

from rehearsal.ledger import Ledger
from rehearsal.service import RehearsalService


FIXTURE_REGISTERS = {
    "baseline_version": "board-1",
    "segments": {
        "plaza-yard": {"one_way": "outbound", "change_at": 30},
        "yard-bridge": {"one_way": "both"},
        "bridge-south": {"one_way": "both"},
        "plaza-south": {"one_way": "both"},
    },
    "slots": [
        {"id": "welcome", "team": "drum-a", "equipment": "cart-2",
         "stop": "plaza", "minutes": [10, 25],
         "interactions": ["drum-call"], "access": ["step-free"]},
        {"id": "court-song", "team": "dance-b", "equipment": "cart-2",
         "stop": "yard", "minutes": [35, 50],
         "interactions": ["sing-along"], "access": ["step-free"]},
        {"id": "bridge-parade", "team": "drum-a",
         "stop": "bridge", "minutes": [45, 55],
         "interactions": ["flag-wave"], "access": ["step-free"]},
    ],
    "team_buffers": {
        "drum-a": {"costume": 8, "movement": 9},
        "dance-b": {"movement": 7},
    },
    "tour_groups": [
        {"group": "trip-19", "stop": "yard", "arrival": 28,
         "promised": "court-song", "needs": ["step-free"]},
    ],
    "substitutes": [
        {"for": "court-song", "program": "plaza-song",
         "eligible_stops": ["plaza", "south"], "access": ["step-free"]},
    ],
}


def build_service(registers=None) -> RehearsalService:
    ledger = Ledger()
    ledger.load_registers(registers or FIXTURE_REGISTERS)
    return RehearsalService(ledger)


def error_codes(plan) -> set[str]:
    return {f.code for f in plan.findings if f.level == "error"}


def warning_codes(plan) -> set[str]:
    return {f.code for f in plan.findings if f.level == "warning"}
