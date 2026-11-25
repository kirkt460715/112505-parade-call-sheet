"""领域模型：所有时间均以活动开场后的分钟整数表示。

业务术语对照：
- segment  古村巷口/路口，按登记方向通行
- stop     站点（广场、院、桥、南广场……）
- slot     排演单上的一个节目预约（场地、队伍、道具、时段）
- booking  某一版排演单中实际生效的预约（slot 可被改排，booking 是版本化的结果）
- group    导游带的游客团
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# ---------- 场地与通路 ----------

@dataclass(frozen=True)
class SegmentReport:
    """场地上报的一段通路的方向与生效时间。"""
    segment: str
    direction: str          # both / outbound / inbound / closed
    since: int
    source_cursor: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FenceState:
    """路口版本围栏：记录该路口当前按哪一版排演单放行/拦截。

    同一段路两端围栏版本不一致时，任何一端都不得自行推断该段已开放。
    """
    stop: str
    version: str
    action: str             # released（放行）/ blocked（拦截）
    minute: int
    cursor: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------- 节目与预约 ----------

@dataclass(frozen=True)
class Buffer:
    """同队连续两场之间的缓冲（分钟）。"""
    costume: int = 0
    movement: int = 0


@dataclass(frozen=True)
class Booking:
    booking_id: str
    program: str
    stop: str
    team: Optional[str]
    equipment: Optional[str]
    start: int
    end: int
    kind: str = "parade"            # parade（巡游）/ fixed（定点表演）
    slot_id: Optional[str] = None   # 由哪个原始 slot 改排而来
    substitution_for: Optional[str] = None
    interactions: tuple[str, ...] = ()
    access: tuple[str, ...] = ()

    def occupies(self, resource: str) -> bool:
        return resource == self.team or resource == self.equipment

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["interactions"] = list(self.interactions)
        data["access"] = list(self.access)
        return data


# ---------- 游客 ----------

@dataclass
class TourGroup:
    group: str
    stop: str
    arrival: int
    promised_program: str
    needs: tuple[str, ...]
    disposition: str = "waiting"    # waiting（原地等候）/ movable（可带走）
    current_stop: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "stop": self.stop,
            "arrival": self.arrival,
            "promised_program": self.promised_program,
            "needs": list(self.needs),
            "disposition": self.disposition,
            "current_stop": self.current_stop or self.stop,
        }


@dataclass(frozen=True)
class Substitute:
    """事先认可的替补节目。"""
    for_program: str
    program: str
    eligible_stops: frozenset[str]
    access: frozenset[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "for": self.for_program,
            "program": self.program,
            "eligible_stops": sorted(self.eligible_stops),
            "access": sorted(self.access),
        }


# ---------- 现场进度与回执 ----------

@dataclass(frozen=True)
class ProgressRecord:
    minute: int
    kind: str               # team_start / pickup
    booking_id: Optional[str] = None
    team: Optional[str] = None
    stop: Optional[str] = None
    groups: tuple[str, ...] = ()
    version: Optional[str] = None
    cursor: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["groups"] = list(self.groups)
        return data


@dataclass
class DeliveryState:
    """某站点对某版排演单的送达/生效状态。"""
    stop: str
    version: str
    delivered: bool = False
    in_effect: bool = False
    updated_minute: Optional[int] = None
    cursor: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IngestedEvent:
    cursor: int
    source: str
    minute: int
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------- 改排草稿与排演单 ----------

@dataclass
class Finding:
    level: str              # error / warning
    code: str
    message: str
    ref: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlanDraft:
    plan_id: str
    base_version: str
    changes: tuple[dict[str, Any], ...]
    bookings: tuple[Booking, ...]
    findings: list[Finding] = field(default_factory=list)
    generation: int = 0
    status: str = "proposed"        # proposed / confirmed / superseded / rejected
    created_minute: int = 0

    @property
    def valid(self) -> bool:
        return not any(f.level == "error" for f in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "base_version": self.base_version,
            "generation": self.generation,
            "status": self.status,
            "created_minute": self.created_minute,
            "changes": list(self.changes),
            "findings": [f.to_dict() for f in self.findings],
            "bookings": [b.to_dict() for b in self.bookings],
        }


@dataclass
class CallSheet:
    """一版排演单：不可变的预约集合 + 版本元数据。"""
    version: str
    generation: int
    bookings: tuple[Booking, ...]
    created_minute: int
    plan_id: Optional[str] = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "generation": self.generation,
            "created_minute": self.created_minute,
            "plan_id": self.plan_id,
            "note": self.note,
            "bookings": [b.to_dict() for b in self.bookings],
        }
