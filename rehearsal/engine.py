"""改排规则引擎：可达性、资源占用、承诺审计、不可变进度。"""

from __future__ import annotations

from collections import deque
from typing import Optional

from .ledger import Ledger
from .models import Booking, Finding


class EngineError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code = code
        self.status = status


# ---------- 通路与围栏 ----------

def segment_direction(ledger: Ledger, segment: str, minute: int) -> str:
    """取某时刻有效的方向。

    台账首条登记是基线方向，其 since(change_at) 表示临时改向的生效点；
    生效点之前按基线方向通行，生效点之后按最新上报方向通行。
    """
    reports = ledger.segment_reports.get(segment)
    if not reports:
        return "closed"
    active = [r for r in reports if r.since <= minute]
    return (active[-1] if active else reports[0]).direction


def _fence_allows(ledger: Ledger, segment: str) -> Optional[Finding]:
    """围栏检查：同一分段两端版本不一致即视为裂脑，两边都不得推断已开放。"""
    a, b = ledger.segment_geometry[segment]
    fa, fb = ledger.fences.get(a), ledger.fences.get(b)
    if fa is None or fb is None:
        return None  # 未登记围栏的一端不做推断限制
    if fa.version != fb.version or fa.action != fb.action:
        return Finding(
            "error", "fence_split",
            f"分段 {segment} 两端围栏不一致：{a}={fa.version}/{fa.action}，"
            f"{b}={fb.version}/{fb.action}，任一端不得自行推断该段已开放",
            ref=segment,
        )
    if fa.action != "released":
        return Finding("error", "fence_blocked",
                       f"分段 {segment} 在版本 {fa.version} 下仍被拦截", ref=segment)
    return None


def find_route(ledger: Ledger, origin: str, destination: str, minute: int,
               version: Optional[str] = None) -> dict:
    """按 minute 时刻的有效方向做 BFS；任何一段围栏不一致则该路不可走。

    返回 {"ok": bool, "path": [segment...], "stops": [...], "blocked": [Finding...]}。
    围栏不通过的边直接从图中剔除，并在 blocked 中保留原因。
    """
    if origin == destination:
        return {"ok": True, "path": [], "stops": [origin], "blocked": []}

    adjacency: dict[str, list[tuple[str, str]]] = {}
    blocked: list[Finding] = []
    for stops in ledger.segment_geometry.values():
        adjacency.setdefault(stops[0], [])
        adjacency.setdefault(stops[1], [])
    fence_errors: dict[str, Finding] = {}
    for segment, (a, b) in ledger.segment_geometry.items():
        direction = segment_direction(ledger, segment, minute)
        fence_finding = _fence_allows(ledger, segment)
        if fence_finding is not None:
            blocked.append(fence_finding)
            fence_errors[segment] = fence_finding
            continue
        if direction in ("both", "outbound"):
            adjacency.setdefault(a, []).append((b, segment))
        if direction in ("both", "inbound"):
            adjacency.setdefault(b, []).append((a, segment))
        if direction == "closed":
            blocked.append(Finding("warning", "segment_closed",
                                   f"分段 {segment} 在 {minute} 分钟关闭", segment))

    # 起终点之间的每一段都必须无围栏裂脑，否则该出行请求不得放行
    for segment, finding in fence_errors.items():
        edge_stops = set(ledger.segment_geometry[segment])
        if origin in edge_stops or destination in edge_stops:
            return {"ok": False, "path": [], "stops": [],
                    "blocked": blocked + [finding]}

    queue = deque([(origin, [origin], [])])
    seen = {origin}
    while queue:
        stop, stops, path = queue.popleft()
        for nxt, segment in adjacency.get(stop, []):
            if nxt in seen:
                continue
            if nxt == destination:
                return {"ok": True, "path": path + [segment],
                        "stops": stops + [nxt], "blocked": blocked}
            seen.add(nxt)
            queue.append((nxt, stops + [nxt], path + [segment]))
    blocked.append(Finding("error", "no_route",
                           f"{minute} 分钟时从 {origin} 到 {destination} 无可用通路"))
    return {"ok": False, "path": [], "stops": [], "blocked": blocked}


