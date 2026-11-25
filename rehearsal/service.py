"""应用服务：导演台/场地/各队共用的改排办理入口。"""

from __future__ import annotations

import itertools
from typing import Optional

from .engine import (
    EngineError, apply_changes, find_route, promise_status,
    segment_direction, validate_plan,
)
from .ledger import Ledger
from .models import (
    Booking, CallSheet, DeliveryState, FenceState, IngestedEvent, PlanDraft,
    ProgressRecord, SegmentReport, TourGroup,
)


class RehearsalService:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        start = 1
        for plan_id in ledger.plans:
            if plan_id.startswith("plan-") and plan_id[5:].isdigit():
                start = max(start, int(plan_id[5:]) + 1)
        self._plan_seq = itertools.count(start)

    # ================= 场地：分段方向 =================

    def report_segment(self, segment: str, direction: str, minute: int,
                       cursor: Optional[int] = None) -> SegmentReport:
        if segment not in self.ledger.segment_geometry:
            raise EngineError("unknown_segment", f"未知分段 {segment}", 404)
        if direction not in {"both", "outbound", "inbound", "closed"}:
            raise EngineError("invalid_direction",
                              "direction 仅支持 both/outbound/inbound/closed", 400)
        with self.ledger.lock:
            report = SegmentReport(segment, direction, int(minute), cursor)
            reports = self.ledger.segment_reports.setdefault(segment, [])
            reports.append(report)
            reports.sort(key=lambda r: r.since)
            self.ledger.persist()
        return report

    def segment_status(self, minute: int) -> list[dict]:
        with self.ledger.lock:
            rows = []
            for segment, (a, b) in self.ledger.segment_geometry.items():
                fa, fb = self.ledger.fences.get(a), self.ledger.fences.get(b)
                split = bool(fa and fb and (fa.version != fb.version
                                            or fa.action != fb.action))
                rows.append({
                    "segment": segment,
                    "stops": [a, b],
                    "direction": segment_direction(self.ledger, segment, minute),
                    "fence": {"a": fa.to_dict() if fa else None,
                              "b": fb.to_dict() if fb else None,
                              "split_brain": split},
                })
            return rows

    def route(self, origin: str, destination: str, minute: int) -> dict:
        return find_route(self.ledger, origin, destination, int(minute))

    # ================= 场地/观众：游客到位 =================

    def register_audience(self, group_id: str, stop: str, minute: int,
                          disposition: str = "waiting",
                          cursor: Optional[int] = None) -> TourGroup:
        if disposition not in {"waiting", "movable"}:
            raise EngineError("invalid_disposition",
                              "disposition 仅支持 waiting/movable", 400)
        with self.ledger.lock:
            group = self.ledger.groups.get(group_id)
            if group is None:
                raise EngineError("unknown_group", f"未知游客团 {group_id}", 404)
            group.arrival = int(minute)
            group.current_stop = stop
            group.disposition = disposition
            self.ledger.persist()
            return group

    # ================= 导演台：改排草稿与确认 =================

    def create_plan(self, changes: list[dict], minute: int,
                    base_version: Optional[str] = None) -> PlanDraft:
        with self.ledger.lock:
            base_version = base_version or self.ledger.current_sheet.version
            candidates = apply_changes(self.ledger, base_version, changes)
            findings = validate_plan(self.ledger, changes, candidates, int(minute))
            plan = PlanDraft(
                plan_id=f"plan-{next(self._plan_seq)}",
                base_version=base_version,
                changes=tuple(changes),
                bookings=tuple(candidates),
                findings=findings,
                generation=self.ledger.current_generation,
                created_minute=int(minute),
            )
            self.ledger.plans[plan.plan_id] = plan
            self.ledger.persist()
            return plan

    def confirm_plan(self, plan_id: str, minute: int,
                     version: Optional[str] = None) -> CallSheet:
        """确认草稿并生成新版排演单。

        代次栅栏：草稿基于的版本必须仍是当前确认版本，否则另一套改排已经
        先占用了同一资源，本草稿必须带新数据重算，不能拿旧可用时段直接生效。
        """
        with self.ledger.lock:
            plan = self.ledger.plans.get(plan_id)
            if plan is None:
                raise EngineError("unknown_plan", f"未知改排单 {plan_id}", 404)
            if plan.status == "confirmed":
                return self.ledger.sheets[self._sheet_version_of(plan)]
            # 代次栅栏优先：即便草稿已被标记 superseded，也要把"旧可用时段
            # 不能直接生效"的原因明确返回，而不是笼统拒绝
            if plan.generation != self.ledger.current_generation \
                    or plan.base_version != self.ledger.current_sheet.version:
                plan.status = "rejected"
                self.ledger.persist()
                raise EngineError(
                    "stale_plan",
                    f"改排单基于第 {plan.generation} 代（{plan.base_version}），"
                    f"当前已是第 {self.ledger.current_generation} 代"
                    f"（{self.ledger.current_sheet.version}）；资源占用已变化，"
                    f"请基于新版重新改排", 409)
            if plan.status != "proposed":
                raise EngineError("plan_not_open",
                                  f"改排单 {plan_id} 状态为 {plan.status}，不能再生效", 409)
            if not plan.valid:
                raise EngineError("plan_invalid",
                                  f"改排单 {plan_id} 仍有阻断性问题，不能确认", 422)

            new_version = version or f"board-{self.ledger.current_generation + 1}"
            if new_version in self.ledger.sheets:
                raise EngineError("version_exists", f"版本号 {new_version} 已存在", 409)
            sheet = CallSheet(
                version=new_version,
                generation=self.ledger.current_generation + 1,
                bookings=plan.bookings,
                created_minute=int(minute),
                plan_id=plan.plan_id,
            )
            self.ledger.sheets[new_version] = sheet
            self.ledger.sheet_order.append(new_version)
            plan.status = "confirmed"
            for other in self.ledger.plans.values():
                if other is not plan and other.status == "proposed":
                    other.status = "superseded"
            self.ledger.persist()
            return sheet

    def _sheet_version_of(self, plan: PlanDraft) -> str:
        for version in reversed(self.ledger.sheet_order):
            if self.ledger.sheets[version].plan_id == plan.plan_id:
                return version
        raise EngineError("sheet_missing", "已确认改排单缺少对应排演单", 500)

    def list_plans(self) -> list[dict]:
        with self.ledger.lock:
            return [p.to_dict() for p in self.ledger.plans.values()]

    def sheet(self, version: Optional[str] = None) -> CallSheet:
        with self.ledger.lock:
            if version is None:
                return self.ledger.current_sheet
            if version not in self.ledger.sheets:
                raise EngineError("unknown_version", f"未知版本 {version}", 404)
            return self.ledger.sheets[version]

    def resource_view(self) -> dict:
        """当前确认版本中各队/道具的占用时段（未确认草稿不在其中）。"""
        with self.ledger.lock:
            view: dict[str, list[dict]] = {}
            for booking in self.ledger.current_sheet.bookings:
                for resource in (booking.team, booking.equipment):
                    if resource:
                        view.setdefault(resource, []).append({
                            "booking_id": booking.booking_id,
                            "program": booking.program,
                            "stop": booking.stop,
                            "start": booking.start, "end": booking.end,
                        })
            for rows in view.values():
                rows.sort(key=lambda r: r["start"])
            return {"generation": self.ledger.current_generation,
                    "version": self.ledger.current_sheet.version,
                    "resources": view}

    # ================= 值班台：现场记录 =================

    def record_progress(self, kind: str, minute: int,
                        booking_id: Optional[str] = None,
                        team: Optional[str] = None,
                        stop: Optional[str] = None,
                        groups: Optional[list[str]] = None,
                        version: Optional[str] = None,
                        cursor: Optional[int] = None) -> ProgressRecord:
        if kind not in {"team_start", "pickup"}:
            raise EngineError("invalid_progress_kind",
                              "kind 仅支持 team_start/pickup", 400)
        with self.ledger.lock:
            if booking_id and self.ledger.booking(booking_id) is None:
                raise EngineError("unknown_booking",
                                  f"当前排演单中无预约 {booking_id}", 404)
            if kind == "pickup":
                for group_id in groups or []:
                    if group_id not in self.ledger.groups:
                        raise EngineError("unknown_group",
                                          f"未知游客团 {group_id}", 404)
                if not stop:
                    booking = self.ledger.booking(booking_id) if booking_id else None
                    stop = booking.stop if booking else None
            record = ProgressRecord(
                minute=int(minute), kind=kind, booking_id=booking_id, team=team,
                stop=stop, groups=tuple(groups or ()), version=version,
                cursor=cursor,
            )
            self.ledger.progress.append(record)
            self.ledger.persist()
            return record

    # ================= 路口围栏与送达回执 =================

    def record_fence(self, stop: str, version: str, action: str, minute: int,
                     cursor: Optional[int] = None) -> FenceState:
        if action not in {"released", "blocked"}:
            raise EngineError("invalid_fence_action",
                              "action 仅支持 released/blocked", 400)
        with self.ledger.lock:
            state = FenceState(stop, version, action, int(minute), cursor)
            self.ledger.fences[stop] = state
            self.ledger.persist()
            return state

    def note_delivery(self, stop: str, version: str, delivered: bool,
                      in_effect: bool, minute: int,
                      cursor: Optional[int] = None) -> DeliveryState:
        with self.ledger.lock:
            key = (stop, version)
            state = self.ledger.deliveries.get(key)
            if state is None:
                state = DeliveryState(stop, version)
                self.ledger.deliveries[key] = state
            state.delivered = delivered
            state.in_effect = in_effect
            state.updated_minute = int(minute)
            state.cursor = cursor
            self.ledger.persist()
            return state

    def distribute(self, stop: str, version: str, minute: int,
                   client) -> dict:
        """向站点派发排演单，回执按 reference 幂等，断线可安全重试。"""
        with self.ledger.lock:
            if version not in self.ledger.sheets:
                raise EngineError("unknown_version", f"未知版本 {version}", 404)
        reference = f"sheet-{version}-{stop}"
        receipt = client.post_receipt(
            reference=reference, operation="post-call-sheet", recipient=stop)
        with self.ledger.lock:
            return self.note_delivery(
                stop, version,
                delivered=bool(receipt.get("received", True)),
                in_effect=bool(receipt.get("in_effect", receipt.get("received", True))),
                minute=minute).to_dict()

    # ================= 对账与导游授权 =================

    def reconciliation(self) -> dict:
        """找回各站到底收到、执行了哪版；未确认方向不得用于带队。"""
        with self.ledger.lock:
            current = self.ledger.current_sheet
            stops = sorted({a for pair in self.ledger.segment_geometry.values()
                            for a in pair}
                           | {b.stop for b in self.ledger.deliveries.values()})
            stop_rows = []
            for stop in stops:
                known = [d.to_dict() for d in self.ledger.versions_at_stop(stop)]
                effective = [d for d in known if d["in_effect"]]
                if len(effective) > 1:
                    authority = "conflict"
                elif effective:
                    authority = "current" if effective[0]["version"] == current.version \
                        else "stale"
                elif any(d["delivered"] for d in known):
                    authority = "received_not_effective"
                else:
                    authority = "unknown"
                fence = self.ledger.fences.get(stop)
                stop_rows.append({
                    "stop": stop,
                    "deliveries": known,
                    "authority": authority,
                    "effective_version": effective[0]["version"] if len(effective) == 1
                    else None,
                    "fence": fence.to_dict() if fence else None,
                    "may_follow": authority == "current",
                })
            return {
                "current_version": current.version,
                "generation": current.generation,
                "stops": stop_rows,
                "progress": [p.to_dict() for p in self.ledger.progress],
                "executed_booking_ids": sorted(self.ledger.executed_booking_ids()),
                "promises": promise_status(self.ledger),
            }

    def guide_route(self, group_id: str, destination: str, minute: int) -> dict:
        """导游只能按已确认且在该站生效的版本带队；未确认方向一律拒绝。"""
        with self.ledger.lock:
            group = self.ledger.groups.get(group_id)
            if group is None:
                raise EngineError("unknown_group", f"未知游客团 {group_id}", 404)
            origin = group.current_stop or group.stop
            current = self.ledger.current_sheet
            delivery = self.ledger.delivery(origin, current.version)
            if delivery is None or not delivery.in_effect:
                return {
                    "authorized": False,
                    "reason": f"站点 {origin} 尚未确认版本 {current.version} 生效，"
                              f"不得按未确认的通行方向带队",
                    "code": "version_not_effective",
                }
            route = find_route(self.ledger, origin, destination, int(minute),
                               current.version)
            if not route["ok"]:
                return {"authorized": False, "code": "no_route",
                        "reason": "当前已确认通路下无法到达",
                        "blocked": [f.to_dict() for f in route["blocked"]]}
            if group.disposition == "waiting" and destination != group.stop:
                return {
                    "authorized": False,
                    "reason": f"游客团 {group_id} 只能原地等候",
                    "code": "group_waiting",
                }
            return {"authorized": True, "version": current.version, **route}

    # ================= 事件摄入（来自事件流/现场上报） =================

    def ingest_event(self, cursor: int, source: str, minute: int,
                     payload: dict) -> dict:
        with self.ledger.lock:
            if cursor <= self.ledger.event_cursor:
                raise EngineError("stale_cursor",
                                  f"游标 {cursor} 已处理（当前 {self.ledger.event_cursor}）",
                                  409)
            self.ledger.event_cursor = cursor
            self.ledger.events.append(
                IngestedEvent(cursor, source, int(minute), payload))
            kind = self._apply_event(cursor, source, int(minute), payload)
            self.ledger.persist()
            return {"cursor": cursor, "applied": kind}

    def _apply_event(self, cursor: int, source: str, minute: int,
                     payload: dict) -> str:
        if source == "venue":
            segment = payload.get("segment")
            if segment is None:
                # 兼容只给 site 的台账风格：优先按事件时点匹配临时改向的分段
                site = payload.get("site")
                candidates = [seg for seg, (a, b)
                              in self.ledger.segment_geometry.items()
                              if site in (a, b)]
                timed = [seg for seg in candidates
                         if any(r.since == minute
                                for r in self.ledger.segment_reports.get(seg, []))]
                if len(timed) == 1:
                    segment = timed[0]
                elif len(candidates) == 1:
                    segment = candidates[0]
                else:
                    raise EngineError("ambiguous_segment",
                                      f"场地 {site} 无法唯一定位分段，请在事件中"
                                      f"携带 segment 字段", 422)
            mapping = {"outbound_only": "outbound", "inbound_only": "inbound",
                       "both_ways": "both", "closed": "closed"}
            direction = mapping.get(payload.get("change", ""),
                                    payload.get("direction", "closed"))
            self.report_segment(segment, direction, minute, cursor)
            return "segment_report"

        if source == "audience":
            group_id = payload.get("trip") or payload.get("group")
            stop = payload.get("arrived") or payload.get("stop")
            disposition = payload.get("disposition", "waiting")
            if group_id and stop:
                self.register_audience(group_id, stop, minute, disposition, cursor)
                return "audience"
            return "ignored"

        if source == "stage":
            version = payload.get("version")
            site = payload.get("site")
            state = payload.get("state")
            if state in {"released", "blocked"} and site and version:
                self.record_fence(site, version, state, minute, cursor)
                return "fence"
            if state in {"called", "received", "in_effect", "delivered"} \
                    and site and version:
                in_effect = state in {"called", "in_effect"}
                self.note_delivery(site, version, delivered=True,
                                   in_effect=in_effect, minute=minute,
                                   cursor=cursor)
                return "delivery"
            return "ignored"

        if source == "marshal":
            self.record_progress(
                kind=payload.get("kind", "team_start"), minute=minute,
                booking_id=payload.get("booking_id"), team=payload.get("team"),
                stop=payload.get("stop"), groups=payload.get("groups"),
                version=payload.get("version"), cursor=cursor)
            return "progress"

        return "ignored"
