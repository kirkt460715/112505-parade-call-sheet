import unittest

from rehearsal.engine import find_route, promise_status
from rehearsal.ledger import Ledger

from helpers import build_service, error_codes, warning_codes


class RoutingTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()
        self.ledger = self.service.ledger

    def test_direction_takes_effect_at_change_minute(self):
        # 临时改向前按双向基线通行
        self.assertTrue(find_route(self.ledger, "plaza", "yard", 29)["ok"])
        self.assertTrue(find_route(self.ledger, "yard", "plaza", 29)["ok"])
        # 第 30 分钟起 plaza-yard 仅 outbound：封掉绕行段后反向无路可走
        self.service.report_segment("yard-bridge", "closed", 30)
        self.assertTrue(find_route(self.ledger, "plaza", "yard", 30)["ok"])
        result = find_route(self.ledger, "yard", "plaza", 30)
        self.assertFalse(result["ok"])
        self.assertIn("no_route", {f.code for f in result["blocked"]})
        # 场地再次上报改回双向后恢复
        self.service.report_segment("plaza-yard", "both", 35)
        self.assertTrue(find_route(self.ledger, "yard", "plaza", 35)["ok"])

    def test_fence_split_blocks_both_directions(self):
        # 同一分段两端版本不一致：两边都不能自行推断已开放
        self.service.record_fence("plaza", "board-2", "released", 31)
        self.service.record_fence("yard", "board-1", "blocked", 31)
        result = find_route(self.ledger, "plaza", "yard", 31)
        self.assertFalse(result["ok"])
        self.assertTrue(any(f.code == "fence_split" for f in result["blocked"]))
        reverse = find_route(self.ledger, "yard", "plaza", 31)
        self.assertFalse(reverse["ok"])
        status = self.service.segment_status(31)
        plaza_yard = next(s for s in status if s["segment"] == "plaza-yard")
        self.assertTrue(plaza_yard["fence"]["split_brain"])
        # 两端对齐为同版放行后恢复
        self.service.record_fence("yard", "board-2", "released", 32)
        self.assertTrue(find_route(self.ledger, "plaza", "yard", 32)["ok"])

    def test_closed_segment_is_removed_from_graph(self):
        self.service.report_segment("yard-bridge", "closed", 40)
        result = find_route(self.ledger, "yard", "bridge", 40)
        self.assertFalse(result["ok"])


class BufferAndResourceTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def test_costume_and_movement_buffers_enforced(self):
        # drum-a: 25 在 plaza 结束，移动缓冲 9 分钟；b3 改到 40 开演仅隔 15 分钟，
        # 但还要经 plaza-yard-bridge 转场——缓冲不足先报 movement_buffer_short
        plan = self.service.create_plan(
            [{"op": "move", "booking_id": "b3", "stop": "bridge",
              "start": 30, "end": 40}], minute=30)
        self.assertIn("movement_buffer_short", error_codes(plan))
        # 间隔满足移动缓冲（25 + 9 = 34）且通路可达时放行
        good = self.service.create_plan(
            [{"op": "move", "booking_id": "b3", "stop": "bridge",
              "start": 42, "end": 52}], minute=30)
        self.assertNotIn("movement_buffer_short", error_codes(good))
        self.assertNotIn("team_unreachable", error_codes(good))

    def test_costume_buffer_applies_even_without_location_change(self):
        # 换装缓冲 8 分钟：同站连续两场不同节目也必须留出时间
        plan = self.service.create_plan(
            [{"op": "move", "booking_id": "b3", "stop": "plaza",
              "start": 30, "end": 40}], minute=20)
        # 同站移动按 movement=9 校验，30-25=5 < 9
        self.assertTrue({"movement_buffer_short", "resource_overlap"}
                        & error_codes(plan))
        good = self.service.create_plan(
            [{"op": "move", "booking_id": "b3", "stop": "plaza",
              "start": 42, "end": 52}], minute=20)
        self.assertNotIn("movement_buffer_short", error_codes(good))

    def test_shared_equipment_needs_handover_gap(self):
        # cart-2 被 welcome(plaza,10-25) 与 court-song(yard,35-50) 共用
        plan = self.service.create_plan(
            [{"op": "move", "booking_id": "b2", "stop": "yard",
              "start": 25, "end": 40}], minute=20)
        self.assertIn("resource_overlap", error_codes(plan))

    def test_double_booking_same_team_rejected(self):
        plan = self.service.create_plan(
            [{"op": "substitute", "for_program": "court-song",
              "stop": "plaza", "team": "drum-a",
              "start": 20, "end": 30}], minute=20)
        self.assertIn("resource_overlap", error_codes(plan))


class ReplanRulesTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def test_to_fixed_must_keep_interactions(self):
        # 先构造一个没有承诺互动的巡游预约，再定点化时必须显式补上
        from rehearsal.models import Booking, CallSheet
        sheet = self.service.ledger.current_sheet
        stripped = Booking(
            booking_id="b3", program="bridge-parade", stop="bridge",
            team="drum-a", equipment=None, start=45, end=55,
            kind="parade", slot_id="bridge-parade", interactions=(),
            access=("step-free",))
        self.service.ledger.sheets[sheet.version] = CallSheet(
            version=sheet.version, generation=sheet.generation,
            bookings=tuple(stripped if b.booking_id == "b3" else b
                           for b in sheet.bookings),
            created_minute=sheet.created_minute)
        plan = self.service.create_plan(
            [{"op": "to_fixed", "booking_id": "b3", "stop": "bridge",
              "start": 60, "end": 70}], minute=40)
        self.assertIn("interaction_lost", error_codes(plan))
        kept = self.service.create_plan(
            [{"op": "to_fixed", "booking_id": "b3", "stop": "bridge",
              "start": 60, "end": 70,
              "interactions": ["flag-wave"], "access": ["step-free"]}],
            minute=40)
        self.assertNotIn("interaction_lost", error_codes(kept))

    def test_substitute_must_use_preapproved_stop(self):
        plan = self.service.create_plan(
            [{"op": "substitute", "for_program": "court-song",
              "stop": "bridge", "team": "dance-b",
              "start": 35, "end": 50}], minute=30)
        self.assertIn("substitute_stop_not_eligible", error_codes(plan))

    def test_absent_team_forces_substitute(self):
        self.service.ledger.absent_teams.add("dance-b")
        plan = self.service.create_plan(
            [{"op": "move", "booking_id": "b2", "stop": "yard",
              "start": 36, "end": 51}], minute=30)
        self.assertIn("team_absent", error_codes(plan))
        # 替补用别的队伍、在认可场地、保留无障碍进入方式；
        # trip-19 已在 yard 原地等候而巷口只出不进，承诺无法兑现须显式暴露
        self.service.register_audience("trip-19", "yard", 32, "waiting")
        sub = self.service.create_plan(
            [{"op": "cancel", "booking_id": "b2"},
             {"op": "substitute", "for_program": "court-song",
              "stop": "plaza", "team": "dance-c",
              "start": 36, "end": 51,
              "access": ["step-free"]}], minute=33)
        self.assertEqual(
            error_codes(sub),
            {"promise_not_deliverable", "audience_cannot_move"})
        # 允许把游客带走时，经 yard-bridge-plaza-south 可达 south 的替补
        self.service.register_audience("trip-19", "yard", 32, "movable")
        movable = self.service.create_plan(
            [{"op": "cancel", "booking_id": "b2"},
             {"op": "substitute", "for_program": "court-song",
              "stop": "south", "team": "dance-c",
              "start": 40, "end": 55,
              "access": ["step-free"]}], minute=33)
        self.assertEqual(error_codes(movable), set())

    def test_executed_booking_is_immutable(self):
        self.service.record_progress("team_start", 35, booking_id="b2",
                                     team="dance-b", stop="yard",
                                     version="board-1")
        plan = self.service.create_plan(
            [{"op": "move", "booking_id": "b2", "stop": "plaza",
              "start": 60, "end": 70}], minute=40)
        self.assertIn("executed_immutable", error_codes(plan))


class PromiseTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def test_promise_committed_when_audience_can_attend(self):
        rows = promise_status(self.service.ledger)
        row = next(r for r in rows if r["group"] == "trip-19")
        self.assertEqual(row["status"], "committed")

    def test_promise_blocked_when_route_cut_and_group_waiting(self):
        # 巷口 30 分钟起只能出不能进；trip-19 在 yard 原地等候，
        # 若把 court-song 改到 plaza，观众实际无法参加
        self.service.register_audience("trip-19", "yard", 32, "waiting")
        plan = self.service.create_plan(
            [{"op": "cancel", "booking_id": "b2"},
             {"op": "substitute", "for_program": "court-song",
              "stop": "plaza", "team": "dance-b",
              "start": 40, "end": 55,
              "access": ["step-free"]}], minute=33)
        self.assertIn("promise_not_deliverable", error_codes(plan))

    def test_promise_fulfilled_only_by_actual_pickup(self):
        # 即使排演单上还有节目，没有接人事实就不能自认为已兑现
        rows = promise_status(self.service.ledger)
        self.assertNotEqual(
            next(r for r in rows if r["group"] == "trip-19")["status"],
            "fulfilled")
        self.service.record_progress("pickup", 36, booking_id="b2",
                                     stop="yard", groups=["trip-19"],
                                     version="board-1")
        rows = promise_status(self.service.ledger)
        self.assertEqual(
            next(r for r in rows if r["group"] == "trip-19")["status"],
            "fulfilled")

    def test_access_need_must_be_satisfied(self):
        plan = self.service.create_plan(
            [{"op": "substitute", "for_program": "court-song",
              "stop": "south", "team": "dance-b",
              "start": 40, "end": 55, "access": []}], minute=30)
        self.assertTrue(
            any(f.code == "access_unmet" for f in plan.findings))


if __name__ == "__main__":
    unittest.main()