# ---------- 缓冲与资源占用 ----------

def _required_gap(ledger: Ledger, first: Booking, second: Booking) -> int:
    """同一资源连续两场预约之间至少需要的间隔。"""
    gap = 0
    if first.team and first.team == second.team:
        buffer = ledger.buffers.get(first.team)
        if buffer:
            gap = max(gap, buffer.movement)
            if first.program != second.program:
                gap = max(gap, buffer.costume)
    if first.equipment and first.equipment == second.equipment:
        gap = max(gap, 1)  # 道具交接至少需要 1 分钟
    return gap


def _resource_conflicts(ledger: Ledger, bookings: list[Booking]) -> list[Finding]:
    findings: list[Finding] = []
    resources = {r for b in bookings for r in (b.team, b.equipment) if r}
    for resource in sorted(resources):
        mine = sorted((b for b in bookings if b.occupies(resource)),
                      key=lambda b: (b.start, b.end))
        for earlier, later in zip(mine, mine[1:]):
            gap = _required_gap(ledger, earlier, later)
            if later.start < earlier.end + gap:
                findings.append(Finding(
                    "error", "resource_overlap",
                    f"资源 {resource} 冲突：{earlier.booking_id}({earlier.start}-{earlier.end}) "
                    f"与 {later.booking_id}({later.start}-{later.end}) 间隔不足 {gap} 分钟"
                    f"（含换装/移动/交接）",
                    ref=resource,
                ))
    return findings


# ---------- 改排草稿 ----------

def apply_changes(ledger: Ledger, base_version: str,
                  changes: list[dict]) -> list[Booking]:
    """把改排操作应用到基线版本，产出候选预约列表（尚未校验）。"""
    if base_version not in ledger.sheets:
        raise EngineError("unknown_version", f"未知排演单版本 {base_version}", 404)
    bookings = list(ledger.sheets[base_version].bookings)
    by_id = {b.booking_id: b for b in bookings}

    for index, change in enumerate(changes):
        op = change.get("op")
        if op == "move":
            current = by_id.get(change["booking_id"])
            if current is None:
                raise EngineError("unknown_booking",
                                  f"预约 {change['booking_id']} 不存在", 404)
            updated = Booking(
                booking_id=current.booking_id, program=current.program,
                stop=change.get("stop", current.stop),
                team=current.team, equipment=current.equipment,
                start=int(change["start"]), end=int(change["end"]),
                kind=current.kind, slot_id=current.slot_id,
                substitution_for=current.substitution_for,
                interactions=current.interactions, access=current.access,
            )
            bookings = [updated if b.booking_id == updated.booking_id else b
                       for b in bookings]
            by_id[updated.booking_id] = updated
        elif op == "to_fixed":
            current = by_id.get(change["booking_id"])
            if current is None:
                raise EngineError("unknown_booking",
                                  f"预约 {change['booking_id']} 不存在", 404)
            updated = Booking(
                booking_id=current.booking_id, program=current.program,
                stop=change.get("stop", current.stop),
                team=current.team, equipment=current.equipment,
                start=int(change["start"]), end=int(change["end"]),
                kind="fixed", slot_id=current.slot_id,
                substitution_for=current.substitution_for,
                interactions=tuple(change.get("interactions", current.interactions)),
                access=tuple(change.get("access", current.access)),
            )
            bookings = [updated if b.booking_id == updated.booking_id else b
                       for b in bookings]
            by_id[updated.booking_id] = updated
        elif op == "substitute":
            sub = next((s for s in ledger.substitutes
                        if s.for_program == change["for_program"]), None)
            if sub is None:
                raise EngineError("no_substitute",
                                  f"节目 {change['for_program']} 无事先认可的替补", 422)
            bookings.append(Booking(
                booking_id=change.get("booking_id", f"sub-{index + 1}"),
                program=sub.program, stop=change["stop"],
                team=change.get("team"), equipment=change.get("equipment"),
                start=int(change["start"]), end=int(change["end"]),
                kind=change.get("kind", "fixed"),
                substitution_for=change["for_program"],
                interactions=tuple(change.get("interactions", [])),
                access=tuple(change.get("access", sorted(sub.access))),
            ))
        elif op == "cancel":
            bookings = [b for b in bookings if b.booking_id != change["booking_id"]]
            by_id.pop(change["booking_id"], None)
        else:
            raise EngineError("unknown_op", f"不支持的改排操作 {op!r}", 400)
    return bookings


