"""线程安全的内存台账，带 JSON 快照持久化（用于断线后恢复）。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from threading import RLock
from typing import Optional

from .models import (
    Booking, Buffer, CallSheet, DeliveryState, FenceState, IngestedEvent,
    PlanDraft, ProgressRecord, SegmentReport, Substitute, TourGroup,
)


class Ledger:
    def __init__(self, snapshot_path: Optional[str | Path] = None):
        self.lock = RLock()
        self.snapshot_path = Path(snapshot_path) if snapshot_path else None

        # 静态登记
        self.segment_geometry: dict[str, tuple[str, str]] = {}
        self.buffers: dict[str, Buffer] = {}
        self.substitutes: list[Substitute] = []
        self.absent_teams: set[str] = set()

        # 动态状态
        self.segment_reports: dict[str, list[SegmentReport]] = {}
        self.fences: dict[str, FenceState] = {}
        self.groups: dict[str, TourGroup] = {}
        self.sheets: dict[str, CallSheet] = {}          # version -> sheet
        self.sheet_order: list[str] = []
        self.plans: dict[str, PlanDraft] = {}
        self.deliveries: dict[tuple[str, str], DeliveryState] = {}
        self.progress: list[ProgressRecord] = []
        self.events: list[IngestedEvent] = []
        self.event_cursor = 0

        if self.snapshot_path and self.snapshot_path.exists():
            self._load_snapshot()

    # ---------- 基础装载（来自 registers.json） ----------

    def load_registers(self, registers: dict) -> None:
        with self.lock:
            self.segment_geometry = {}
            self.segment_reports = {}
            for seg_id, spec in registers.get("segments", {}).items():
                a, b = seg_id.split("-", 1)
                self.segment_geometry[seg_id] = (a, b)
                direction = spec.get("one_way", "both")
                change_at = spec.get("change_at")
                if change_at is not None and int(change_at) > 0:
                    # change_at 是临时改向的生效点；此前按双向基线通行
                    self.segment_reports[seg_id] = [
                        SegmentReport(seg_id, "both", 0),
                        SegmentReport(seg_id, direction, int(change_at)),
                    ]
                else:
                    self.segment_reports[seg_id] = [
                        SegmentReport(seg_id, direction, 0)
                    ]
            self.buffers = {
                team: Buffer(costume=int(spec.get("costume", 0)),
                             movement=int(spec.get("movement", 0)))
                for team, spec in registers.get("team_buffers", {}).items()
            }
            self.substitutes = [
                Substitute(
                    for_program=item["for"],
                    program=item["program"],
                    eligible_stops=frozenset(item["eligible_stops"]),
                    access=frozenset(item.get("access", [])),
                )
                for item in registers.get("substitutes", [])
            ]
            self.absent_teams = set(registers.get("absent_teams", []))
            for raw in registers.get("tour_groups", []):
                needs = raw.get("needs", [])
                if isinstance(needs, str):
                    needs = [needs]
                group = TourGroup(
                    group=raw["group"],
                    stop=raw["stop"],
                    arrival=int(raw["arrival"]),
                    promised_program=raw["promised"],
                    needs=tuple(needs),
                )
                self.groups[group.group] = group

            bookings = tuple(self._booking_from_slot(i, slot)
                             for i, slot in enumerate(registers.get("slots", []), 1))
            baseline = CallSheet(
                version=registers.get("baseline_version", "board-1"),
                generation=1,
                bookings=bookings,
                created_minute=0,
                note="baseline",
            )
            self.sheets = {baseline.version: baseline}
            self.sheet_order = [baseline.version]
            self._persist_locked()

    @staticmethod
    def _booking_from_slot(index: int, slot: dict) -> Booking:
        start, end = (int(x) for x in slot["minutes"])
        return Booking(
            booking_id=f"b{index}",
            program=slot["id"],
            stop=slot["stop"],
            team=slot.get("team"),
            equipment=slot.get("equipment"),
            start=start,
            end=end,
            slot_id=slot["id"],
            interactions=tuple(slot.get("interactions", [])),
            access=tuple(slot.get("access", [])),
        )

    # ---------- 读取辅助 ----------

    @property
    def current_sheet(self) -> CallSheet:
        return self.sheets[self.sheet_order[-1]]

    @property
    def current_generation(self) -> int:
        return self.current_sheet.generation

    def booking(self, booking_id: str, sheet: Optional[CallSheet] = None) -> Optional[Booking]:
        sheet = sheet or self.current_sheet
        return next((b for b in sheet.bookings if b.booking_id == booking_id), None)

    def bookings_for_slot(self, slot_id: str, sheet: Optional[CallSheet] = None) -> list[Booking]:
        sheet = sheet or self.current_sheet
        return [b for b in sheet.bookings if b.slot_id == slot_id]

    def executed_booking_ids(self) -> set[str]:
        """已被现场事实确认的预约（队伍开演或实际接人均不可回退）。"""
        ids: set[str] = set()
        for record in self.progress:
            if record.booking_id:
                ids.add(record.booking_id)
        return ids

    def picked_up_groups(self) -> dict[str, str]:
        """group -> 在接人记录里实际参加的节目。"""
        result: dict[str, str] = {}
        for record in self.progress:
            if record.kind == "pickup":
                for group in record.groups:
                    result[group] = record.booking_id or ""
        return result

    def delivery(self, stop: str, version: str) -> Optional[DeliveryState]:
        return self.deliveries.get((stop, version))

    def versions_at_stop(self, stop: str) -> list[DeliveryState]:
        return [d for (s, _v), d in self.deliveries.items() if s == stop]

    # ---------- 快照持久化 ----------

    def persist(self) -> None:
        with self.lock:
            self._persist_locked()

    def _persist_locked(self) -> None:
        if not self.snapshot_path:
            return
        payload = {
            "segment_geometry": self.segment_geometry,
            "buffers": {k: asdict(v) for k, v in self.buffers.items()},
            "substitutes": [s.to_dict() for s in self.substitutes],
            "absent_teams": sorted(self.absent_teams),
            "segment_reports": [
                r.to_dict() for reports in self.segment_reports.values() for r in reports
            ],
            "fences": [f.to_dict() for f in self.fences.values()],
            "groups": [g.to_dict() for g in self.groups.values()],
            "sheets": [
                {**s.to_dict(),
                 "bookings": [{**b.to_dict(),
                               "interactions": list(b.interactions),
                               "access": list(b.access)} for b in s.bookings]}
                for s in (self.sheets[v] for v in self.sheet_order)
            ],
            "sheet_order": list(self.sheet_order),
            "plans": [p.to_dict() for p in self.plans.values()],
            "deliveries": [d.to_dict() for d in self.deliveries.values()],
            "progress": [p.to_dict() for p in self.progress],
            "events": [e.to_dict() for e in self.events],
            "event_cursor": self.event_cursor,
        }
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.snapshot_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(tmp, self.snapshot_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _load_snapshot(self) -> None:
        data = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        self.segment_geometry = {k: tuple(v) for k, v in data["segment_geometry"].items()}
        self.buffers = {k: Buffer(**v) for k, v in data["buffers"].items()}
        self.substitutes = [
            Substitute(for_program=i["for"], program=i["program"],
                       eligible_stops=frozenset(i["eligible_stops"]),
                       access=frozenset(i["access"]))
            for i in data["substitutes"]
        ]
        self.absent_teams = set(data.get("absent_teams", []))
        self.segment_reports = {}
        for raw in data["segment_reports"]:
            self.segment_reports.setdefault(raw["segment"], []).append(SegmentReport(**raw))
        self.fences = {f["stop"]: FenceState(**f) for f in data.get("fences", [])}
        self.groups = {}
        for raw in data["groups"]:
            self.groups[raw["group"]] = TourGroup(
                group=raw["group"], stop=raw["stop"], arrival=raw["arrival"],
                promised_program=raw["promised_program"], needs=tuple(raw["needs"]),
                disposition=raw["disposition"], current_stop=raw.get("current_stop"),
            )
        self.sheets = {}
        self.sheet_order = []
        for raw in data["sheets"]:
            bookings = tuple(
                Booking(**{**b, "interactions": tuple(b["interactions"]),
                           "access": tuple(b["access"])})
                for b in raw["bookings"]
            )
            sheet = CallSheet(version=raw["version"], generation=raw["generation"],
                              bookings=bookings, created_minute=raw["created_minute"],
                              plan_id=raw.get("plan_id"), note=raw.get("note", ""))
            self.sheets[sheet.version] = sheet
            self.sheet_order.append(sheet.version)
        for raw in data.get("plans", []):
            self.plans[raw["plan_id"]] = PlanDraft(
                plan_id=raw["plan_id"], base_version=raw["base_version"],
                changes=tuple(raw["changes"]),
                bookings=tuple(Booking(**{**b, "interactions": tuple(b["interactions"]),
                                          "access": tuple(b["access"])})
                               for b in raw["bookings"]),
                findings=[], generation=raw["generation"], status=raw["status"],
                created_minute=raw["created_minute"],
            )
        for raw in data.get("deliveries", []):
            state = DeliveryState(**raw)
            self.deliveries[(state.stop, state.version)] = state
        self.progress = [
            ProgressRecord(**{**r, "groups": tuple(r["groups"])})
            for r in data.get("progress", [])
        ]
        self.events = [IngestedEvent(**e) for e in data.get("events", [])]
        self.event_cursor = data.get("event_cursor", len(self.events))