def validate_plan(ledger: Ledger, changes: list[dict],
                  candidates: list[Booking], minute: int) -> list[Finding]:
    findings: list[Finding] = []
    executed = ledger.executed_booking_ids()
    base = {b.booking_id: b for b in ledger.current_sheet.bookings}

    # 1. 已执行部分不可变
    touched = {c.get("booking_id") for c in changes if c.get("op") != "substitute"}
    for booking_id in touched & executed:
        findings.append(Finding("error", "executed_immutable",
                                f"预约 {booking_id} 已开始执行，只能保留不能改",
                                ref=booking_id))

    # 2. 缺席队伍不得被再次占用
    for booking in candidates:
        if booking.team and booking.team in ledger.absent_teams:
            findings.append(Finding("error", "team_absent",
                                    f"队伍 {booking.team} 已缺席，"
                                    f"须使用事先认可的替补节目",
                                    ref=booking.booking_id))

    # 3. 资源冲突（同一队/道具的时间重叠与缓冲）
    findings.extend(_resource_conflicts(ledger, candidates))

    # 4. 可达性：队伍要能在开演前从上一场地点赶到
    for booking in candidates:
        if not booking.team:
            continue
        prior = [b for b in candidates
                 if b.team == booking.team and b.end <= booking.start
                 and b.booking_id != booking.booking_id]
        if not prior:
            continue
        previous = max(prior, key=lambda b: b.end)
        buffer = ledger.buffers.get(booking.team)
        need = buffer.movement if buffer else 0
        if previous.stop == booking.stop:
            # 同站转场不涉及巷口通路，但移动/换装缓冲仍要满足
            if need and booking.start - previous.end < need:
                findings.append(Finding(
                    "error", "movement_buffer_short",
                    f"队伍 {booking.team} 同站转场缓冲需 {need} 分钟，"
                    f"实际仅 {booking.start - previous.end} 分钟",
                    ref=booking.booking_id))
            continue
        if need and booking.start - previous.end < need:
            findings.append(Finding(
                "error", "movement_buffer_short",
                f"队伍 {booking.team} 移动缓冲需 {need} 分钟，"
                f"实际仅 {booking.start - previous.end} 分钟",
                ref=booking.booking_id))
        else:
            route = find_route(ledger, previous.stop, booking.stop,
                               previous.end)
            if not route["ok"]:
                findings.append(Finding(
                    "error", "team_unreachable",
                    f"队伍 {booking.team} 无法从 {previous.stop} 赶到 "
                    f"{booking.stop} 参加 {booking.booking_id}",
                    ref=booking.booking_id))

    # 5. 改点/替补的场地与观众进入方式
    for change in changes:
        if change.get("op") == "substitute":
            sub = next((s for s in ledger.substitutes
                        if s.for_program == change["for_program"]), None)
            if sub and change["stop"] not in sub.eligible_stops:
                findings.append(Finding(
                    "error", "substitute_stop_not_eligible",
                    f"替补 {sub.program} 未认可在 {change['stop']} 演出",
                    ref=change["for_program"]))
        if change.get("op") == "to_fixed":
            current = base.get(change["booking_id"])
            if current and current.kind == "parade" and not current.interactions \
                    and not change.get("interactions"):
                findings.append(Finding(
                    "error", "interaction_lost",
                    f"巡游改为定点 {change['booking_id']} 时必须保留承诺的互动内容",
                    ref=change["booking_id"]))

    # 6. 游客承诺审计
    findings.extend(audit_promises(ledger, candidates, minute))
    return findings


def audit_promises(ledger: Ledger, bookings: list[Booking],
                   minute: int) -> list[Finding]:
    """只有观众仍能实际参加（可达 + 进入方式满足 + 开演前到位）才算承诺兑现。"""
    findings: list[Finding] = []
    picked_up = ledger.picked_up_groups()
    by_program: dict[str, list[Booking]] = {}
    for booking in bookings:
        by_program.setdefault(booking.program, []).append(booking)
        if booking.substitution_for:
            by_program.setdefault(booking.substitution_for, []).append(booking)

    for group in ledger.groups.values():
        if group.group in picked_up:
            continue  # 已有现场接人记录，承诺按实际参加认定
        options = by_program.get(group.promised_program, [])
        viable: list[Booking] = []
        for booking in options:
            if not all(need in booking.access for need in group.needs):
                findings.append(Finding(
                    "warning", "access_unmet",
                    f"游客团 {group.group} 需要 {list(group.needs)}，"
                    f"{booking.booking_id} 的观众进入方式不满足",
                    ref=group.group))
                continue
            here = group.current_stop or group.stop
            if booking.stop != here and group.disposition == "waiting":
                findings.append(Finding(
                    "error", "audience_cannot_move",
                    f"游客团 {group.group} 已在 {here} 到位且只能原地等候，"
                    f"无法前往 {booking.stop} 参加 {booking.program}",
                    ref=group.group))
                continue
            route = find_route(ledger, here, booking.stop, minute)
            if not route["ok"]:
                findings.append(Finding(
                    "warning", "audience_unreachable",
                    f"游客团 {group.group} 无法抵达 {booking.stop} 的 "
                    f"{booking.program}", ref=group.group))
                continue
            if group.arrival > booking.start:
                findings.append(Finding(
                    "warning", "audience_late",
                    f"游客团 {group.group} 第 {group.arrival} 分钟才到位，"
                    f"赶不上 {booking.start} 分钟开演", ref=group.group))
                continue
            viable.append(booking)
        if not options:
            findings.append(Finding(
                "error", "promise_without_program",
                f"游客团 {group.group} 被承诺的 {group.promised_program} "
                f"在新版排演单中无任何安排", ref=group.group))
        elif not viable:
            findings.append(Finding(
                "error", "promise_not_deliverable",
                f"游客团 {group.group} 无法实际参加 {group.promised_program}，"
                f"原承诺不能算作已兑现", ref=group.group))
    return findings


def promise_status(ledger: Ledger, version: Optional[str] = None) -> list[dict]:
    sheet = ledger.sheets[version] if version else ledger.current_sheet
    picked_up = ledger.picked_up_groups()
    rows: list[dict] = []
    for group in ledger.groups.values():
        if group.group in picked_up:
            booking = next((b for b in sheet.bookings
                            if b.booking_id == picked_up[group.group]), None)
            rows.append({"group": group.group, "status": "fulfilled",
                         "program": booking.program if booking else None,
                         "stop": booking.stop if booking else None,
                         "reason": "现场接人记录确认实际参加"})
            continue
        findings = audit_promises(
            ledger, [b for b in sheet.bookings
                     if b.program == group.promised_program
                     or b.substitution_for == group.promised_program],
            group.arrival)
        hard = [f for f in findings if f.level == "error" and f.ref == group.group]
        rows.append({
            "group": group.group,
            "status": "blocked" if hard else "committed",
            "program": group.promised_program,
            "stop": group.current_stop or group.stop,
            "reason": hard[0].message if hard else "新版排演单仍可实际参加",
        })
    return rows
